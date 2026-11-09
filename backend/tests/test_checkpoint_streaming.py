"""Tests for LangGraph checkpointing (multi-turn memory) and SSE streaming.

Covers:
- Multi-turn conversation state carrying over a checkpointer thread.
- Per-turn state isolation (stale artifacts never leak across turns).
- The ``stream_analyze`` event protocol (start -> node* -> result).
- SSE keep-alive heartbeats and error propagation.
- The ``/analyze/stream`` HTTP endpoint wire format.
"""

import asyncio
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from app.agents.graph.checkpointer import create_checkpointer
from app.agents.llm_client import MockLLMClient
from app.main import app
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.security.schemas import ValidationResult
from app.semantic.retrieval.schemas import (
    RetrievalResult,
    RetrievalStatus,
    SelectedMetric,
)
from app.services.analyze_service import AnalyzeService, get_analyze_service
from app.services.sse import format_sse, heartbeat_stream

SQL_STATE_REVENUE = """SELECT
    c.customer_state,
    SUM(p.payment_value) AS revenue
FROM business.payments p
JOIN business.orders o ON p.order_id = o.order_id
JOIN business.customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state"""

SQL_TOTAL_REVENUE = "SELECT SUM(p.payment_value) AS revenue FROM business.payments p"


def fenced(sql: str) -> str:
    """Wrap SQL in a ```sql fence exactly as LLM responses appear."""
    return f"```sql\n{sql}\n```"


def make_retrieval(with_metrics: bool = True) -> RetrievalResult:
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
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.85,
        selected_metrics=metrics,
        selected_dimensions=[],
        is_safe_for_direct_execution=True,
    )


def make_execution() -> ExecutionResult:
    """Build a successful ExecutionResult fixture."""
    return ExecutionResult(
        status=ExecutionStatus.SUCCESS,
        sql="SELECT 1",
        columns=["revenue"],
        rows=[[103_555.12]],
        row_count=1,
        total_rows_fetched=1,
    )


class SequencedRetriever:
    """Stub retriever returning canned results in order, last one repeated."""

    def __init__(self, results: list[RetrievalResult]) -> None:
        self._results = list(results)

    def retrieve(self, question: str) -> RetrievalResult:
        if len(self._results) > 1:
            return self._results.pop(0)
        return self._results[0]


class FakeRepository:
    """Stub executor returning a canned ExecutionResult."""

    def __init__(self, result: ExecutionResult) -> None:
        self.result = result
        self.calls: list[str] = []

    async def execute(self, sql: str, **kwargs: object) -> ExecutionResult:
        self.calls.append(sql)
        return self.result


class AlwaysValidValidator:
    """Validator stub that accepts every candidate and returns it unchanged."""

    def validate(self, sql: str, *, retrieval: RetrievalResult | None = None) -> ValidationResult:
        return ValidationResult(is_valid=True, original_sql=sql, sanitized_sql=sql)


def make_service(
    *,
    retrievals: list[RetrievalResult],
    llm: MockLLMClient,
    checkpointer: Any | None = None,
) -> AnalyzeService:
    """Build an AnalyzeService wired with fake components and optionally a checkpointer."""
    return AnalyzeService(
        retriever=SequencedRetriever(retrievals),
        llm_client=llm,
        validator=AlwaysValidValidator(),
        repository=FakeRepository(make_execution()),
        checkpointer=checkpointer,
    )


# ==============================================================================
# CHECKPOINTING / MULTI-TURN
# ==============================================================================


@pytest.mark.asyncio
async def test_multi_turn_thread_carries_conversation_context() -> None:
    """A follow-up turn shares a thread and replays prior turns to the LLM."""
    llm = MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE), fenced(SQL_STATE_REVENUE)])
    checkpointer = await create_checkpointer(backend="memory")
    service = make_service(
        retrievals=[make_retrieval(), make_retrieval()],
        llm=llm,
        checkpointer=checkpointer,
    )

    first = await service.analyze("what is total revenue?")
    assert first.success is True
    assert first.conversation_id is not None

    second = await service.analyze(
        "and group revenue by customer state?",
        conversation_id=first.conversation_id,
    )
    assert second.success is True
    assert second.conversation_id == first.conversation_id
    assert second.final_sql == SQL_STATE_REVENUE

    # The second LLM call must include the first turn's conversation.
    assert len(llm.call_history) == 2
    first_turn_messages = llm.call_history[0]
    second_turn_messages = llm.call_history[1]
    joined = " ".join(message["content"] for message in second_turn_messages)
    assert len(second_turn_messages) > len(first_turn_messages)
    assert SQL_TOTAL_REVENUE in joined  # first turn's assistant SQL is visible


