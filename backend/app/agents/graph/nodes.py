"""LangGraph node implementations for the conversational analytics pipeline.

Nodes are thin async wrappers around the components already built in earlier
phases (semantic retrieval, Text-to-SQL LLM calls, guardrail validation, and
PostgreSQL execution). All orchestration/loop control lives in the graph
edges and routing functions, not in the nodes themselves.
"""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.agents.graph.state import AnalyticsState
from app.agents.llm_client import BaseLLMClient, LLMError
from app.agents.prompts import (
    build_initial_messages,
    render_execution_repair_feedback_prompt,
    render_repair_feedback_prompt,
)
from app.agents.schemas import RepairStep, SQLGenerationRequest
from app.agents.sql_generator import extract_sql, format_violations
from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.schemas.analyze import AnalyzeResponse
from app.security.schemas import ValidationResult
from app.security.validator import validate_sql
from app.semantic.retrieval.schemas import RetrievalResult, RetrievalStatus

logger = get_logger(__name__)

STAGE_RETRIEVAL = "retrieval"
STAGE_GENERATION = "generation"
STAGE_EXECUTION = "execution"


class RetrieverLike(Protocol):
    """Structural type for the semantic retriever used by the graph."""

    def retrieve(self, question: str) -> RetrievalResult:
        """Return semantic metadata selected for the question."""


class ValidatorLike(Protocol):
    """Structural type for the SQL guardrail validator used by the graph."""

    def validate(self, sql: str) -> ValidationResult:
        """Validate a candidate SQL statement and return the guardrail report."""


def retrieval_failure_reason(retrieval: RetrievalResult) -> str | None:
    """Return a refusal reason when retrieval cannot safely drive generation."""
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


