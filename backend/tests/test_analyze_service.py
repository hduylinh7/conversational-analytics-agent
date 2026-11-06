"""Tests for AnalyzeService orchestration and the POST /analyze endpoint."""

import pytest
from httpx import AsyncClient

from app.agents.llm_client import LLMError
from app.agents.schemas import SQLGenerationResponse
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
                expression="SUMpayments(value)",
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


def make_generation(
    is_valid: bool = True,
    final_sql: str | None = "SELECT SUM(p.value) FROM business.payments p",
    raw_sql: str | None = None,
) -> SQLGenerationResponse:
    """Build a SQLGenerationResponse fixture."""
    return SQLGenerationResponse(
        question="test question",
        raw_sql=raw_sql if raw_sql is not None else (final_sql or ""),
        is_valid=is_valid,
        final_sql=final_sql if is_valid else None,
        validation_result=ValidationResult(is_valid=is_valid, original_sql=raw_sql or ""),
        model_used="mock-model",
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


class FakeGenerator:
    """Stub SQL generator returning a canned SQLGenerationResponse and execution result."""

    def __init__(
        self,
        response: SQLGenerationResponse,
        execution: ExecutionResult | None = None,
    ) -> None:
        self.response = response
        self.execution = execution
        self.requests: list = []

    async def generate_and_execute_async(
        self, request, executor=None
    ) -> tuple[SQLGenerationResponse, ExecutionResult | None]:
        self.requests.append(request)
        return self.response, self.execution


class FailingGenerator:
    """Stub SQL generator that raises LLMError, simulating a provider outage."""

    def __init__(self, exc: LLMError) -> None:
        self.exc = exc

    async def generate_and_execute_async(self, request, executor=None) -> tuple:
        raise self.exc


class FakeRepository:
    """Stub executor returning a canned ExecutionResult and recording input SQL."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def execute(self, sql: str, **kwargs) -> ExecutionResult:
        self.calls.append(sql)
        return self.result


@pytest.mark.asyncio
async def test_service_full_success_pipeline() -> None:
    """Verify orchestration passes sanitized SQL from generator to repository."""
    retrieval = make_retrieval()
    generation = make_generation()
    execution = make_execution()
    service = AnalyzeService(
        retriever=FakeRetriever(retrieval),
        generator=FakeGenerator(generation, execution),
        repository=FakeRepository(execution),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is True
    assert response.failed_stage is None
    assert response.failure_reason is None
    assert response.final_sql == generation.final_sql
    assert response.retrieval_status == RetrievalStatus.SUCCESS
    assert response.execution is execution


@pytest.mark.asyncio
async def test_service_fails_at_retrieval_without_metrics() -> None:
    """Verify no LLM call occurs when retrieval matches nothing."""
    fake_generator = FakeGenerator(make_generation())
    service = AnalyzeService(
        retriever=FakeRetriever(make_retrieval(with_metrics=False)),
        generator=fake_generator,
        repository=FakeRepository(make_execution()),
    )

    response = await service.analyze("hello weather today?")

    assert response.success is False
    assert response.failed_stage == "retrieval"
    assert fake_generator.requests == []
    assert response.execution is None


@pytest.mark.asyncio
async def test_service_fails_at_retrieval_on_unsafe_join() -> None:
    """Verify unsafe join plans stop the pipeline before generation."""
    service = AnalyzeService(
        retriever=FakeRetriever(make_retrieval(unsafe_join=True)),
        generator=FakeGenerator(make_generation()),
        repository=FakeRepository(make_execution()),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "retrieval"
    assert "unsafe" in response.failure_reason


@pytest.mark.asyncio
async def test_service_fails_at_generation_on_guardrail_exhaustion() -> None:
    """Verify exhausted self-repair attempts stop execution."""
    service = AnalyzeService(
        retriever=FakeRetriever(make_retrieval()),
        generator=FakeGenerator(make_generation(is_valid=False, raw_sql="HACK")),
        repository=FakeRepository(make_execution()),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "generation"
    assert response.execution is None
    assert response.raw_sql == "HACK"


@pytest.mark.asyncio
async def test_service_fails_at_generation_on_llm_provider_error() -> None:
    """Verify an LLMError is mapped to a graceful generation-stage failure."""
    service = AnalyzeService(
        retriever=FakeRetriever(make_retrieval()),
        generator=FailingGenerator(
            LLMError("OpenRouter API error (404): model `string` not found")
        ),
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
async def test_service_fails_at_execution_on_database_error() -> None:
    """Verify database failures surface as failed_stage='execution'."""
    service = AnalyzeService(
        retriever=FakeRetriever(make_retrieval()),
        generator=FakeGenerator(
            make_generation(), make_execution(status=ExecutionStatus.DATABASE_ERROR)
        ),
        repository=FakeRepository(make_execution(status=ExecutionStatus.DATABASE_ERROR)),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is False
    assert response.failed_stage == "execution"
    assert response.failure_reason is not None
    assert "failed on purpose" in response.failure_reason


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
    retrieval = make_retrieval()
    generation = make_generation()
    service = AnalyzeService(
        retriever=FakeRetriever(retrieval),
        generator=FakeGenerator(generation, execution),
        repository=FakeRepository(execution),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is True
    assert response.execution.truncated is True


@pytest.mark.asyncio
async def test_endpoint_full_success(async_client: AsyncClient) -> None:
    """Verify POST /analyze returns 200 with execution rows."""

    class StubService:
        async def analyze(self, question, *, model=None, temperature=0.0):
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
        async def analyze(self, question, *, model=None, temperature=0.0):
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