@pytest.mark.asyncio
async def test_each_unidentified_turn_gets_a_new_thread() -> None:
    """After every turn without a provided conversation id, a fresh thread is created."""
    checkpointer = await create_checkpointer(backend="memory")
    service = make_service(
        retrievals=[make_retrieval(), make_retrieval()],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)] * 2),
        checkpointer=checkpointer,
    )

    first = await service.analyze("what is total revenue?")
    second = await service.analyze("what is total revenue?")

    assert first.conversation_id is not None
    assert second.conversation_id is not None
    assert first.conversation_id != second.conversation_id


@pytest.mark.asyncio
async def test_explicit_conversation_id_is_echoed() -> None:
    """Client-provided thread ids are honored and echoed on the response."""
    checkpointer = await create_checkpointer(backend="memory")
    service = make_service(
        retrievals=[make_retrieval()],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)]),
        checkpointer=checkpointer,
    )

    response = await service.analyze("what is total revenue?", conversation_id="conv-abc-123")

    assert response.success is True
    assert response.conversation_id == "conv-abc-123"


@pytest.mark.asyncio
async def test_turn_state_is_reset_between_turns() -> None:
    """Artifacts of turn one must never surface in a failed turn two."""
    checkpointer = await create_checkpointer(backend="memory")
    service = make_service(
        retrievals=[make_retrieval(), make_retrieval(with_metrics=False)],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)]),
        checkpointer=checkpointer,
    )

    first = await service.analyze("what is total revenue?")
    assert first.success is True

    second = await service.analyze(
        "unrelated small talk, please",
        conversation_id=first.conversation_id,
    )

    assert second.success is False
    assert second.failed_stage == "retrieval"
    # Stale per-turn artifacts from turn one must not leak into turn two.
    assert second.raw_sql is None
    assert second.final_sql is None
    assert second.execution is None
    assert second.repair_attempts == 0


@pytest.mark.asyncio
async def test_service_without_checkpointer_stays_stateless() -> None:
    """No checkpointer -> no fabricated conversation ids; behavior unchanged."""
    service = make_service(
        retrievals=[make_retrieval()],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)]),
    )

    response = await service.analyze("what is total revenue?")

    assert response.success is True
    assert response.conversation_id is None


# ==============================================================================
# SSE STREAMING
# ==============================================================================


@pytest.mark.asyncio
async def test_stream_events_follow_the_pipeline_sequence() -> None:
    """Events run start -> node events in graph order -> result; then the stream ends."""
    checkpointer = await create_checkpointer(backend="memory")
    service = make_service(
        retrievals=[make_retrieval()],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)]),
        checkpointer=checkpointer,
    )

    events = [
        (event, payload)
        async for event, payload in service.stream_analyze(
            "what is total revenue?", conversation_id="thread-e2e"
        )
    ]

    names = [name for name, _ in events]
    assert names[0] == "start"
    assert names[-1] == "result"

    nodes = [payload["node"] for name, payload in events if name == "node"]
    assert nodes == ["retrieve", "generate", "validate", "execute"]

    order = {name: index for index, name in enumerate(nodes)}
    retriever_idx, generate_idx = order["retrieve"], order["generate"]
    validate_idx, execute_idx = order["validate"], order["execute"]
    assert retriever_idx < generate_idx < validate_idx < execute_idx

    event_name, result = events[-1]
    assert event_name == "result"
    assert result["conversation_id"] == "thread-e2e"
    assert result["response"]["question"] == "what is total revenue?"
    assert result["response"]["success"] is True
    assert result["response"]["final_sql"] == SQL_TOTAL_REVENUE


@pytest.mark.asyncio
async def test_stream_retrieval_failure_still_yields_result() -> None:
    """A retrieval-gated run finalizes with a failed response, not an error event."""
    service = make_service(
        retrievals=[make_retrieval(with_metrics=False)],
        llm=MockLLMClient(),
    )

    events = [
        (event, payload) async for event, payload in service.stream_analyze("hello weather today?")
    ]

    names = [name for name, _ in events]
    assert names == ["start", "node", "result"]
    assert events[-1][1]["response"]["success"] is False
    assert events[-1][1]["response"]["failed_stage"] == "retrieval"


@pytest.mark.asyncio
async def test_stream_start_echoes_client_conversation_id() -> None:
    """The leading start event carries the supplied thread id for UI binding."""
    service = make_service(
        retrievals=[make_retrieval()],
        llm=MockLLMClient(responses=[fenced(SQL_TOTAL_REVENUE)]),
    )

    events = [
        (event, payload)
        async for event, payload in service.stream_analyze(
            "what is total revenue?", conversation_id="conv-42"
        )
    ]

    assert events[0] == ("start", {"conversation_id": "conv-42"})


# ==============================================================================
# SSE HEARTBEAT
# ==============================================================================


