"""Tests for the execution-stage SQL self-repair loop (SQLSTATE feedback to the LLM)."""

import pytest

from app.agents.llm_client import MockLLMClient
from app.agents.prompts import (
    describe_execution_error,
    render_execution_repair_feedback_prompt,
)
from app.agents.schemas import SQLGenerationRequest
from app.agents.sql_generator import SQLGenerator
from app.repositories.schemas import ExecutionResult, ExecutionStatus
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


def make_retrieval() -> RetrievalResult:
    """Build a minimal RetrievalResult fixture with one matched metric."""
    return RetrievalResult(
        question="test question",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.9,
        selected_metrics=[
            SelectedMetric(
                name="order_count",
                description="Number of orders",
                expression="COUNT(DISTINCT o.order_id)",
                aggregation="COUNT",
                source_tables=["business.orders"],
                score=0.9,
            )
        ],
        selected_dimensions=[],
        is_safe_for_direct_execution=True,
    )


def make_request() -> SQLGenerationRequest:
    """Build a SQLGenerationRequest fixture."""
    return SQLGenerationRequest(question="test question", retrieval_result=make_retrieval())


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


class ScriptedExecutor:
    """Async stub executor that maps input SQL to canned ExecutionResults in order."""

    def __init__(self, results_by_sql: dict[str, ExecutionResult]) -> None:
        self.results_by_sql = results_by_sql
        self.executed: list[str] = []

    async def __call__(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        return self.results_by_sql[sql]


class _ValidatorStub:
    """Guardrail validator stub that delegates to a canned decision function."""

    def __init__(self, decide) -> None:
        self._decide = decide

    def validate(self, sql: str) -> ValidationResult:
        return self._decide(sql)


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
# GENERATOR + EXECUTOR LOOP
# ==============================================================================


@pytest.mark.asyncio
async def test_full_success_no_repair() -> None:
    """A valid first attempt executes once with zero repair steps."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    generator = SQLGenerator(
        llm_client=llm,
        validator=_ValidatorStub(lambda sql: valid_validation()),
        max_repair_attempts=1,
        max_execution_repair_attempts=1,
    )
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    generation, execution = await generator.generate_and_execute_async(
        make_request(), executor=executor
    )

    assert generation.is_valid is True
    assert generation.repair_attempts == 0
    assert generation.repair_history == []
    assert execution is not None
    assert execution.is_success is True
    assert executor.executed == [VALID_SQL]
    assert len(llm.call_history) == 1


@pytest.mark.asyncio
async def test_execution_error_triggers_repair_then_success() -> None:
    """A repairable SQLSTATE feeds back to the LLM and the fixed query succeeds."""
    llm = MockLLMClient(responses=[fenced(BROKEN_COLUMN_SQL), fenced(VALID_SQL)])
    generator = SQLGenerator(
        llm_client=llm,
        validator=_ValidatorStub(lambda sql: valid_validation(sql)),
        max_repair_attempts=3,
        max_execution_repair_attempts=2,
    )
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

    generation, execution = await generator.generate_and_execute_async(
        make_request(), executor=executor
    )

    assert generation.is_valid is True
    assert generation.repair_attempts == 1
    assert len(generation.repair_history) == 1
    repair_step = generation.repair_history[0]
    assert repair_step.stage == "execution"
    assert repair_step.error_code == SQLSTATE_UNDEFINED_COLUMN
    assert repair_step.feedback_prompt is not None
    assert "42703" in repair_step.feedback_prompt
    assert execution is not None
    assert execution.is_success is True
    assert executor.executed == [BROKEN_COLUMN_SQL, VALID_SQL]

    # Same conversation: second LLM call contains assistant attempt + repair prompt.
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
        responses=[fenced(BROKEN_COLUMN_SQL), fenced(BROKEN_COLUMN_SQL), fenced(BROKEN_COLUMN_SQL)]
    )
    generator = SQLGenerator(
        llm_client=llm,
        validator=_ValidatorStub(lambda sql: valid_validation(sql)),
        max_repair_attempts=5,
        max_execution_repair_attempts=2,
    )
    failure = make_execution(
        status=ExecutionStatus.DATABASE_ERROR,
        error_code=SQLSTATE_UNDEFINED_COLUMN,
        error_message="column does not exist",
    )
    executor = ScriptedExecutor({BROKEN_COLUMN_SQL: failure})

    generation, execution = await generator.generate_and_execute_async(
        make_request(), executor=executor
    )

    assert generation.is_valid is True
    assert generation.repair_attempts == 2
    assert all(step.stage == "execution" for step in generation.repair_history)
    assert execution is failure
    assert executor.executed == [BROKEN_COLUMN_SQL, BROKEN_COLUMN_SQL, BROKEN_COLUMN_SQL]
    assert len(llm.call_history) == 3


@pytest.mark.asyncio
async def test_non_repairable_execution_stops_immediately() -> None:
    """CANCELLED results never enter the repair loop."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    generator = SQLGenerator(
        llm_client=llm,
        validator=_ValidatorStub(lambda sql: valid_validation()),
        max_repair_attempts=3,
        max_execution_repair_attempts=2,
    )
    cancelled = make_execution(status=ExecutionStatus.CANCELLED)
    executor = ScriptedExecutor({VALID_SQL: cancelled})

    generation, execution = await generator.generate_and_execute_async(
        make_request(), executor=executor
    )

    assert execution is cancelled
    assert generation.repair_attempts == 0
    assert len(llm.call_history) == 1


@pytest.mark.asyncio
async def test_guardrail_repair_runs_before_execution() -> None:
    """A first invalid attempt is repaired without ever invoking the executor."""
    llm = MockLLMClient(responses=[HACK_SQL, fenced(VALID_SQL)])
    validator_calls: list[str] = []

    def decide(sql: str) -> ValidationResult:
        validator_calls.append(sql)
        if sql == HACK_SQL:
            return ValidationResult(is_valid=False, original_sql=sql)
        return valid_validation(sql)

    generator = SQLGenerator(
        llm_client=llm,
        validator=_ValidatorStub(decide),
        max_repair_attempts=1,
        max_execution_repair_attempts=1,
    )
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    generation, execution = await generator.generate_and_execute_async(
        make_request(), executor=executor
    )

    assert generation.is_valid is True
    assert generation.repair_attempts == 1
    assert generation.repair_history[0].stage == "guardrail"
    assert executor.executed == [VALID_SQL]
    assert HACK_SQL not in executor.executed
    assert execution is not None
    assert execution.is_success is True


@pytest.mark.asyncio
async def test_skip_response_returns_no_execution() -> None:
    """Non-analytics questions short-circuit with no executor call."""
    empty_retrieval = RetrievalResult(
        question="hello",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.1,
    )
    request = SQLGenerationRequest(question="hello", retrieval_result=empty_retrieval)
    generator = SQLGenerator(
        llm_client=MockLLMClient(responses=[fenced(VALID_SQL)]),
        validator=_ValidatorStub(lambda sql: valid_validation()),
        max_repair_attempts=1,
        max_execution_repair_attempts=1,
    )
    executor = ScriptedExecutor({VALID_SQL: make_execution()})

    generation, execution = await generator.generate_and_execute_async(request, executor=executor)

    assert generation.is_valid is False
    assert generation.final_sql is None
    assert execution is None
    assert executor.executed == []
    assert len(generator.llm_client.call_history) == 0
