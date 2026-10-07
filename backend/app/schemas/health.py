from typing import Literal

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Liveness probe response model."""

    status: Literal["ok"] = "ok"
    service: str = "conversational-analytics-agent"


class DependencyStatus(BaseModel):
    """Status details for system dependencies."""

    postgres: Literal["ok", "unavailable"]
    redis: Literal["ok", "unavailable"]


class ReadinessResponse(BaseModel):
    """Readiness probe response model."""

    status: Literal["ready", "unhealthy"]
    dependencies: DependencyStatus = Field(
        default_factory=lambda: DependencyStatus(postgres="ok", redis="ok")
    )
