from fastapi import APIRouter, Response, status
from fastapi.responses import JSONResponse

from app.db.postgres import check_postgres_connection
from app.db.redis import check_redis_connection
from app.schemas.health import DependencyStatus, HealthResponse, ReadinessResponse

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness probe",
    description="Returns OK if the backend API service is running.",
)
async def health_check() -> HealthResponse:
    """Basic liveness check."""
    return HealthResponse(status="ok", service="conversational-analytics-agent")


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    responses={
        status.HTTP_200_OK: {"model": ReadinessResponse},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse},
    },
    summary="Readiness probe",
    description="Verifies connectivity to all backing services (PostgreSQL, Redis).",
)
async def readiness_check(response: Response) -> ReadinessResponse | JSONResponse:
    """Check readiness of critical dependencies."""
    pg_ok = await check_postgres_connection()
    redis_ok = await check_redis_connection()

    all_ready = pg_ok and redis_ok

    body = ReadinessResponse(
        status="ready" if all_ready else "unhealthy",
        dependencies=DependencyStatus(
            postgres="ok" if pg_ok else "unavailable",
            redis="ok" if redis_ok else "unavailable",
        ),
    )

    if not all_ready:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(),
        )

    return body
