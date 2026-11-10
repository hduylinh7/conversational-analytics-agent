"""Redis token-bucket rate limiting middleware for the /analyze endpoints.

A single atomic Lua script implements the token bucket: ``RATE_LIMIT_BURST``
is the bucket capacity, refilled continuously at ``RATE_LIMIT_RPM / 60``
tokens per second per client IP. Requests exceeding both the burst and the
sustained rate receive ``429 Too Many Requests`` with a ``Retry-After`` hint.

Fail-open: if Redis is unavailable the request is allowed (the analytics
pipeline must not go down because the limiter cannot reach Redis); the failure
is logged once per episode.
"""

import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Atomic token bucket: HMGET tokens/ts, refill by elapsed time, consume,
# persist state and set a cleanup TTL, then compute Retry-After when denied.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
    tokens = capacity
    ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= requested then
    tokens = tokens - requested
    allowed = 1
else
    retry_after = math.ceil((requested - tokens) / rate)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 1)
return {allowed, retry_after}
"""


class TokenBucketRateLimiter:
    """Token bucket rate limiter enforced atomically inside Redis."""

    def __init__(
        self,
        client: Any,  # noqa: ANN401 - Redis-compatible async client (redis.Redis or fakes)
        *,
        requests_per_minute: int,
        burst: int,
    ) -> None:
        """Configure the sustained rate and burst capacity per client key.

        ``client`` must expose ``eval(script, numkeys, *args)`` as in
        ``redis.asyncio.Redis``; any object with that surface works, which keeps
        unit tests free of a live Redis.
        """
        self.client = client
        self.rate_per_second = requests_per_minute / 60.0
        self.capacity = float(burst)

    async def check(self, key: str) -> tuple[bool, float]:
        """Consume one token for ``key``.

        Returns:
            ``(allowed, retry_after_seconds)`` where ``retry_after_seconds``
            is ``0`` when allowed. Redis failures fail open (request allowed).
        """
        try:
            result = await self.client.eval(
                _TOKEN_BUCKET_LUA,
                1,
                key,
                self.rate_per_second,
                self.capacity,
                time.time(),
                1,
            )
            allowed = int(result[0]) == 1
            retry_after = float(result[1])
            return allowed, retry_after
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Rate limiter unreachable, failing open: %s", exc)
            return True, 0.0


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware applying the token bucket limiter per client IP."""

    def __init__(
        self,
        app: Any,  # noqa: ANN401 - ASGI app passed by add_middleware
        limiter: TokenBucketRateLimiter,
        *,
        path_prefix: str = "/analyze",
    ) -> None:
        """Scope limiting to endpoints sharing ``path_prefix``."""
        super().__init__(app)
        self.limiter = limiter
        self.path_prefix = path_prefix

    @staticmethod
    def _client_ip_forward() -> str:
        """Fallback IP used when the connection peer is None."""
        return "unknown"

    def _client_ip(self, request: Request) -> str:
        """Resolve the client IP, honoring the first X-Forwarded-For hop."""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first_hop = forwarded.split(",")[0].strip()
            if first_hop:
                return first_hop
        client = request.client
        return client.host if client is not None else self._client_ip_forward()

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],  # noqa: ANN401 - ASGI contract
    ) -> Response:
        """Apply the limiter to matching endpoints and reject over-limit calls."""
        if not request.url.path.startswith(self.path_prefix):
            return cast(Response, await call_next(request))

        allowed, retry_after = await self.limiter.check(
            f"analytics:ratelimit:{self._client_ip(request)}"
        )
        if not allowed:
            retry_after = max(1.0, retry_after)
            logger.warning(
                "Rate limit exceeded for %s on %s (retry after %.0fs).",
                self._client_ip(request),
                request.url.path,
                retry_after,
            )
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Please slow down."},
                headers={"Retry-After": str(int(retry_after))},
            )
        return cast(Response, await call_next(request))


def create_rate_limit_limiter() -> TokenBucketRateLimiter | None:
    """Build the production limiter from settings, or ``None`` when disabled."""
    from app.db.redis import get_redis_client

    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return None
    return TokenBucketRateLimiter(
        get_redis_client(),
        requests_per_minute=settings.RATE_LIMIT_RPM,
        burst=settings.RATE_LIMIT_BURST,
    )
