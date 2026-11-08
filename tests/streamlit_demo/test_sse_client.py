"""Unit tests for the SSE streaming bridge (POST /analyze/stream consumption)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from frontend.streamlit.analytics_client import (
    AnalyticsAPIError,
    AnalyticsClient,
    AnalyticsClientError,
)

CONVERSATION_ID = "conv-sse"

RESULT_PAYLOAD: dict[str, Any] = {
    "question": "Which state has the most orders?",
    "conversation_id": CONVERSATION_ID,
    "success": True,
    "failed_stage": None,
    "failure_reason": None,
    "retrieval_status": "SUCCESS",
    "confidence_score": 0.9,
    "final_sql": "SELECT 1",
    "raw_sql": "SELECT 1",
    "explanation": None,
    "repair_attempts": 0,
    "model_used": "mock",
    "warnings": [],
    "execution": {
        "status": "SUCCESS",
        "sql": "SELECT 1",
        "columns": ["one"],
        "rows": [[1]],
        "row_count": 1,
        "total_rows_fetched": 1,
        "truncated": False,
        "execution_ms": 1.0,
        "limit_applied": None,
        "error_code": None,
        "error_message": None,
    },
}


def make_stream_client(handler) -> AnalyticsClient:
    return AnalyticsClient(
        base_url="http://testserver",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def sse_body(frames: list[tuple[str, dict[str, Any]]]) -> bytes:
    """Encode frames exactly like ``app.services.sse.format_sse`` does."""
    text = ""
    for event, data in frames:
        text += f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    return text.encode()


def default_stream_frames() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("start", {"conversation_id": CONVERSATION_ID}),
        ("node", {"node": "retrieve"}),
        ("node", {"node": "generate"}),
        ("node", {"node": "validate"}),
        ("node", {"node": "execute"}),
        ("node", {"node": "finalize"}),
        ("result", {"conversation_id": CONVERSATION_ID, "response": RESULT_PAYLOAD}),
        ("done", {}),
    ]


def analyze_request_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_stream_analyze_yields_backend_events_in_order() -> None:
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path
        sent["body"] = analyze_request_body(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body(default_stream_frames()),
        )

    events = list(make_stream_client(handler).stream_analyze("Which state has the most orders?"))

    assert sent["path"] == "/analyze/stream"
    assert sent["body"]["question"] == "Which state has the most orders?"
    # Every wire event is surfaced, including done; keep-alives add nothing.
    assert [e for e, _ in events] == [
        "start",
        "node",
        "node",
        "node",
        "node",
        "node",
        "result",
        "done",
    ]
    assert events[0][1]["conversation_id"] == CONVERSATION_ID
    assert events[-2][1]["response"] == RESULT_PAYLOAD


def test_stream_analyze_skips_keepalive_comments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = sse_body(default_stream_frames())
        # Heartbeats are frame-level comments; splice one between frames.
        body = body.replace(
            b'event: node',
            b": keep-alive\n\nevent: node",
            1,
        )
        return httpx.Response(200, content=body)

    events = list(make_stream_client(handler).stream_analyze("hi"))
    kinds = [event for event, _ in events]
    assert kinds.count("node") == 5
    assert "result" in kinds


def test_stream_analyze_sends_conversation_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert analyze_request_body(request)["conversation_id"] == CONVERSATION_ID
        return httpx.Response(200, content=sse_body(default_stream_frames()))

    list(make_stream_client(handler).stream_analyze("follow up?", conversation_id=CONVERSATION_ID))


def test_stream_analyze_forwards_model_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert analyze_request_body(request)["model"] == "left/model"
        return httpx.Response(200, content=sse_body(default_stream_frames()))

    list(make_stream_client(handler).stream_analyze("hi", model="left/model"))


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_stream_error_event_is_surfaced_to_the_consumer() -> None:
    # The client yields the raw ``error`` event; turning it into an exception
    # (with the backend-provided reason) is the consumer's job — the demo UI
    # does exactly that in ``ask_backend_stream``.
    frames = [
        ("start", {"conversation_id": CONVERSATION_ID}),
        ("node", {"node": "retrieve"}),
        ("node", {"node": "generate"}),
        ("error", {"reason": "The analytics workflow exceeded its maximum number of steps."}),
        ("done", {}),
    ]

    events = list(
        make_stream_client(
            lambda request: httpx.Response(200, content=sse_body(frames))
        ).stream_analyze("hi")
    )
    assert events[-2] == (
        "error",
        {"reason": "The analytics workflow exceeded its maximum number of steps."},
    )


def test_stream_http_error_raises_api_error() -> None:
    with pytest.raises(AnalyticsAPIError) as excinfo:
        list(
            make_stream_client(
                lambda request: httpx.Response(404, json={"detail": "Not Found"})
            ).stream_analyze("hi")
        )

    assert excinfo.value.status_code == 404


def test_stream_unreachable_backend_raises_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.ConnectError("cannot connect", request=request)

    with pytest.raises(AnalyticsClientError):
        list(make_stream_client(handler).stream_analyze("hi"))


def test_stream_malformed_json_data_raises_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"event: start\ndata: {not-json}\n\n")

    with pytest.raises(AnalyticsAPIError):
        list(make_stream_client(handler).stream_analyze("hi"))


def test_stream_without_result_event_simply_yields_available_events() -> None:
    # At the client level a missing ``result`` is not itself a protocol error;
    # the consumer (the demo UI) decides whether a run concluded successfully.
    frames = [
        ("start", {"conversation_id": CONVERSATION_ID}),
        ("node", {"node": "retrieve"}),
        ("done", {}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse_body(frames))

    events = list(make_stream_client(handler).stream_analyze("hi"))
    assert [event for event, _ in events] == ["start", "node", "done"]
