"""Conversational analytics endpoint orchestrating retrieval, generation, and execution."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.schemas.analyze import AnalyzeRequest, AnalyzeResponse
from app.services.analyze_service import AnalyzeService, get_analyze_service

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
        "rows, columns, and pipeline diagnostics; failures report the failing stage."
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
    )
