"""Conversational analytics endpoints orchestrating retrieval, generation, and execution."""

from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import StreamingResponse

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.services.analyze_service import AnalyzeService, get_analyze_service
from app.services.sse import format_sse, heartbeat_stream

logger = get_logger(__name__)
router = APIRouter(tags=["Analyze"])


@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    responses={
        status.HTTP_200_OK: {"model": AnalyzeResponse},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Invalid question payload."},
    },
    summary="Ask a natural language analytics question",
    description=(
        "Full pipeline: semantic retrieval -> Text-to-SQL generation with "
        "self-repair -> guardrail validation -> PostgreSQL execution. Returns "
        "rows, columns, and pipeline diagnostics; failures report the failing stage. "
        "Supply ``conversation_id`` (or take it from the response) to continue a "
        "multi-turn conversation."
    ),
)
async def analyze(
    request: AnalyzeRequest,
    service: Annotated[AnalyzeService, Depends(get_analyze_service)],
) -> AnalyzeResponse:
    """Run the analytics pipeline for a user question."""
    return await service.analyze(
        request.question,
        model=request.model,
        temperature=request.temperature,
        conversation_id=request.conversation_id,
    )


@router.post(
    "/analyze/stream",
    summary="Ask an analytics question and stream pipeline progress (SSE)",
    description=(
        "Same pipeline as POST /analyze, but progress is streamed as "
        "Server-Sent Events: ``start`` -> one ``node`` event per completed "
        "graph node (retrieve, generate, validate, execute, repair) -> "
        "``result`` with the full response -> ``done``. An ``error`` event is "
        "sent instead of ``result`` if the pipeline fails. Keep-alive comments "
        "are emitted while the pipeline is silent."
    ),
)
async def analyze_stream(
    request: AnalyzeRequest,
    service: Annotated[AnalyzeService, Depends(get_analyze_service)],
) -> StreamingResponse:
    """Stream pipeline progress for a user question as SSE events."""

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            async for frame in heartbeat_stream(
                service.stream_analyze(
                    request.question,
                    model=request.model,
                    temperature=request.temperature,
                    conversation_id=request.conversation_id,
                ),
                interval_seconds=get_settings().SSE_HEARTBEAT_INTERVAL_SECONDS,
            ):
                yield frame
        except Exception:
            logger.exception("Analyze streaming pipeline failed")
            yield format_sse(
                "error",
                {"reason": "The analytics pipeline failed while streaming."},
            )
        yield format_sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Disable buffering in reverse proxies (e.g. nginx) so events
            # reach the client as they are produced.
            "X-Accel-Buffering": "no",
        },
    )
