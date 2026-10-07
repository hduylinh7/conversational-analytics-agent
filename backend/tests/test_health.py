from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check_returns_ok(async_client: AsyncClient) -> None:
    """Verify that GET /health returns 200 and ok status."""
    response = await async_client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "conversational-analytics-agent"


@pytest.mark.asyncio
async def test_readiness_check_all_healthy(async_client: AsyncClient) -> None:
    """Verify that GET /health/ready returns 200 when all dependencies are healthy."""
    with (
        patch("app.api.routes.health.check_postgres_connection", new_callable=AsyncMock) as mock_pg,
        patch("app.api.routes.health.check_redis_connection", new_callable=AsyncMock) as mock_redis,
    ):
        mock_pg.return_value = True
        mock_redis.return_value = True

        response = await async_client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
        assert data["dependencies"]["postgres"] == "ok"
        assert data["dependencies"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readiness_check_postgres_down(async_client: AsyncClient) -> None:
    """Verify that GET /health/ready returns 503 when postgres is unavailable."""
    with (
        patch("app.api.routes.health.check_postgres_connection", new_callable=AsyncMock) as mock_pg,
        patch("app.api.routes.health.check_redis_connection", new_callable=AsyncMock) as mock_redis,
    ):
        mock_pg.return_value = False
        mock_redis.return_value = True

        response = await async_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["dependencies"]["postgres"] == "unavailable"
        assert data["dependencies"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readiness_check_redis_down(async_client: AsyncClient) -> None:
    """Verify that GET /health/ready returns 503 when redis is unavailable."""
    with (
        patch("app.api.routes.health.check_postgres_connection", new_callable=AsyncMock) as mock_pg,
        patch("app.api.routes.health.check_redis_connection", new_callable=AsyncMock) as mock_redis,
    ):
        mock_pg.return_value = True
        mock_redis.return_value = False

        response = await async_client.get("/health/ready")
        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["dependencies"]["postgres"] == "ok"
        assert data["dependencies"]["redis"] == "unavailable"
