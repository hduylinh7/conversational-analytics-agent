"""Tests for AnalyzeService LangGraph orchestration and the POST /analyze endpoint."""

from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from app.agents.graph.nodes import ValidatorLike
from app.agents.llm_client import BaseLLMClient, LLMError, MockLLMClient
from app.main import app
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.schemas.analyze import AnalyzeResponse
from app.security.schemas import ValidationResult
from app.semantic.retrieval.schemas import (
    RetrievalResult,
    RetrievalStatus,
    SelectedMetric,
)
from app.services.analyze_service import AnalyzeService, get_analyze_service

VALID_SQL = "SELECT SUM(p.payment_value) FROM business.payments p"


def fenced(sql: str) -> str:
    """Wrap SQL in a ```sql fence exactly as LLM responses appear."""
    return f"```sql\n{sql}\n```"


def make_retrieval(
    with_metrics: bool = True,
    status: RetrievalStatus = RetrievalStatus.SUCCESS,
    unsafe_join: bool = False,
) -> RetrievalResult:
    """Build a RetrievalResult fixture."""
    metrics = (
        [
            SelectedMetric(
                name="revenue",
                description="Total revenue",
                expression="SUM(p.payment_value)",
                aggregation="SUM",
                source_tables=["business.payments"],
                score=0.92,
            )
        ]
        if with_metrics
        else []
    )
    return RetrievalResult(
        question="test question",
        status=status,
        confidence_score=0.85,
        selected_metrics=metrics,
        selected_dimensions=[],
        is_safe_for_direct_execution=not unsafe_join,
    )


def make_execution(
    status: ExecutionStatus = ExecutionStatus.SUCCESS,
    rows: list | None = None,
) -> ExecutionResult:
    """Build an ExecutionResult fixture."""
    if rows is None:
        rows = [[103_555.12]]
    return ExecutionResult(
        status=status,
        sql="SELECT 1",
        columns=["revenue"],
        rows=rows,
        row_count=len(rows),
        total_rows_fetched=len(rows),
        truncated=False,
        error_code=None if status == ExecutionStatus.SUCCESS else "X0000",
        error_message=None if status == ExecutionStatus.SUCCESS else "failed on purpose",
    )


class FakeRetriever:
    """Stub retriever returning a canned RetrievalResult."""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result

    def retrieve(self, question: str) -> RetrievalResult:
        return self.result


class FakeRepository:
    """Stub executor returning a canned ExecutionResult and recording input SQL."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def execute(self, sql: str, **kwargs: object) -> ExecutionResult:
        self.calls.append(sql)
        return self.result


class ValidatorStub:
    """Guardrail validator stub that delegates to a canned decision function."""

    def __init__(self, decide: Callable[[str], ValidationResult]) -> None:
        self._decide = decide
        self.calls: list[str] = []

    def validate(self, sql: str) -> ValidationResult:
        self.calls.append(sql)
        return self._decide(sql)


class AlwaysValidValidator:
    """Validator stub that accepts every candidate and returns it unchanged."""

    def validate(self, sql: str) -> ValidationResult:
        return ValidationResult(is_valid=True, original_sql=sql, sanitized_sql=sql)


class RejectingValidator:
    """Validator stub that rejects every candidate."""

    def validate(self, sql: str) -> ValidationResult:
        return ValidationResult(is_valid=False, original_sql=sql)


class FailingLLM(BaseLLMClient):
    """LLM stub that always raises a provider error."""

    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("provider down")

    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("OpenRouter API error (404): model `string` not found")


def make_service(
    *,
    retrieval: RetrievalResult,
    llm: BaseLLMClient,
    repository: FakeRepository,
    validator: ValidatorLike | None = None,
    max_guardrail_repairs: int = 3,
    max_execution_repairs: int = 2,
) -> AnalyzeService:
    """Build an AnalyzeService wired with fake components."""
    return AnalyzeService(
        retriever=FakeRetriever(retrieval),
        llm_client=llm,
        validator=validator or AlwaysValidValidator(),
        repository=repository,
        max_guardrail_repairs=max_guardrail_repairs,
        max_execution_repairs=max_execution_repairs,
    )


@pytest.mark.asyncio
async def test_service_full_success_pipeline() -> None:
    """Verify the graph passes sanitized SQL from generation to the repository."""
    execution = make_execution()
    repository = FakeRepository(execution)
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    service = make_service(retrieval=make_retrieval(), llm=llm, repository=repository)

    response = await service.analyze("what is total revenue?")

    assert response.success is True
    assert response.failed_stage is None
    assert response.failure_reason is None
    assert response.final_sql == VALID_SQL
    assert response.retrieval_status == RetrievalStatus.SUCCESS
    assert response.execution is execution
    assert repository.calls == [VALID_SQL]
    assert len(llm.call_history) == 1


@pytest.mark.asyncio
async def test_service_fails_at_retrieval_without_metrics() -> None:
    """Verify no LLM call occurs when retrieval matches nothing."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    repository = FakeRepository(make_execution())
    service = make_service(
        retrieval=make_retrieval(with_metrics=False), llm=llm, repository=repository
    )

    response = await service.analyze("hello weather today?")

    assert response.success is False
    assert response.failed_stage == "retrieval"
    assert llm.call_history == []
    assert repository.calls == []
    assert response.execution is None


