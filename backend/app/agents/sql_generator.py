"""Text-to-SQL generation agent with guardrail validation and self-repair loop."""

import re
from collections.abc import Awaitable, Callable
from typing import Any

from app.agents.llm_client import BaseLLMClient, create_llm_client
from app.agents.prompts import (
    build_initial_messages,
    render_execution_repair_feedback_prompt,
    render_repair_feedback_prompt,
)
from app.agents.schemas import RepairStep, SQLGenerationRequest, SQLGenerationResponse
from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.schemas import ExecutionResult
from app.security.schemas import ValidationResult
from app.security.validator import SQLGuardrailValidator, validate_sql

logger = get_logger(__name__)

FENCED_SQL_PATTERN = re.compile(r"```(?:sql)?\s*(.*?)\s*```", flags=re.DOTALL | re.IGNORECASE)


def extract_sql(response_text: str) -> str | None:
    """Extract candidate SQL from a raw LLM response (prefers the last fenced block)."""
    text = response_text.strip()
    if not text:
        return None

    blocks = FENCED_SQL_PATTERN.findall(text)
    if blocks:
        return blocks[-1].strip() or None

    return text.strip() or None


def format_violations(result: ValidationResult) -> list[str]:
    """Human-readable violation lines for feedback prompts and repair history."""
    return [
        f"[{violation.severity}] {violation.rule}: {violation.message}"
        for violation in result.violations
    ]


