"""AppTest-based smoke tests rendering the Streamlit demo UI end to end.

These run the real script through ``streamlit.testing.v1.AppTest`` with a
mocked HTTP transport, so no backend or database has to be running.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

# AppTest requires Streamlit; skip the whole module when it is not installed.
pytest.importorskip("streamlit.testing.v1")

from streamlit.testing.v1 import AppTest  # noqa: E402

from frontend.streamlit import analytics_client as client_module  # noqa: E402

APP_PATH = Path(__file__).resolve().parents[2] / "frontend" / "streamlit" / "streamlit_app.py"

HEALTH_ROUTE = "/health"
ANALYZE_ROUTE = "/analyze"
STREAM_ROUTE = "/analyze/stream"

Handler = Callable[[httpx.Request], httpx.Response]

ANALYZE_PAYLOAD: dict[str, Any] = {
    "question": "Which state has the most orders?",
    "conversation_id": "conv-1",
    "success": True,
    "failed_stage": None,
    "failure_reason": None,
    "retrieval_status": "SUCCESS",
    "confidence_score": 0.87,
    "final_sql": "SELECT customer_state, COUNT(*) AS orders FROM business.orders LIMIT 10",
    "raw_sql": "SELECT customer_state, count(*) AS orders FROM orders LIMIT 10",
    "explanation": None,
    "repair_attempts": 1,
    "model_used": "google/gemini-2.5-flash",
    "warnings": ["Row limit enforced"],
    "execution": {
        "status": "SUCCESS",
        "sql": "SELECT ...",
        "columns": ["customer_state", "orders"],
        "rows": [["SP", 41746], ["MG", 11600], ["RJ", 7000]],
        "row_count": 3,
        "total_rows_fetched": 3,
        "truncated": False,
        "execution_ms": 18.4,
        "limit_applied": 10,
        "error_code": None,
        "error_message": None,
    },
}


def handler_health(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "ok", "service": "conversational-analytics-agent"})


def sse_response(frames: list[tuple[str, dict[str, Any]]]) -> httpx.Response:
    """Serialize frames like the backend's ``app.services.sse`` module."""
    text = ""
    for event, data in frames:
        text += f"event: {event}\ndata: {json.dumps(data)}\n\n"
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=text.encode()
    )


def success_stream_frames() -> list[tuple[str, dict[str, Any]]]:
    return [
        ("start", {"conversation_id": "conv-1"}),
        ("node", {"node": "retrieve"}),
        ("node", {"node": "generate"}),
        ("node", {"node": "validate"}),
        ("node", {"node": "execute"}),
        ("node", {"node": "finalize"}),
        ("result", {"conversation_id": "conv-1", "response": ANALYZE_PAYLOAD}),
        ("done", {}),
    ]


class MockRoutes:
    """Route dict -> injectable httpx client bridge for the demo app."""

    def __init__(self, routes: dict[str, Handler]) -> None:
        self._underlying = httpx.MockTransport(lambda request: routes[request.url.path](request))

    def client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            transport=self._underlying,
            base_url="http://testserver",
            timeout=timeout or 5.0,
        )


@pytest.fixture
def app_factory(monkeypatch: pytest.MonkeyPatch):
    """Factory running the demo app with its HTTP bridge bound to mock routes."""

    def _factory(routes: dict[str, Handler]) -> AppTest:
        bridge = MockRoutes(routes)
        monkeypatch.setattr(
            client_module.AnalyticsClient,
            "_http_client",
            lambda self: bridge.client(timeout=self.timeout),
        )
        return AppTest.from_file(str(APP_PATH), default_timeout=60)

    return _factory


def test_first_run_renders_header_and_reaches_backend(app_factory) -> None:
    app = app_factory({HEALTH_ROUTE: handler_health})
    app.run()

    assert not app.exception
    assert any("Conversational Analytics Agent" in title.value for title in app.title)


def test_successful_question_renders_interactive_answer(app_factory) -> None:
    app = app_factory(
        {
            HEALTH_ROUTE: handler_health,
            ANALYZE_ROUTE: lambda request: httpx.Response(200, json=ANALYZE_PAYLOAD),
        }
    )
    app.run()
    app.toggle[0].set_value(False).run()  # blocking POST /analyze path
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Which state has the most orders?"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["kind"] == "result"
    assert messages[1]["payload"]["success"] is True
    # The conversation id round-trip enables multi-turn follow-ups.
    assert app.session_state["conversation_id"] == "conv-1"
    # Result table and executed SQL are rendered.
    assert len(app.dataframe) == 1
    assert any(ANALYZE_PAYLOAD["final_sql"] in code.value for code in app.code)


def test_backend_outage_renders_friendly_error(app_factory) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = app_factory({HEALTH_ROUTE: failing, ANALYZE_ROUTE: failing})
    app.run()
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[-1]["kind"] == "error"
    assert "Could not reach the analytics backend" in messages[-1]["content"]


def test_api_error_renders_friendly_error(app_factory) -> None:
    app = app_factory(
        {
            HEALTH_ROUTE: handler_health,
            ANALYZE_ROUTE: lambda request: httpx.Response(500, json={"detail": "Internal error"}),
        }
    )
    app.run()
    app.toggle[0].set_value(False).run()  # blocking POST /analyze path
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[-1]["kind"] == "error"
    assert "analytics API rejected the request" in messages[-1]["content"]


def test_streaming_turn_adopts_conversation_and_renders_result(app_factory) -> None:
    # First run must flip the toggle ON only if the widget exists; it defaults
    # to True in session state, so POST /analyze/stream is used directly.
    app = app_factory(
        {
            HEALTH_ROUTE: handler_health,
            STREAM_ROUTE: lambda request: sse_response(success_stream_frames()),
        }
    )
    app.run()
    assert not app.exception
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[-1]["kind"] == "result"
    assert messages[-1]["payload"]["success"] is True
    # Conversation id comes from the stream's ``start`` event.
    assert app.session_state["conversation_id"] == "conv-1"
    assert len(app.dataframe) == 1


def test_streaming_pipeline_error_renders_friendly_error(app_factory) -> None:
    frames = [
        ("start", {"conversation_id": "conv-1"}),
        ("node", {"node": "retrieve"}),
        ("node", {"node": "generate"}),
        ("error", {"reason": "The analytics workflow exceeded its maximum number of steps."}),
        ("done", {}),
    ]
    app = app_factory(
        {
            HEALTH_ROUTE: handler_health,
            STREAM_ROUTE: lambda request: sse_response(frames),
        }
    )
    app.run()
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[-1]["kind"] == "error"
    assert "pipeline reported a failure" in messages[-1]["content"]
    assert "maximum number of steps" in messages[-1]["content"]
    # A started conversation is kept alive even when the turn failed.
    assert app.session_state["conversation_id"] == "conv-1"


def test_streaming_connection_loss_renders_friendly_error(app_factory) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    app = app_factory(
        {
            HEALTH_ROUTE: failing,
            STREAM_ROUTE: failing,
        }
    )
    app.run()
    app.chat_input[0].set_value("Which state has the most orders?").run()

    assert not app.exception
    messages = app.session_state["messages"]
    assert messages[-1]["kind"] == "error"
    assert "Could not reach the analytics backend" in messages[-1]["content"]
