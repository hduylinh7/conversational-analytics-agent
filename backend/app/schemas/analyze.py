"""API request and response schemas for the conversational analyze endpoint."""

from pydantic import BaseModel, ConfigDict, Field

from app.repositories.schemas import ExecutionResult


class AnalyzeRequest(BaseModel):
    """Payload for asking a natural language analytics question."""

    question: str = Field(
        ...,
        min_length=3,
        max_length=2000,
        description="User's natural language analytics question.",
    )
    model: str | None = Field(
        default=None,
        description="Optional LLM model override (e.g. 'google/gemini-2.5-flash').",
    )
    temperature: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Sampling temperature for LLM SQL generation.",
    )


class AnalyzeResponse(BaseModel):
    """End-to-end result of the retrieval -> generation -> execution pipeline."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: str = Field(
        ...,
        description="Original user question.",
    )
    success: bool = Field(
        ...,
        description="True only if the query executed and returned results.",
    )
    failed_stage: str | None = Field(
        default=None,
        description="Stage where the pipeline stopped: 'retrieval', 'generation', or 'execution'.",
    )
    failure_reason: str | None = Field(
        default=None,
        description="Human-readable explanation of the pipeline failure.",
    )
    retrieval_status: str = Field(
        ...,
        description="Semantic retrieval status (SUCCESS, PARTIAL, INSUFFICIENT_METADATA, ...).",
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Overall retrieval confidence score.",
    )
    final_sql: str | None = Field(
        default=None,
        description="Sanitized SQL that was executed, or attempted before failing guardrails.",
    )
    raw_sql: str | None = Field(
        default=None,
        description="Raw SQL output produced by the LLM before sanitization.",
    )
    explanation: str | None = Field(
        default=None,
        description="Natural language explanation of the query or pipeline failure.",
    )
    repair_attempts: int = Field(
        default=0,
        ge=0,
        description="Number of LLM self-repair iterations used during generation.",
    )
    model_used: str | None = Field(
        default=None,
        description="LLM model identifier that produced the query.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Combined retrieval, guardrail, and execution warnings.",
    )
    execution: ExecutionResult | None = Field(
        default=None,
        description="Query execution report with rows, columns, and metrics if reached.",
    )
