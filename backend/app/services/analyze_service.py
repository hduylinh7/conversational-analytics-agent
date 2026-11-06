"""AnalyzeService: end-to-end analytics pipeline orchestration.

Pipeline: semantic retrieval -> Text-to-SQL generation with self-repair ->
guardrail re-validation (inside repository) -> PostgreSQL execution ->
execution-error self-repair loop (SQLSTATE feedback to the LLM).
"""

from app.agents.llm_client import LLMError
from app.agents.schemas import SQLGenerationRequest
from app.agents.sql_generator import SQLGenerator
from app.core.logging import get_logger
from app.repositories.analytics import AnalyticsRepository
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.schemas.analyze import AnalyzeResponse
from app.semantic.retrieval.retriever import SemanticRetriever
from app.semantic.retrieval.schemas import RetrievalResult, RetrievalStatus

logger = get_logger(__name__)

STAGE_RETRIEVAL = "retrieval"
STAGE_GENERATION = "generation"
STAGE_EXECUTION = "execution"


class AnalyzeService:
    """Coordinates retrieval, generation, and execution into a single response."""

    def __init__(
        self,
        retriever: SemanticRetriever | None = None,
        generator: SQLGenerator | None = None,
        repository: AnalyticsRepository | None = None,
    ) -> None:
        """Initialize the pipeline with injectable components for testing."""
        self.retriever = retriever or SemanticRetriever()
        self.generator = generator or SQLGenerator()
        self.repository = repository or AnalyticsRepository()

    def _retrieval_failure(self, retrieval: RetrievalResult) -> str | None:
        """Non-actionable guard: retrieval must yield metadata and a safe join plan."""
        if not retrieval.selected_metrics and not retrieval.selected_dimensions:
            return (
                "The question does not match any metrics or dimensions in the "
                "semantic catalog (or confidence is too low)."
            )

        if retrieval.status in (
            RetrievalStatus.INSUFFICIENT_METADATA,
            RetrievalStatus.UNSAFE_JOIN,
        ):
            return f"Retrieval stopped with status {retrieval.status}: {retrieval.warnings}"

        is_unsafe_join = not retrieval.is_safe_for_direct_execution or (
            retrieval.resolved_join_path is not None
            and not retrieval.resolved_join_path.is_safe_for_direct_execution
        )
        if is_unsafe_join:
            return "Resolved join path is unsafe for direct execution: " + "; ".join(
                retrieval.warnings
            )

        return None

    def _execution_failure(self, execution: ExecutionResult) -> str | None:
        """Map a failed execution to a user-facing reason, upgradeable by repair data."""
        if execution.is_success:
            return None

        reason = execution.error_message or f"Execution failed with status {execution.status}."
        if execution.status == ExecutionStatus.TIMEOUT:
            logger.warning("Execution timeout (code=%s)", execution.error_code)
        return reason

    async def analyze(
        self, question: str, *, model: str | None = None, temperature: float = 0.0
    ) -> AnalyzeResponse:
        """Run the full analytics pipeline for a natural language question."""
        retrieval = self.retriever.retrieve(question)

        retrieval_failure = self._retrieval_failure(retrieval)
        if retrieval_failure is not None:
            logger.info("Retrieval failure for question '%s': %s", question, retrieval_failure)
            return AnalyzeResponse(
                question=question,
                success=False,
                failed_stage=STAGE_RETRIEVAL,
                failure_reason=retrieval_failure,
                retrieval_status=retrieval.status,
                confidence_score=retrieval.confidence_score,
                warnings=retrieval.warnings,
            )

        try:
            generation, execution = await self.generator.generate_and_execute_async(
                SQLGenerationRequest(
                    question=question,
                    retrieval_result=retrieval,
                    model=model,
                    temperature=temperature,
                ),
                executor=self.repository.execute,
            )
        except LLMError as exc:
            logger.error("LLM provider error for question '%s': %s", question, exc)
            return AnalyzeResponse(
                question=question,
                success=False,
                failed_stage=STAGE_GENERATION,
                failure_reason=(
                    "SQL generation failed due to an LLM provider error: "
                    f"{str(exc).splitlines()[0][:500]}"
                ),
                retrieval_status=retrieval.status,
                confidence_score=retrieval.confidence_score,
                warnings=retrieval.warnings,
            )

        if not generation.is_valid or generation.final_sql is None:
            violation_messages = (
                [
                    f"{v.severity} {v.rule}: {v.message}"
                    for v in generation.validation_result.violations
                ]
                if generation.validation_result is not None
                else []
            )
            reason = (
                generation.explanation
                or "; ".join(violation_messages)
                or "The generated SQL failed all guardrail validation attempts."
            )
            logger.warning(
                "Generation failure for question '%s' after %d repair attempts",
                question,
                generation.repair_attempts,
            )
            return AnalyzeResponse(
                question=question,
                success=False,
                failed_stage=STAGE_GENERATION,
                failure_reason=reason,
                retrieval_status=retrieval.status,
                confidence_score=retrieval.confidence_score,
                final_sql=generation.final_sql,
                raw_sql=generation.raw_sql,
                explanation=generation.explanation,
                repair_attempts=generation.repair_attempts,
                model_used=generation.model_used,
                warnings=retrieval.warnings,
            )

        execution_failure = self._execution_failure(execution) if execution is not None else None
        warnings = [*retrieval.warnings]
        if generation.validation_result is not None:
            warnings.extend(generation.validation_result.warnings)
        for repair in generation.repair_history:
            if repair.stage == "execution" and repair.error_code:
                warnings.append(
                    f"SQL self-repair after execution error {repair.error_code}: "
                    f"{repair.error_message or 'no message'}"
                )

        success = execution is not None and execution.status == ExecutionStatus.SUCCESS
        return AnalyzeResponse(
            question=question,
            success=success,
            failed_stage=(
                None
                if success
                else (STAGE_EXECUTION if execution is not None else STAGE_GENERATION)
            ),
            failure_reason=execution_failure,
            retrieval_status=retrieval.status,
            confidence_score=retrieval.confidence_score,
            final_sql=generation.final_sql,
            raw_sql=generation.raw_sql,
            explanation=generation.explanation,
            repair_attempts=generation.repair_attempts,
            model_used=generation.model_used,
            warnings=warnings,
            execution=execution,
        )


_global_analyze_service: AnalyzeService | None = None


def get_analyze_service() -> AnalyzeService:
    """Return a singleton AnalyzeService instance."""
    global _global_analyze_service
    if _global_analyze_service is None:
        _global_analyze_service = AnalyzeService()
    return _global_analyze_service