class AnalyticsNodes:
    """Node callables backing the analytics StateGraph.

    The class owns shared dependencies and repair budgets so every node is a
    pure function of the current state plus these injected collaborators.
    """

    def __init__(
        self,
        *,
        retriever: RetrieverLike,
        llm_client: BaseLLMClient,
        executor: Callable[[str], Awaitable[ExecutionResult]],
        validator: ValidatorLike | None = None,
        max_guardrail_repairs: int | None = None,
        max_execution_repairs: int | None = None,
        default_model: str | None = None,
    ) -> None:
        """Wire the graph nodes to existing pipeline components and budgets."""
        settings = get_settings()
        self.retriever = retriever
        self.llm_client = llm_client
        self.executor = executor
        self.validate: Callable[[str], ValidationResult] = (
            validator.validate if validator is not None else validate_sql
        )
        self.max_guardrail_repairs = (
            max_guardrail_repairs
            if max_guardrail_repairs is not None
            else settings.SQL_MAX_REPAIR_ATTEMPTS
        )
        self.max_execution_repairs = (
            max_execution_repairs
            if max_execution_repairs is not None
            else settings.SQL_MAX_EXECUTION_REPAIR_ATTEMPTS
        )
        self.default_model = default_model or settings.LLM_MODEL

    # ------------------------------------------------------------------
    # Nodes
    # ------------------------------------------------------------------

    async def retrieve(self, state: AnalyticsState) -> dict:
        """Run semantic retrieval and short-circuit unsafe or non-analytics questions."""
        question = state.get("question", "")
        retrieval = self.retriever.retrieve(question)
        failure = retrieval_failure_reason(retrieval)

        update: dict = {
            "retrieval": retrieval,
            "retrieval_failure": failure,
            "warnings": list(retrieval.warnings),
            "repair_history": [],
            "guardrail_repairs": 0,
            "execution_repairs": 0,
            "last_failure": None,
            "model_used": state.get("model") or self.default_model,
        }
        if failure is None:
            request = SQLGenerationRequest(
                question=question,
                retrieval_result=retrieval,
                model=state.get("model"),
                temperature=state.get("temperature", 0.0),
            )
            update["messages"] = build_initial_messages(request)
        return update

    async def generate(self, state: AnalyticsState) -> dict:
        """Ask the LLM for a SQL candidate using the shared repair conversation."""
        messages = state.get("messages", [])
        kwargs: dict = {"temperature": state.get("temperature", 0.0)}
        if state.get("model"):
            kwargs["model"] = state["model"]

        try:
            response_text = await self.llm_client.generate_async(
                [dict(message) for message in messages], **kwargs
            )
        except LLMError as exc:
            reason = (
                "SQL generation failed due to an LLM provider error: "
                f"{str(exc).splitlines()[0][:500]}"
            )
            logger.error("LLM provider error: %s", exc)
            return {"generation_error": reason}

        raw_sql = extract_sql(response_text) or ""
        return {
            "generation_error": None,
            "llm_response": response_text,
            "sql_candidate": raw_sql,
            "messages": [
                *messages,
                {"role": "assistant", "content": response_text},
            ],
        }

    async def validate_sql(self, state: AnalyticsState) -> dict:
        """Apply AST guardrails to the current SQL candidate."""
        result = self.validate(state.get("sql_candidate", ""))
        return {
            "validation_result": result,
            "last_failure": None if result.is_valid else "guardrail",
        }

    async def execute(self, state: AnalyticsState) -> dict:
        """Execute validated SQL against PostgreSQL through the analytics repository."""
        validation = state.get("validation_result")
        sql = (
            validation.sanitized_sql
            if validation is not None and validation.sanitized_sql
            else state.get("sql_candidate", "")
        )
        execution = await self.executor(sql)
        repairable_failure = not execution.is_success and execution.is_repairable
        return {
            "execution": execution,
            "last_failure": "execution" if repairable_failure else None,
        }

    async def repair(self, state: AnalyticsState) -> dict:
        """Append corrective feedback to the conversation and consume repair budget."""
        if state.get("last_failure") == "execution":
            return self._execution_repair(state)
        return self._guardrail_repair(state)

    def _guardrail_repair(self, state: AnalyticsState) -> dict:
        """Feed guardrail violations back to the LLM as the next user turn."""
        attempt = state.get("guardrail_repairs", 0) + 1
        validation = state.get("validation_result")
        violations = format_violations(validation) if validation is not None else []
        warnings = validation.warnings if validation is not None else []
        sql = state.get("sql_candidate", "")

        feedback_prompt = render_repair_feedback_prompt(
            generated_sql=sql,
            violations=violations,
            warnings=warnings,
            attempt=attempt,
            max_attempts=self.max_guardrail_repairs,
        )
        logger.warning(
            "SQL validation failed (attempt %d/%d), entering repair loop",
            attempt,
            self.max_guardrail_repairs,
        )
        step = RepairStep(
            attempt=len(state.get("repair_history", [])) + 1,
            stage="guardrail",
            generated_sql=sql,
            violations=validation.violations if validation is not None else [],
            warnings=warnings,
            feedback_prompt=feedback_prompt,
        )
        return {
            "guardrail_repairs": attempt,
            "repair_history": [*state.get("repair_history", []), step],
            "messages": [
                *state.get("messages", []),
                {"role": "user", "content": feedback_prompt},
            ],
        }

    def _execution_repair(self, state: AnalyticsState) -> dict:
        """Feed the PostgreSQL SQLSTATE error back to the LLM as the next user turn."""
        attempt = state.get("execution_repairs", 0) + 1
        execution = state.get("execution")
        error_code = execution.error_code if execution is not None else None
        error_message = execution.error_message if execution is not None else None
        sql = state.get("sql_candidate", "")

        feedback_prompt = render_execution_repair_feedback_prompt(
            generated_sql=sql,
            error_code=error_code,
            error_message=error_message,
            attempt=attempt,
            max_attempts=self.max_execution_repairs,
        )
        logger.warning(
            "SQL execution failed (repair attempt %d/%d, SQLSTATE=%s), entering repair loop",
            attempt,
            self.max_execution_repairs,
            error_code,
        )
        step = RepairStep(
            attempt=len(state.get("repair_history", [])) + 1,
            stage="execution",
            generated_sql=sql,
            feedback_prompt=feedback_prompt,
            error_code=error_code,
            error_message=error_message,
        )
        return {
            "execution_repairs": attempt,
            "repair_history": [*state.get("repair_history", []), step],
            "messages": [
                *state.get("messages", []),
                {"role": "user", "content": feedback_prompt},
            ],
        }

    async def finalize(self, state: AnalyticsState) -> dict:
        """Normalize the accumulated state into the public API response model."""
        response = self.build_response(state)
        return {
            "response": response,
            "failure_stage": response.failed_stage,
            "failure_reason": response.failure_reason,
            "warnings": response.warnings,
        }

    # ------------------------------------------------------------------
    # Response mapping
    # ------------------------------------------------------------------

    def _failure(self, state: AnalyticsState) -> tuple[str | None, str | None]:
        """Map the terminal state to a failed stage and human-readable reason."""
        if state.get("retrieval_failure"):
            return STAGE_RETRIEVAL, state["retrieval_failure"]

        if state.get("generation_error"):
            return STAGE_GENERATION, state["generation_error"]

        validation = state.get("validation_result")
        if validation is None or not validation.is_valid:
            violations = format_violations(validation) if validation is not None else []
            reason = (
                "; ".join(violations)
                or "The generated SQL failed all guardrail validation attempts."
            )
            return STAGE_GENERATION, reason

        execution = state.get("execution")
        if execution is None:
            return STAGE_GENERATION, "The pipeline stopped before executing the query."

        if not execution.is_success:
            if execution.status == ExecutionStatus.TIMEOUT:
                logger.warning("Execution timeout (code=%s)", execution.error_code)
            reason = (
                execution.error_message
                or f"Execution failed with status {execution.status}."
            )
            return STAGE_EXECUTION, reason

        return None, None

    def build_response(self, state: AnalyticsState) -> AnalyzeResponse:
        """Build the AnalyzeResponse consumed by the /analyze endpoint."""
        retrieval = state.get("retrieval")
        validation = state.get("validation_result")
        execution = state.get("execution")
        failure_stage, failure_reason = self._failure(state)
        success = (
            failure_stage is None
            and execution is not None
            and execution.status == ExecutionStatus.SUCCESS
        )

        warnings = list(state.get("warnings", []))
        if validation is not None:
            warnings.extend(validation.warnings)
        for repair in state.get("repair_history", []):
            if repair.stage == "execution" and repair.error_code:
                warnings.append(
                    f"SQL self-repair after execution error {repair.error_code}: "
                    f"{repair.error_message or 'no message'}"
                )

        return AnalyzeResponse(
            question=state.get("question", ""),
            success=success,
            failed_stage=None if success else failure_stage,
            failure_reason=None if success else failure_reason,
            retrieval_status=retrieval.status if retrieval is not None else RetrievalStatus.INSUFFICIENT_METADATA,
            confidence_score=retrieval.confidence_score if retrieval is not None else 0.0,
            final_sql=(
                validation.sanitized_sql
                if validation is not None and validation.is_valid
                else None
            ),
            raw_sql=state.get("sql_candidate") or None,
            explanation=None,
            repair_attempts=len(state.get("repair_history", [])),
            model_used=state.get("model_used") or self.default_model,
            warnings=warnings,
            execution=execution,
        )
