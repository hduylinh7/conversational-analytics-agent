import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

settings = get_settings()

redis_pool = aioredis.ConnectionPool.from_url(
    settings.redis_url,
    decode_responses=True,
    max_connections=20,
    socket_connect_timeout=2.0,
    socket_timeout=2.0,
)


def get_redis_client() -> Redis:
    """Return a reusable Redis client connected to the shared connection pool."""
    return Redis(connection_pool=redis_pool)


async def get_redis() -> AsyncGenerator[Redis, Any]:
    """FastAPI dependency for accessing the Redis client."""
    client = get_redis_client()
    try:
        yield client
    finally:
        await client.aclose()


async def check_redis_connection(timeout_seconds: float = 2.0) -> bool:
    """
    Verify Redis connectivity by performing a ping followed by a round-trip
    write, read, and delete test key operation.
    """
    try:
        async with asyncio.timeout(timeout_seconds):
            client = get_redis_client()
            try:
                # Step 1: Ping
                pong = await client.ping()
                if not pong:
                    return False

                # Step 2-4: Set, get, delete temporary test key
                test_key = f"healthcheck:{uuid.uuid4().hex}"
                test_value = "ok"

                await client.set(test_key, test_value, ex=10)
                retrieved = await client.get(test_key)
                await client.delete(test_key)

                return retrieved == test_value
            finally:
                await client.aclose()
    except Exception as exc:
        logger.warning("Redis connection check failed: %s", exc)
        return False


async def close_redis() -> None:
    """Disconnect and close the Redis connection pool."""
    logger.info("Closing Redis connection pool...")
    await redis_pool.disconnect()
