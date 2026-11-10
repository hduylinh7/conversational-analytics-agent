"""Shared state definition for the LangGraph analytics orchestration workflow.

The graph owns the control flow (retrieval -> generation -> validation ->
execution -> repair loops), while this state carries every artifact produced
along the way. Nodes return partial updates that LangGraph merges into the
running state.
"""

from typing import Literal, TypedDict

from app.agents.schemas import RepairStep
from app.repositories.schemas import ExecutionResult
from app.schemas.analyze import AnalyzeResponse
from app.security.schemas import ValidationResult
from app.semantic.retrieval.schemas import RetrievalResult

FailureKind = Literal["guardrail", "execution"]


class AnalyticsState(TypedDict, total=False):
    """Mutable analytics pipeline state passed between LangGraph nodes."""

    # Input
    question: str
    model: str | None
    temperature: float

    # Retrieval
    retrieval: RetrievalResult | None
    retrieval_failure: str | None

    # Generation conversation (shared by guardrail and execution repair loops)
    messages: list[dict[str, str]]
    llm_response: str
    sql_candidate: str
    model_used: str
    generation_error: str | None

    # Guardrail validation and execution
    validation_result: ValidationResult | None
    execution: ExecutionResult | None

    # Caching (prompt digest resolved in the generate node; committed on validation)
    prompt_cache_key: str
    cache_hit: bool
    is_first_turn: bool
    semantic_cache_vector: list[float]

    # Repair accounting
    repair_history: list[RepairStep]
    guardrail_repairs: int
    execution_repairs: int
    last_failure: FailureKind | None

    # Finalization
    failure_stage: str | None
    failure_reason: str | None
    warnings: list[str]
    response: AnalyzeResponse | None
