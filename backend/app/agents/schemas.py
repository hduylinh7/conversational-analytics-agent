"""Pydantic schemas and DTOs for Text-to-SQL generation and self-repair loops."""

from pydantic import BaseModel, ConfigDict, Field

from app.security.schemas import SecurityViolation, ValidationResult
from app.semantic.retrieval.schemas import RetrievalResult


class SQLGenerationRequest(BaseModel):
    """Input payload to generate a SQL query from natural language and semantic retrieval context."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: str = Field(
        ...,
        description="User's original analytics question.",
    )
    retrieval_result: RetrievalResult = Field(
        ...,
        description="Retrieved semantic metadata containing metrics, dimensions, tables, and join rules.",
    )
    model: str | None = Field(
        default=None,
        description="Optional model override (e.g. 'google/gemini-2.5-flash').",
    )
    temperature: float = Field(
        default=0.0,
        description="Sampling temperature for LLM generation (0.0 for deterministic).",
        ge=0.0,
        le=1.0,
    )


class RepairStep(BaseModel):
    """Record of a single self-repair iteration."""

    attempt: int = Field(
        ...,
        description="1-based iteration index of the repair attempt.",
    )
    generated_sql: str = Field(
        ...,
        description="The SQL generated in this iteration that failed validation.",
    )
    violations: list[SecurityViolation] = Field(
        default_factory=list,
        description="Security and AST violations identified by the guardrail validator.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings produced during validation.",
    )
    feedback_prompt: str = Field(
        ...,
        description="The corrective feedback prompt sent to the LLM for this iteration.",
    )


class SQLGenerationResponse(BaseModel):
    """Output report containing generated SQL, AST validation status, and repair audit history."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    question: str = Field(
        ...,
        description="Original user question.",
    )
    final_sql: str | None = Field(
        default=None,
        description="The safe, validated, canonical SQL query ready for PostgreSQL execution (None if validation failed).",
    )
    raw_sql: str = Field(
        ...,
        description="The final raw SQL output string produced by the LLM.",
    )
    llm_response: str | None = Field(
        default=None,
        description="Full raw text response of the final LLM call, including any prose or comments.",
    )
    is_valid: bool = Field(
        ...,
        description="True if final_sql successfully passed all AST guardrails and policies.",
    )
    validation_result: ValidationResult | None = Field(
        default=None,
        description="Complete AST validation report from SQLGuardrailValidator.",
    )
    repair_attempts: int = Field(
        default=0,
        description="Number of self-repair feedback iterations executed.",
    )
    repair_history: list[RepairStep] = Field(
        default_factory=list,
        description="Chronological log of failed attempts and corrective prompts.",
    )
    explanation: str | None = Field(
        default=None,
        description="Natural language summary or reasoning of the generated query.",
    )
    model_used: str = Field(
        ...,
        description="Identifier of the model that generated the query.",
    )
    tokens_used: int | None = Field(
        default=None,
        description="Total token consumption if reported by LLM provider.",
    )

