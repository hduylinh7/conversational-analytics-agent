"""HTTP bridge between the Streamlit demo UI and the FastAPI analytics backend.

The demo UI keeps a strict separation from the backend: all agent logic lives
behind the existing ``POST /analyze`` API and this thin client only commits it
as JSON and maps transport-level failures into user-friendly exceptions.

Configuration is read from environment variables so deployments can point the
UI at a different backend without code changes:

- ``STREAMLIT_BACKEND_URL``    Base URL of the FastAPI service.
- ``STREAMLIT_TIMEOUT_SECONDS``Per-request HTTP timeout (LLM calls can be slow).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BACKEND_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 120.0

_TIMEOUT_CAP_SECONDS = 900.0


def backend_url() -> str:
    """Base URL of the analytics backend, configurable via environment."""
    return os.getenv("STREAMLIT_BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")


def backend_timeout() -> float:
    """Per-request HTTP timeout in seconds, configurable via environment."""
    raw = os.getenv("STREAMLIT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if timeout <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(timeout, _TIMEOUT_CAP_SECONDS)


class AnalyticsClientError(RuntimeError):
    """Raised when the analytics backend cannot be reached."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.__cause__ = cause


class AnalyticsAPIError(AnalyticsClientError):
    """Raised for non-2xx responses or payloads that do not match the API contract."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PipelineError(AnalyticsClientError):
    """Raised when the analytics pipeline itself reports a failure over SSE.

    Carries the user-friendly ``reason`` emitted by the stream's ``error``
    event (e.g. LangGraph recursion-limit exhaustion), distinct from transport
    problems between the UI and the backend.
    """



class AnalyticsClient:
    """Synchronous HTTP client for the conversational analytics API.

    ``transport`` is injectable (``httpx.MockTransport``) so the client can be
    unit-tested without a running backend. It is ignored by the demo app.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or backend_url()).rstrip("/")
        self.timeout = timeout if timeout is not None else backend_timeout()
        self._transport = transport

    def _http_client(self) -> httpx.Client:
        if self._transport is not None:
            return httpx.Client(
                transport=self._transport,
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return httpx.Client(base_url=self.base_url, timeout=self.timeout)

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def analyze(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """Run the analytics pipeline for one question and return its response.

        Returns the ``AnalyzeResponse`` payload as a plain dict (the demo UI
        deliberately does not import backend models). Raises
        :class:`AnalyticsClientError` when the backend is unreachable and
        :class:`AnalyticsAPIError` for protocol/validation failures.
        """
        payload: dict[str, Any] = {"question": question, "temperature": temperature}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if model:
            payload["model"] = model

        logger.debug(
            "POST %s/analyze (conversation=%s, question=%r)",
            self.base_url,
            conversation_id,
            question,
        )

        try:
            response = self._request("POST", "/analyze", json_payload=payload)
        except AnalyticsClientError:
            raise
        except Exception as exc:  # unexpected transport/HTTP client failure
            logger.exception("Unexpected error while calling %s/analyze", self.base_url)
            raise AnalyticsClientError(
                "Unexpected problem while contacting the analytics backend.",
                cause=exc,
            ) from exc

        parsed = _parse_json(response)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("success"), bool):
            logger.debug("Analyze response payload: %.2000s", repr(parsed))
            raise AnalyticsAPIError(
                "The analytics backend returned an unexpected response format.",
                status_code=response.status_code,
            )
        return parsed

    def health(self) -> dict[str, Any]:
        """Call the liveness endpoint; raises :class:`AnalyticsClientError` when down."""
        response = self._request("GET", "/health", expect_json=True)
        parsed = _parse_json(response)
        if not isinstance(parsed, dict):
            raise AnalyticsAPIError("Unexpected /health response format.", status_code=response.status_code)
        return parsed

    # ------------------------------------------------------------------
    # SSE streaming endpoint
    # ------------------------------------------------------------------

    def stream_analyze(
        self,
        question: str,
        *,
        conversation_id: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> Generator[tuple[str, dict[str, Any]]]:
        """Consume ``POST /analyze/stream`` and yield ``(event, payload)`` tuples.

        Events follow the backend contract: ``start`` (conversation id),
        one ``node`` event per completed graph node (retrieve/generate/
        validate/execute/repair/finalize), ``result`` (full AnalyzeResponse),
        ``error`` (pipeline-level failure), and a final ``done``. Keep-alive
        comment frames are silently skipped. The generator raises for
        transport errors (:class:`AnalyticsClientError`), protocol problems
        (:class:`AnalyticsAPIError`), and pipeline failures
        (:class:`PipelineError`, carrying the backend-provided reason).
        """
        payload: dict[str, Any] = {"question": question, "temperature": temperature}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if model:
            payload["model"] = model

        logger.debug(
            "POST %s/analyze/stream (conversation=%s, question=%r)",
            self.base_url,
            conversation_id,
            question,
        )

        try:
            with self._http_client() as client:
                with client.stream(
                    "POST", "/analyze/stream", json=payload
                ) as response:
                    response.raise_for_status()
                    yield from _parse_sse_stream(response)
        except PipelineError:
            raise
        except AnalyticsClientError:
            raise
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Analytics API returned HTTP %d for POST /analyze/stream: %s",
                exc.response.status_code,
                _error_detail(exc.response),
            )
            raise AnalyticsAPIError(
                f"The analytics backend rejected the request (HTTP {exc.response.status_code}).",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            logger.warning(
                "Stream broke while reading /analyze/stream at %s: %s",
                self.base_url,
                exc,
            )
            raise AnalyticsClientError(
                f"Lost the connection to the analytics backend at {self.base_url}.",
                cause=exc,
            ) from exc
        except Exception as exc:
            logger.exception("Unexpected error reading %s/analyze/stream", self.base_url)
            raise AnalyticsClientError(
                "Unexpected problem while contacting the analytics backend.",
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        json_payload: Mapping[str, Any] | None = None,
        *,
        expect_json: bool = True,
    ) -> httpx.Response:
        """Perform one request, mapping transport failures to friendly errors."""
        try:
            with self._http_client() as client:
                response = client.request(
                    method,
                    path,
                    json=json_payload if json_payload is not None else None,
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _error_detail(exc.response)
            logger.warning(
                "Analytics API returned HTTP %d for %s %s: %s",
                exc.response.status_code,
                method,
                path,
                detail,
            )
            raise AnalyticsAPIError(
                f"The analytics backend rejected the request (HTTP {exc.response.status_code}).",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TransportError as exc:
            logger.warning("Cannot reach analytics backend at %s: %s", self.base_url, exc)
            raise AnalyticsClientError(
                f"Cannot reach the analytics backend at {self.base_url}.",
                cause=exc,
            ) from exc

        if expect_json:
            _parse_json(response)  # validate decodability early; body cached
        return response


def _parse_json(response: httpx.Response) -> Any:
    """Decode a JSON response body, raising :class:`AnalyticsAPIError` when invalid."""
    try:
        return response.json()
    except ValueError as exc:
        logger.warning("Backend returned a non-JSON body (HTTP %d).", response.status_code)
        raise AnalyticsAPIError(
            "The analytics backend returned an invalid response.",
            status_code=response.status_code,
        ) from exc


def _parse_sse_stream(response: httpx.Response) -> Generator[tuple[str, dict[str, Any]]]:
    """Yield ``(event, payload)`` tuples from an SSE response body.

    Understands the minimal SSE wire format the backend emits (``event:`` /
    ``data:`` pairs separated by blank lines), skipping comment keep-alives
    (``: keep-alive``). Malformed frames raise :class:`AnalyticsAPIError`.
    """
    current_event: str | None = None
    data_lines: list[str] = []

    def dispatch() -> Generator[tuple[str, dict[str, Any]]]:
        nonlocal current_event, data_lines
        if current_event is None:
            return
        event_name = current_event
        raw_data = "\n".join(data_lines).strip()
        current_event, data_lines = None, []
        if raw_data:
            try:
                payload = json.loads(raw_data)
            except ValueError as exc:
                logger.warning("Malformed SSE data frame for event %r.", event_name)
                raise AnalyticsAPIError(
                    "The analytics backend sent an invalid streaming event.",
                    status_code=response.status_code,
                ) from exc
            if not isinstance(payload, dict):
                raise AnalyticsAPIError(
                    "The analytics backend sent an invalid streaming event.",
                    status_code=response.status_code,
                )
        else:
            payload = {}
        yield event_name, payload

    for raw_line in response.iter_lines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            yield from dispatch()
        elif line.startswith(":"):
            continue  # keep-alive comment
        elif line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
        else:
            logger.debug("Ignoring non-SSE line from /analyze/stream: %.120s", line)

    yield from dispatch()  # tolerate a missing final blank line


def _error_detail(response: httpx.Response) -> str:
    """Best-effort extraction of a FastAPI ``detail`` message for logging."""
    try:
        parsed = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(parsed, dict) and isinstance(parsed.get("detail"), str):
        return parsed["detail"]
    return str(parsed)[:200]