class SQLGenerator:
    """Generates SQL from a question using semantic retrieval context and guardrails.

    Pipeline: prompt rendering -> LLM generation -> SQL extraction ->
    guardrail validation -> self-repair loop (feedback with violations) ->
    SQLGenerationResponse.

    `generate_and_execute_async` additionally executes the validated SQL and
    feeds runtime PostgreSQL failures back into the same repair conversation.
    """

    def __init__(
        self,
        llm_client: BaseLLMClient | None = None,
        validator: SQLGuardrailValidator | None = None,
        max_repair_attempts: int | None = None,
        max_execution_repair_attempts: int | None = None,
    ) -> None:
        """Initialize generator with LLM client, guardrail validator, and repair budgets."""
        settings = get_settings()
        self.llm_client = llm_client or create_llm_client()
        self._validator = validator
        self.max_repair_attempts = (
            max_repair_attempts
            if max_repair_attempts is not None
            else settings.SQL_MAX_REPAIR_ATTEMPTS
        )
        self.max_execution_repair_attempts = (
            max_execution_repair_attempts
            if max_execution_repair_attempts is not None
            else settings.SQL_MAX_EXECUTION_REPAIR_ATTEMPTS
        )
        self.validate = validator.validate if validator is not None else validate_sql

    def _build_llm_kwargs(self, request: SQLGenerationRequest) -> dict[str, Any]:
        """Request-scoped LLM call parameters (temperature and optional model override)."""
        kwargs: dict[str, Any] = {"temperature": request.temperature}
        if request.model:
            kwargs["model"] = request.model
        return kwargs

    def _build_response(
        self,
        request: SQLGenerationRequest,
        raw_sql: str,
        response_text: str,
        validation_result: ValidationResult,
        repair_history: list[RepairStep],
        explanation: str | None = None,
    ) -> SQLGenerationResponse:
        """Assemble the final response from the accumulated pipeline state."""
        settings = get_settings()
        return SQLGenerationResponse(
            question=request.question,
            final_sql=(validation_result.sanitized_sql if validation_result.is_valid else None),
            raw_sql=raw_sql,
            llm_response=response_text,
            is_valid=validation_result.is_valid,
            validation_result=validation_result,
            repair_attempts=len(repair_history),
            repair_history=repair_history,
            model_used=request.model or settings.LLM_MODEL,
            explanation=explanation,
        )

    def _skip_reason(self, request: SQLGenerationRequest) -> str | None:
        """Non-analytics guard: no matched metric or dimension means generation is skipped."""
        retrieval = request.retrieval_result
        if not retrieval.selected_metrics and not retrieval.selected_dimensions:
            return (
                "The question does not match any analytics metrics or dimensions in the "
                "semantic catalog; SQL generation skipped (likely not an analytics question)."
            )
        return None

    def _skip_response(
        self,
        request: SQLGenerationRequest,
    ) -> SQLGenerationResponse:
        """Deterministic refusal response without any LLM call."""
        return self._build_response(
            request,
            raw_sql="",
            response_text="",
            validation_result=ValidationResult(is_valid=False, original_sql=""),
            repair_history=[],
            explanation=self._skip_reason(request),
        )

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        """Generate and repair a SQL query synchronously."""
        if self._skip_reason(request):
            return self._skip_response(request)
        messages = build_initial_messages(request)
        llm_kwargs = self._build_llm_kwargs(request)
        repair_history: list[RepairStep] = []

        while True:
            response_text = self.llm_client.generate(
                [dict(message) for message in messages], **llm_kwargs
            )
            raw_sql = extract_sql(response_text) or ""
            validation_result = self.validate(raw_sql)

            if validation_result.is_valid:
                return self._build_response(
                    request, raw_sql, response_text, validation_result, repair_history
                )

            if len(repair_history) >= self.max_repair_attempts:
                return self._build_response(
                    request, raw_sql, response_text, validation_result, repair_history
                )

            attempt = len(repair_history) + 1
            feedback_prompt = render_repair_feedback_prompt(
                generated_sql=raw_sql,
                violations=format_violations(validation_result),
                warnings=validation_result.warnings,
                attempt=attempt,
                max_attempts=self.max_repair_attempts,
            )
            repair_history.append(
                RepairStep(
                    attempt=attempt,
                    generated_sql=raw_sql,
                    violations=validation_result.violations,
                    warnings=validation_result.warnings,
                    feedback_prompt=feedback_prompt,
                )
            )

            logger.warning(
                "SQL validation failed (attempt %d/%d), entering repair loop",
                attempt,
                self.max_repair_attempts,
            )
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": feedback_prompt})

    async def generate_and_execute_async(
        self,
        request: SQLGenerationRequest,
        executor: Callable[[str], Awaitable[ExecutionResult]],
    ) -> tuple[SQLGenerationResponse, ExecutionResult | None]:
        """Generate, validate, execute, and self-repair across both failure surfaces.

        State machine per iteration:
        1. LLM produces SQL -> guardrail validation.
           - Invalid: feed guardrail violations back to the LLM (budget:
             `max_repair_attempts`). No execution is attempted.
        2. Valid SQL -> `executor(sanitized_sql)` runs against PostgreSQL.
           - Success or non-repairable failure: stop and return the result.
           - Repairable failure (DATABASE_ERROR, TIMEOUT, VALIDATION_FAILED):
             feed the SQLSTATE error plus a human hint back to the LLM within
             the same conversation (budget: `max_execution_repair_attempts`).

        Both repair kinds share one chat history, so the LLM always sees its own
        previous attempts. Guardrail budget counts all recorded repair steps,
        capping the combined loop.

        Args:
            request: Generation request with question and retrieval context.
            executor: Async callable that executes sanitized SQL, returning an
                ExecutionResult (typically AnalyticsRepository.execute).

        Returns:
            Tuple of the generation report (audit history included) and the
            last ExecutionResult, or None when no execution was reached.
        """
        if self._skip_reason(request):
            return self._skip_response(request), None

        messages = build_initial_messages(request)
        llm_kwargs = self._build_llm_kwargs(request)
        repair_history: list[RepairStep] = []
        execution_repairs = 0

        while True:
            response_text = await self.llm_client.generate_async(
                [dict(message) for message in messages], **llm_kwargs
            )
            raw_sql = extract_sql(response_text) or ""
            validation_result = self.validate(raw_sql)

            if not validation_result.is_valid:
                if len(repair_history) >= self.max_repair_attempts:
                    return (
                        self._build_response(
                            request, raw_sql, response_text, validation_result, repair_history
                        ),
                        None,
                    )

                attempt = len(repair_history) + 1
                feedback_prompt = render_repair_feedback_prompt(
                    generated_sql=raw_sql,
                    violations=format_violations(validation_result),
                    warnings=validation_result.warnings,
                    attempt=attempt,
                    max_attempts=self.max_repair_attempts,
                )
                repair_history.append(
                    RepairStep(
                        attempt=attempt,
                        stage="guardrail",
                        generated_sql=raw_sql,
                        violations=validation_result.violations,
                        warnings=validation_result.warnings,
                        feedback_prompt=feedback_prompt,
                    )
                )
                logger.warning(
                    "SQL validation failed (attempt %d/%d), entering repair loop",
                    attempt,
                    self.max_repair_attempts,
                )
                messages.append({"role": "assistant", "content": response_text})
                messages.append({"role": "user", "content": feedback_prompt})
                continue

            execution = await executor(validation_result.sanitized_sql or raw_sql)

            if execution.is_success or not execution.is_repairable:
                return (
                    self._build_response(
                        request, raw_sql, response_text, validation_result, repair_history
                    ),
                    execution,
                )

            if execution_repairs >= self.max_execution_repair_attempts:
                logger.warning(
                    "Execution repair budget exhausted (%d/%d) with code=%s",
                    execution_repairs,
                    self.max_execution_repair_attempts,
                    execution.error_code,
                )
                return (
                    self._build_response(
                        request, raw_sql, response_text, validation_result, repair_history
                    ),
                    execution,
                )

            execution_repairs += 1
            attempt = len(repair_history) + 1
            feedback_prompt = render_execution_repair_feedback_prompt(
                generated_sql=raw_sql,
                error_code=execution.error_code,
                error_message=execution.error_message,
                attempt=attempt,
                max_attempts=self.max_execution_repair_attempts,
            )
            repair_history.append(
                RepairStep(
                    attempt=attempt,
                    stage="execution",
                    generated_sql=raw_sql,
                    feedback_prompt=feedback_prompt,
                    error_code=execution.error_code,
                    error_message=execution.error_message,
                )
            )
            logger.warning(
                "SQL execution failed (repair attempt %d/%d, SQLSTATE=%s), entering repair loop",
                attempt,
                self.max_execution_repair_attempts,
                execution.error_code,
            )
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": feedback_prompt})

    async def generate_async(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        """Generate and repair a SQL query asynchronously."""
        if self._skip_reason(request):
            return self._skip_response(request)

        messages = build_initial_messages(request)
        llm_kwargs = self._build_llm_kwargs(request)
        repair_history: list[RepairStep] = []

        while True:
            response_text = await self.llm_client.generate_async(
                [dict(message) for message in messages], **llm_kwargs
            )
            raw_sql = extract_sql(response_text) or ""
            validation_result = self.validate(raw_sql)

            if validation_result.is_valid:
                return self._build_response(
                    request, raw_sql, response_text, validation_result, repair_history
                )

            if len(repair_history) >= self.max_repair_attempts:
                return self._build_response(
                    request, raw_sql, response_text, validation_result, repair_history
                )

            attempt = len(repair_history) + 1
            feedback_prompt = render_repair_feedback_prompt(
                generated_sql=raw_sql,
                violations=format_violations(validation_result),
                warnings=validation_result.warnings,
                attempt=attempt,
                max_attempts=self.max_repair_attempts,
            )
            repair_history.append(
                RepairStep(
                    attempt=attempt,
                    generated_sql=raw_sql,
                    violations=validation_result.violations,
                    warnings=validation_result.warnings,
                    feedback_prompt=feedback_prompt,
                )
            )

            logger.warning(
                "SQL validation failed (attempt %d/%d), entering repair loop",
                attempt,
                self.max_repair_attempts,
            )
            messages.append({"role": "assistant", "content": response_text})
            messages.append({"role": "user", "content": feedback_prompt})
