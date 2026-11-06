"""Tests for the LangGraph analytics orchestration workflow.

Covers the graph topology end to end: retrieval gating, guardrail repair,
execution-error repair, budget exhaustion, and non-repairable failures.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest
from langgraph.graph.state import CompiledStateGraph

from app.agents.graph import AnalyticsNodes, AnalyticsState, build_analytics_graph
from app.agents.llm_client import LLMError, MockLLMClient
from app.agents.prompts import (
    describe_execution_error,
    render_execution_repair_feedback_prompt,
)
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.schemas.analyze import AnalyzeResponse
from app.security.schemas import ValidationResult
from app.semantic.retrieval.schemas import (
    RetrievalResult,
    RetrievalStatus,
    SelectedMetric,
)

SQLSTATE_UNDEFINED_COLUMN = "42703"

VALID_SQL = """SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
JOIN business.customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state
ORDER BY order_count DESC"""

BROKEN_COLUMN_SQL = """SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
JOIN business.customers c ON o.customer_id = c.customer_id
WHERE o.order_statuz = 'delivered'
GROUP BY c.customer_state
ORDER BY order_count DESC"""

HACK_SQL = "DROP TABLE business.orders"


def fenced(sql: str) -> str:
    """Wrap SQL in a ```sql fence exactly as LLM responses appear."""
    return f"```sql\n{sql}\n```"


def make_retrieval(with_metrics: bool = True) -> RetrievalResult:
    """Build a minimal RetrievalResult fixture with one matched metric."""
    metrics = (
        [
            SelectedMetric(
                name="order_count",
                description="Number of orders",
                expression="COUNT(DISTINCT o.order_id)",
                aggregation="COUNT",
                source_tables=["business.orders"],
                score=0.9,
            )
        ]
        if with_metrics
        else []
    )
    return RetrievalResult(
        question="test question",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.9,
        selected_metrics=metrics,
        selected_dimensions=[],
        is_safe_for_direct_execution=True,
    )


def make_execution(
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    error_code: str | None = None,
    error_message: str | None = None,
) -> ExecutionResult:
    """Build an ExecutionResult fixture."""
    return ExecutionResult(
        status=status,
        sql="SELECT 1",
        columns=["order_count"],
        rows=[[42]],
        row_count=1 if status == ExecutionStatus.SUCCESS else 0,
        total_rows_fetched=1 if status == ExecutionStatus.SUCCESS else 0,
        error_code=error_code,
        error_message=error_message,
    )


def valid_validation(sql: str = VALID_SQL) -> ValidationResult:
    """Build a passing ValidationResult fixture."""
    return ValidationResult(is_valid=True, original_sql=sql, sanitized_sql=sql)


class FakeRetriever:
    """Stub retriever returning a canned RetrievalResult."""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result

    def retrieve(self, question: str) -> RetrievalResult:
        return self.result


class ScriptedExecutor:
    """Async stub executor that maps input SQL to canned ExecutionResults."""

    def __init__(self, results_by_sql: dict[str, ExecutionResult]) -> None:
        self.results_by_sql = results_by_sql
        self.executed: list[str] = []

    async def __call__(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        return self.results_by_sql[sql]


class ValidatorStub:
    """Guardrail validator stub that delegates to a canned decision function."""

    def __init__(self, decide: Callable[[str], ValidationResult]) -> None:
        self._decide = decide
        self.calls: list[str] = []

    def validate(self, sql: str) -> ValidationResult:
        self.calls.append(sql)
        return self._decide(sql)


class FailingLLM(MockLLMClient):
    """LLM stub that always raises a provider error."""

    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("provider down")

    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("OpenRouter API error (404): model `string` not found")


def build_graph(
    *,
    retrieval: RetrievalResult,
    llm: MockLLMClient,
    validator: ValidatorStub,
    executor: ScriptedExecutor,
    max_guardrail_repairs: int = 3,
    max_execution_repairs: int = 2,
) -> CompiledStateGraph:
    """Compile a graph with injected fakes and explicit repair budgets."""
    nodes = AnalyticsNodes(
        retriever=FakeRetriever(retrieval),
        llm_client=llm,
        executor=executor,
        validator=validator,
        max_guardrail_repairs=max_guardrail_repairs,
        max_execution_repairs=max_execution_repairs,
        default_model="mock-model",
    )
    return build_analytics_graph(nodes)


async def run_graph(**kwargs: Any) -> tuple[AnalyticsState, AnalyzeResponse]:
    """Invoke a compiled test graph and return (final_state, AnalyzeResponse)."""
    graph = build_graph(**kwargs)
    state = cast(
        AnalyticsState,
        await graph.ainvoke({"question": "test question", "temperature": 0.0}),
    )
    response = state.get("response")
    assert response is not None
    return state, response


# ==============================================================================
# PROMPT RENDERING
# ==============================================================================


def test_execution_repair_prompt_includes_sqlstate_and_hint() -> None:
    """Verify the execution repair prompt carries SQLSTATE, message, and guidance."""
    prompt = render_execution_repair_feedback_prompt(
        generated_sql="SELECT bad_column FROM business.orders",
        error_code=SQLSTATE_UNDEFINED_COLUMN,
        error_message='column "bad_column" does not exist',
        attempt=1,
        max_attempts=2,
    )

    assert "FAILED while executing against PostgreSQL" in prompt
    assert "42703" in prompt
    assert 'column "bad_column" does not exist' in prompt
    assert "does not exist on the table" in prompt
    assert "repair attempt 1/2" in prompt


def test_describe_execution_error_known_and_unknown_codes() -> None:
    """Verify known SQLSTATEs map to hints and unknown codes fall back gracefully."""
    assert describe_execution_error("42P01") != describe_execution_error("42703")
    assert "Inspect" in describe_execution_error("ZZZZZ")
    assert "Inspect" in describe_execution_error(None)


# ==============================================================================
# GRAPH HAPPY PATH
# ==============================================================================


@pytest.mark.asyncio
async def test_full_success_no_repair() -> None:
    """A valid first attempt executes once with zero repair steps."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    validator = ValidatorStub(lambda sql: valid_validation(sql))
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=1,
        max_execution_repairs=1,
    )

    assert response.success is True
    assert response.failed_stage is None
    assert response.repair_attempts == 0
    assert response.final_sql == VALID_SQL
    assert response.execution is not None
    assert response.execution.is_success is True
    assert executor.executed == [VALID_SQL]
    assert validator.calls == [VALID_SQL]
    assert len(llm.call_history) == 1


# ==============================================================================
# EXECUTION REPAIR LOOP
# ==============================================================================


@pytest.mark.asyncio
async def test_execution_error_triggers_repair_then_success() -> None:
    """A repairable SQLSTATE feeds back to the LLM and the fixed query succeeds."""
    llm = MockLLMClient(responses=[fenced(BROKEN_COLUMN_SQL), fenced(VALID_SQL)])
    validator = ValidatorStub(lambda sql: valid_validation(sql))
    executor = ScriptedExecutor(
        {
            BROKEN_COLUMN_SQL: make_execution(
                status=ExecutionStatus.DATABASE_ERROR,
                error_code=SQLSTATE_UNDEFINED_COLUMN,
                error_message='column "o.order_statuz" does not exist',
            ),
            VALID_SQL: make_execution(),
        }
    )

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=3,
        max_execution_repairs=2,
    )

    assert response.success is True
    assert response.repair_attempts == 1
    assert response.execution is not None
    assert response.execution.is_success is True
    assert executor.executed == [BROKEN_COLUMN_SQL, VALID_SQL]
    assert any("42703" in warning for warning in response.warnings)

    # The repaired SQL is always re-validated before touching the executor.
    assert validator.calls == [BROKEN_COLUMN_SQL, VALID_SQL]

    # Same conversation: second LLM call contains the assistant attempt + feedback.
    assert len(llm.call_history) == 2
    second_call = llm.call_history[1]
    assert second_call[-2]["role"] == "assistant"
    assert BROKEN_COLUMN_SQL in second_call[-2]["content"]
    assert second_call[-1]["role"] == "user"
    assert "FAILED while executing against PostgreSQL" in second_call[-1]["content"]


@pytest.mark.asyncio
async def test_execution_repair_budget_exhaustion() -> None:
    """Persistent runtime errors stop after the execution repair budget."""
    llm = MockLLMClient(
        responses=[fenced(BROKEN_COLUMN_SQL)] * 10
    )
    validator = ValidatorStub(lambda sql: valid_validation(sql))
    failure = make_execution(
        status=ExecutionStatus.DATABASE_ERROR,
        error_code=SQLSTATE_UNDEFINED_COLUMN,
        error_message="column does not exist",
    )
    executor = ScriptedExecutor({BROKEN_COLUMN_SQL: failure})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=5,
        max_execution_repairs=2,
    )

    assert response.success is False
    assert response.failed_stage == "execution"
    assert response.repair_attempts == 2
    assert response.execution is failure
    assert executor.executed == [BROKEN_COLUMN_SQL] * 3
    assert len(llm.call_history) == 3


@pytest.mark.asyncio
async def test_non_repairable_execution_stops_immediately() -> None:
    """CANCELLED results never enter the repair loop."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    validator = ValidatorStub(lambda sql: valid_validation())
    cancelled = make_execution(status=ExecutionStatus.CANCELLED)
    executor = ScriptedExecutor({VALID_SQL: cancelled})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=3,
        max_execution_repairs=2,
    )

    assert response.success is False
    assert response.failed_stage == "execution"
    assert response.repair_attempts == 0
    assert response.execution is cancelled
    assert len(llm.call_history) == 1


# ==============================================================================
# GUARDRAIL REPAIR LOOP
# ==============================================================================


@pytest.mark.asyncio
async def test_guardrail_repair_runs_before_execution() -> None:
    """A first invalid attempt is repaired without ever invoking the executor."""
    llm = MockLLMClient(responses=[HACK_SQL, fenced(VALID_SQL)])

    def decide(sql: str) -> ValidationResult:
        if sql == HACK_SQL:
            return ValidationResult(is_valid=False, original_sql=sql)
        return valid_validation(sql)

    validator = ValidatorStub(decide)
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=1,
        max_execution_repairs=1,
    )

    assert response.success is True
    assert response.repair_attempts == 1
    assert executor.executed == [VALID_SQL]
    assert HACK_SQL not in executor.executed
    # Invalid SQL is rejected by guardrails and never executed.
    assert validator.calls == [HACK_SQL, VALID_SQL]


@pytest.mark.asyncio
async def test_guardrail_repair_budget_exhaustion() -> None:
    """Persistent guardrail violations stop at the generation stage."""
    llm = MockLLMClient(responses=[HACK_SQL] * 10)
    validator = ValidatorStub(
        lambda sql: ValidationResult(is_valid=False, original_sql=sql)
    )
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
        max_guardrail_repairs=2,
        max_execution_repairs=2,
    )

    assert response.success is False
    assert response.failed_stage == "generation"
    assert response.final_sql is None
    assert response.repair_attempts == 2
    assert response.raw_sql == HACK_SQL
    assert executor.executed == []
    assert validator.calls == [HACK_SQL] * 3
    assert len(llm.call_history) == 3


# ==============================================================================
# RETRIEVAL GATE AND PROVIDER ERRORS
# ==============================================================================


@pytest.mark.asyncio
async def test_non_analytics_question_short_circuits_without_llm() -> None:
    """Non-analytics questions finalize at retrieval with no LLM or executor calls."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    validator = ValidatorStub(lambda sql: valid_validation())
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    _, response = await run_graph(
        retrieval=make_retrieval(with_metrics=False),
        llm=llm,
        validator=validator,
        executor=executor,
    )

    assert response.success is False
    assert response.failed_stage == "retrieval"
    assert response.execution is None
    assert llm.call_history == []
    assert validator.calls == []
    assert executor.executed == []


@pytest.mark.asyncio
async def test_llm_provider_error_finalizes_at_generation() -> None:
    """An LLMError is captured by the generate node and finalized gracefully."""
    llm = FailingLLM(responses=[])
    validator = ValidatorStub(lambda sql: valid_validation())
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    _, response = await run_graph(
        retrieval=make_retrieval(),
        llm=llm,
        validator=validator,
        executor=executor,
    )

    assert response.success is False
    assert response.failed_stage == "generation"
    assert "LLM provider error" in (response.failure_reason or "")
    assert response.execution is None
    assert executor.executed == []