@pytest.mark.asyncio
async def test_heartbeat_emits_keep_alive_when_idle() -> None:
    """Silence shorter than the heartbeat interval produces keep-alive comments."""

    async def slow_source() -> AsyncGenerator[tuple[str, dict], None]:
        await asyncio.sleep(0.05)
        yield "node", {"node": "retrieve"}

    frames = [frame async for frame in heartbeat_stream(slow_source(), interval_seconds=0.01)]

    keep_alives = [frame for frame in frames if frame.startswith(": keep-alive")]
    assert keep_alives, "expected at least one keep-alive comment while idle"
    assert frames[-1].startswith("event: node\n")
    assert 'data: {"node": "retrieve"}' in frames[-1]


@pytest.mark.asyncio
async def test_heartbeat_formats_and_forwards_all_events() -> None:
    """Events pass through unchanged, serialized as SSE frames."""

    async def source() -> AsyncGenerator[tuple[str, dict], None]:
        yield "start", {"conversation_id": "t1"}
        yield "result", {"ok": True}

    frames = [frame async for frame in heartbeat_stream(source(), interval_seconds=5)]

    assert frames[0] == format_sse("start", {"conversation_id": "t1"})
    assert frames[1] == format_sse("result", {"ok": True})
    assert len(frames) == 2  # no keep-alives: source never idle long enough


@pytest.mark.asyncio
async def test_heartbeat_propagates_source_errors() -> None:
    """Source exceptions are re-raised on the consumer side."""

    async def failing_source() -> AsyncGenerator[tuple[str, dict], None]:
        yield "node", {"node": "retrieve"}
        raise RuntimeError("executor exploded")

    with pytest.raises(RuntimeError, match="executor exploded"):
        _ = [frame async for frame in heartbeat_stream(failing_source(), interval_seconds=5)]


# ==============================================================================
# HTTP ENDPOINT
# ==============================================================================


@pytest.mark.asyncio
async def test_stream_endpoint_emits_sse_wire_format(async_client: AsyncClient) -> None:
    """POST /analyze/stream returns text/event-stream with proper frame order."""

    class StubService:
        async def stream_analyze(
            self,
            question: str,
            *,
            model: str | None = None,
            temperature: float = 0.0,
            conversation_id: str | None = None,
        ) -> AsyncGenerator[tuple[str, dict], None]:
            yield "start", {"conversation_id": None}
            yield "node", {"node": "retrieve"}
            yield "result", {"conversation_id": "conv-9", "response": {"success": True}}

    app.dependency_overrides[get_analyze_service] = lambda: StubService()
    try:
        response = await async_client.post(
            "/analyze/stream",
            json={"question": "what is total revenue?", "conversation_id": "conv-9"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"

    body = response.text
    assert body.index("event: start") < body.index("event: node")
    assert body.index("event: node") < body.index("event: result")
    assert body.index("event: result") < body.index("event: done")
    assert body.rstrip().endswith("data: {}")
    # Frames must be properly closed by a blank line.
    assert body.count("event: ") == 4
    assert body.count("data: ") == 4


@pytest.mark.asyncio
async def test_stream_endpoint_sends_error_and_done_on_failure(
    async_client: AsyncClient,
) -> None:
    """Exceptions inside the stream degrade to an error event, not a broken pipe."""

    class FailingService:
        async def stream_analyze(
            self,
            question: str,
            *,
            model: str | None = None,
            temperature: float = 0.0,
            conversation_id: str | None = None,
        ) -> AsyncGenerator[tuple[str, dict], None]:
            yield "start", {"conversation_id": None}
            yield "node", {"node": "generate"}
            raise RuntimeError("postgres exploded")

    app.dependency_overrides[get_analyze_service] = lambda: FailingService()
    try:
        response = await async_client.post(
            "/analyze/stream",
            json={"question": "what is total revenue?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.text
    assert "event: start" in body
    assert "event: error" in body
    assert "event: done" in body
    assert body.index("event: error") < body.index("event: done")


@pytest.mark.asyncio
async def test_stream_endpoint_heartbeats_between_slow_events(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep-alives appear in the HTTP body when the service is slow."""
    import app.api.routes.analyze as analyze_route

    class SlowService:
        async def stream_analyze(
            self,
            question: str,
            *,
            model: str | None = None,
            temperature: float = 0.0,
            conversation_id: str | None = None,
        ) -> AsyncGenerator[tuple[str, dict], None]:
            await asyncio.sleep(0.05)
            yield "start", {"conversation_id": None}
            yield "result", {"conversation_id": None, "response": {"success": True}}

    app.dependency_overrides[get_analyze_service] = lambda: SlowService()
    monkeypatch.setattr(
        analyze_route,
        "get_settings",
        lambda: SimpleNamespace(SSE_HEARTBEAT_INTERVAL_SECONDS=0.01),
    )
    try:
        response = await async_client.post(
            "/analyze/stream",
            json={"question": "what is total revenue?"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert ": keep-alive" in response.text


@pytest.mark.asyncio
async def test_stream_endpoint_validates_question_payload(
    async_client: AsyncClient,
) -> None:
    """Short payloads fail 422 validation without touching the service."""
    response = await async_client.post("/analyze/stream", json={"question": "ab"})

    assert response.status_code == 422
