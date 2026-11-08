"""Integration tests for the Streamlit demo UI's HTTP bridge to the FastAPI backend."""

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

CONVERSATION_ID = "conv-123"
ANALYZE_PAYLOAD: dict[str, Any] = {
    "question": "Which state has the most orders?",
    "conversation_id": CONVERSATION_ID,
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
    "warnings": [],
    "execution": {
        "status": "SUCCESS",
        "sql": "SELECT customer_state, COUNT(*) AS orders FROM business.orders LIMIT 10",
        "columns": ["customer_state", "orders"],
        "rows": [["SP", 41746], ["MG", 11600]],
        "row_count": 2,
        "total_rows_fetched": 2,
        "truncated": False,
        "execution_ms": 18.4,
        "limit_applied": 10,
        "error_code": None,
        "error_message": None,
    },
}


def make_client(handler) -> AnalyticsClient:
    """Build a client backed by an httpx.MockTransport handler."""
    return AnalyticsClient(
        base_url="http://testserver",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def test_analyze_success_returns_payload_and_sends_schema_fields() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=ANALYZE_PAYLOAD)

    payload = make_client(handler).analyze(
        "Which state has the most orders?",
        conversation_id=None,
        model=None,
        temperature=0.2,
    )

    assert captured["method"] == "POST"
    assert captured["path"] == "/analyze"
    assert captured["body"] == {
        "question": "Which state has the most orders?",
        "temperature": 0.2,
    }
    assert payload["success"] is True
    assert payload["conversation_id"] == CONVERSATION_ID
    assert payload["execution"]["row_count"] == 2


def test_analyze_sends_conversation_id_for_multi_turn() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={**ANALYZE_PAYLOAD, "question": body["question"]})

    payload = make_client(handler).analyze(
        "and group by month?",
        conversation_id=CONVERSATION_ID,
        temperature=0.0,
    )

    assert payload["conversation_id"] == CONVERSATION_ID


def test_analyze_forwards_optional_model_override() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json={**ANALYZE_PAYLOAD, "question": body["question"]})

    make_client(handler).analyze(
        "hello",
        conversation_id=None,
        model="google/gemini-2.5-flash",
        temperature=0.0,
    )


def test_health_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        return httpx.Response(200, json={"status": "ok", "service": "conversational-analytics-agent"})

    assert make_client(handler).health()["status"] == "ok"


def test_backend_unreachable_raised_as_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.ConnectError("cannot connect", request=request)

    with pytest.raises(AnalyticsClientError) as excinfo:
        make_client(handler).analyze("hello")

    assert "Cannot reach the analytics backend" in str(excinfo.value)


def test_http_error_raised_as_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Invalid question payload."})

    with pytest.raises(AnalyticsAPIError) as excinfo:
        make_client(handler).analyze("hi")

    assert excinfo.value.status_code == 422


def test_invalid_json_raised_as_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway error</html>")

    with pytest.raises(AnalyticsAPIError):
        make_client(handler).analyze("hello")


def test_unexpected_payload_shape_raised_as_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(AnalyticsAPIError):
        make_client(handler).analyze("hello")
