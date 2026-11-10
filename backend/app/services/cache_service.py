"""CacheService: Redis-backed prompt cache and SQL result cache.

Fail-open design: every Redis operation is wrapped so that a Redis outage only
degrades cache behavior (clients fall through to the normal pipeline) and never
breaks the analytics workflow.

Cached payloads:

- **Prompt cache**: the raw LLM response text for a prompt digest. Only SQL
  that passed guardrail validation is committed to this cache (the write happens
  in the ``validate`` graph node), so cached candidates are always sanitary.
  Keys embed the semantic layer digest so YAML edits invalidate the cache.
- **Result cache**: serialised successful ``ExecutionResult`` payloads keyed by
  the sanitized SQL, so identical queries skip PostgreSQL within the TTL.
"""

import hashlib
import json
from typing import Any, cast

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.schemas import ExecutionResult
from app.semantic.loader import get_semantic_version

logger = get_logger(__name__)

PROMPT_KEY_PREFIX = "analytics:cache:prompt:"
RESULT_KEY_PREFIX = "analytics:cache:result:"

# Redis-compatible async client alias: production passes ``redis.asyncio.Redis``
# (from ``app.db.redis``); tests may pass any object with ``get``/``set``.
CacheClient = Any


def _digest(payload: str) -> str:
    """Return the SHA-256 hex digest of a string payload."""
    return hashlib.sha256(payload.encode()).hexdigest()


class CacheService:
    """Thin, fail-open Redis wrapper exposing the prompt and result caches.

    A Redis-compatible async client is injected (from ``app.db.redis``),
    keeping this class decoupled from connection management and easy to fake
    in tests.
    """

    def __init__(
        self,
        client: CacheClient,
        *,
        prompt_ttl_seconds: int | None = None,
        result_ttl_seconds: int | None = None,
    ) -> None:
        """Wire the cache to a Redis-compatible client and TTL configuration."""
        settings = get_settings()
        self.client = client
        self.prompt_ttl = prompt_ttl_seconds or settings.PROMPT_CACHE_TTL_SECONDS
        self.result_ttl = result_ttl_seconds or settings.RESULT_CACHE_TTL_SECONDS

    # ------------------------------------------------------------------
    # Prompt cache (raw LLM response text keyed by generation inputs)
    # ------------------------------------------------------------------

    def prompt_key(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None,
        temperature: float,
    ) -> str:
        """Build the prompt cache key from the exact generation inputs.

        The digest covers the full LLM conversation (question, retrieved
        context, repair feedback, multi-turn history) plus sampling parameters
        and the semantic layer version, so any input change yields a new key.
        """
        payload = json.dumps(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "semantic_version": get_semantic_version(),
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"{PROMPT_KEY_PREFIX}{_digest(payload)}"

    async def get_prompt(self, key: str) -> str | None:
        """Return the cached LLM response text for ``key`` or ``None`` on miss/failure."""
        try:
            raw = await self.client.get(key)
            return cast(str | None, raw)
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Prompt cache get failed for %s: %s", key, exc)
            return None

    async def commit_prompt(self, key: str, response_text: str) -> None:
        """Best-effort store of an LLM response with the prompt TTL."""
        try:
            await self.client.set(key, response_text, ex=self.prompt_ttl)
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Prompt cache set failed for %s: %s", key, exc)

    # ------------------------------------------------------------------
    # Result cache (serialised ExecutionResult keyed by sanitized SQL)
    # ------------------------------------------------------------------

    def result_key(self, sql: str) -> str:
        """Build the result cache key from normalized sanitized SQL."""
        return f"{RESULT_KEY_PREFIX}{_digest(sql.strip())}"

    async def get_result(self, sql: str) -> ExecutionResult | None:
        """Return the cached successful ExecutionResult for SQL or ``None``."""
        key = self.result_key(sql)
        try:
            raw = await self.client.get(key)
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Result cache get failed for %s: %s", key, exc)
            return None
        if raw is None:
            return None
        try:
            parsed = json.loads(cast(str, raw))
            if not isinstance(parsed, dict):
                raise ValueError("not a JSON object")
            return ExecutionResult.model_validate(parsed)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Result cache payload for %s is invalid; ignoring: %s", key, exc)
            return None

    async def commit_result(self, sql: str, execution: ExecutionResult) -> None:
        """Best-effort store of a successful execution report with the result TTL."""
        key = self.result_key(sql)
        try:
            payload = json.dumps(
                execution.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Result for %s is not serializable; skipping cache: %s", key, exc)
            return
        try:
            await self.client.set(key, payload, ex=self.result_ttl)
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Result cache set failed for %s: %s", key, exc)