@pytest.mark.asyncio
async def test_service_fails_at_retrieval_on_unsafe_join() -> None:
    """Verify unsafe join plans stop the pipeline before generation."""
    llm = MockLLMClient(responses=[fenced(VALID_SQL)])
    service = make_service(
        retrieval=make_retrieval(unsafe_join=True),
        llm=llm,
        repository=FakeRepository(make_execution()),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "retrieval"
    assert "unsafe" in (response.failure_reason or "")
    assert llm.call_history == []


@pytest.mark.asyncio
async def test_service_fails_at_generation_on_guardrail_exhaustion() -> None:
    """Verify exhausted guardrail repair attempts stop before execution."""
    repository = FakeRepository(make_execution())
    llm = MockLLMClient(responses=[fenced("DROP TABLE business.orders")] * 10)
    service = make_service(
        retrieval=make_retrieval(),
        llm=llm,
        repository=repository,
        validator=RejectingValidator(),
        max_guardrail_repairs=2,
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "generation"
    assert response.execution is None
    assert response.raw_sql == "DROP TABLE business.orders"
    assert response.repair_attempts == 2
    assert repository.calls == []


@pytest.mark.asyncio
async def test_service_fails_at_generation_on_llm_provider_error() -> None:
    """Verify an LLMError is mapped to a graceful generation-stage failure."""
    service = make_service(
        retrieval=make_retrieval(),
        llm=FailingLLM(),
        repository=FakeRepository(make_execution()),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "generation"
    assert "LLM provider error" in (response.failure_reason or "")
    assert response.final_sql is None
    assert response.execution is None
    assert response.retrieval_status == RetrievalStatus.SUCCESS


@pytest.mark.asyncio
async def test_service_fails_at_execution_on_non_repairable_error() -> None:
    """Verify non-repairable execution failures surface as failed_stage='execution'."""
    failure = make_execution(status=ExecutionStatus.CANCELLED)
    service = make_service(
        retrieval=make_retrieval(),
        llm=MockLLMClient(responses=[fenced(VALID_SQL)]),
        repository=FakeRepository(failure),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "execution"
    assert response.failure_reason is not None
    assert "failed on purpose" in response.failure_reason
    assert response.execution is failure


@pytest.mark.asyncio
async def test_service_truncation_and_warning_pass_through() -> None:
    """Verify execution truncation flag and retrieval warnings reach the response."""
    execution = ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        sql="SELECT 1",
        columns=["revenue"],
        rows=[[1.0]],
        row_count=1,
        total_rows_fetched=2,
        truncated=True,
    )
    service = make_service(
        retrieval=make_retrieval(),
        llm=MockLLMClient(responses=[fenced(VALID_SQL)]),
        repository=FakeRepository(execution),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is True
    assert response.execution is not None
    assert response.execution.truncated is True


@pytest.mark.asyncio
async def test_endpoint_full_success(async_client: AsyncClient) -> None:
    """Verify POST /analyze returns 200 with execution rows."""

    class StubService:
        async def analyze(
            self,
            question: str,
            *,
            model: str | None = None,
            temperature: float = 0.0,
        ) -> AnalyzeResponse:
            return AnalyzeResponse(
                question=question,
                success=True,
                retrieval_status="SUCCESS",
                confidence_score=0.9,
                final_sql="SELECT 1",
                execution=make_execution(),
            )

    app.dependency_overrides[get_analyze_service] = lambda: StubService()
    try:
        response = await async_client.post(
            "/analyze",
            json={"question": "what is total revenue?", "temperature": 0.0},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["retrieval_status"] == "SUCCESS"
    assert data["execution"]["rows"] == [[103_555.12]]
    assert data["execution"]["columns"] == ["revenue"]


@pytest.mark.asyncio
async def test_endpoint_reports_failed_stage(async_client: AsyncClient) -> None:
    """Verify failed pipelines still return 200 with diagnostics."""

    class StubService:
        async def analyze(
            self,
            question: str,
            *,
            model: str | None = None,
            temperature: float = 0.0,
        ) -> AnalyzeResponse:
            return AnalyzeResponse(
                question=question,
                success=False,
                failed_stage="generation",
                failure_reason="guardrails rejected the query",
                retrieval_status="SUCCESS",
                confidence_score=0.9,
            )

    app.dependency_overrides[get_analyze_service] = lambda: StubService()
    try:
        response = await async_client.post(
            "/analyze",
            json={"question": "what is total revenue?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False
    assert data["failed_stage"] == "generation"
    assert data["execution"] is None


@pytest.mark.asyncio
async def test_endpoint_validates_question_payload(async_client: AsyncClient) -> None:
    """Verify short payloads fail 422 validation without calling the service."""
    response = await async_client.post("/analyze", json={"question": "ab"})

    assert response.status_code == 422
