"""SemanticCacheService: question-embedding similarity cache over Redis.

Purpose: users rarely repeat a question verbatim ("revenue by category" vs
"revenue per category"), so exact-key caching misses. This service embeds
first-turn questions and matches them against cached entries; when a cached
question is sufficiently similar (raw cosine >= threshold), its validated SQL
is replayed instead of an LLM call.

Storage (plain Redis, no modules required):

- One HASH per entry at ``analytics:scache:<id>`` holding question, model,
  semantic version, validated SQL, and the question embedding as a JSON list
  of floats (the shared pool uses ``decode_responses=True``).
- A ZSET index at ``analytics:scache:index`` scores entry ids by creation
  time so lookups scan newest-first and eviction trims the oldest entries
  beyond ``max_entries``.

All Redis/embedding failures fail open: the caller simply proceeds with a
normal LLM generation. Lookups are O(cached entries) cosine computations in
Python; if the corpus ever outgrows ``SEMANTIC_CACHE_MAX_ENTRIES`` meaningfully,
swap the scan for RediSearch ``FT.SEARCH`` (redis-stack) behind this interface.
"""

import asyncio
import json
import math
import time
import uuid
from typing import Any, Protocol

from app.core.config import get_settings
from app.core.logging import get_logger
from app.semantic.loader import get_semantic_version
from app.semantic.retrieval.embedding import BaseEmbeddingProvider

logger = get_logger(__name__)

SEMANTIC_CACHE_PREFIX = "analytics:scache:"
SEMANTIC_CACHE_INDEX_KEY = "analytics:scache:index"

ENTRY_QUESTION = "question"
ENTRY_MODEL = "model"
ENTRY_VERSION = "semantic_version"
ENTRY_SQL = "sql"
ENTRY_VECTOR = "vector"


class SemanticCacheClient(Protocol):
    """Structural subset of the async Redis client used by this service."""

    async def hset(self, name: str, key: str | None = None, value: str | None = None, mapping: dict | None = None) -> Any:  # noqa: ANN401
        """Set hash fields."""

    async def hgetall(self, name: str) -> dict:  # noqa: ANN401
        """Return all fields of a hash."""

    async def expire(self, key: str, seconds: int) -> Any:  # noqa: ANN401
        """Set an expiry on a key."""

    async def zadd(self, key: str, mapping: dict[str, float]) -> Any:  # noqa: ANN401
        """Add members scored by numeric value to a sorted set."""

    async def zrevrange(self, key: str, start: int, stop: int) -> list:  # noqa: ANN401
        """Return members of a sorted set, newest (highest score) first."""

    async def zremrangebyrank(self, key: str, start: int, stop: int) -> Any:  # noqa: ANN401
        """Remove members by rank (used to trim the oldest entries)."""

    async def zcard(self, key: str) -> Any:  # noqa: ANN401
        """Return the number of members in a sorted set."""

    async def delete(self, key: str) -> Any:  # noqa: ANN401
        """Delete a key."""


def raw_cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Standard cosine similarity in [-1.0, 1.0] without score rescaling.

    (``embedding.cosine_similarity`` rescales into [0, 1] for retrieval scoring,
    which would silently change the meaning of the similarity threshold.)
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(a * a for a in v2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return dot / (norm1 * norm2)


class SemanticCacheService:
    """Fail-open question-similarity cache storing validated SQL in Redis."""

    def __init__(
        self,
        client: Any,  # noqa: ANN401 - Redis-compatible async client (redis.asyncio.Redis or fakes)
        provider: BaseEmbeddingProvider,
        *,
        threshold: float | None = None,
        ttl_seconds: int | None = None,
        max_entries: int | None = None,
    ) -> None:
        """Wire the cache to a Redis client and an embedding provider."""
        settings = get_settings()
        self.client = client
        self.provider = provider
        self.threshold = threshold or settings.SEMANTIC_CACHE_THRESHOLD
        self.ttl = ttl_seconds or settings.SEMANTIC_CACHE_TTL_SECONDS
        self.max_entries = max_entries or settings.SEMANTIC_CACHE_MAX_ENTRIES
        # Monotonic index score so equal-clock stores still evict oldest-first.
        self._last_score: float = 0.0

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, question: str) -> list[float] | None:
        """Embed a question, or return ``None`` when the provider fails.

        Providers are synchronous (httpx under the hood), so the call runs in a
        worker thread to avoid blocking the event loop.
        """
        if not question.strip():
            return None
        try:
            vector = await asyncio.to_thread(self.provider.embed_text, question)
            return vector or None
        except Exception as exc:  # noqa: BLE001 - fail open on provider failure
            logger.warning("Semantic cache embedding failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Lookup / store
    # ------------------------------------------------------------------

    async def find_by_vector(
        self,
        vector: list[float],
        *,
        model: str | None,
        semantic_version: str,
    ) -> str | None:
        """Return the validated SQL of the most similar cached question, if any.

        Entries are matched within the same (model, semantic version) pair so
        prompt/model upgrades and semantic layer edits invalidate old SQL.
        """
        try:
            entry_ids = await self.client.zrevrange(SEMANTIC_CACHE_INDEX_KEY, 0, -1)
            best_score = 0.0
            best_sql: str | None = None
            for entry_id in entry_ids:
                entry = await self.client.hgetall(f"{SEMANTIC_CACHE_PREFIX}{entry_id}")
                if not entry or entry.get(ENTRY_VERSION) != semantic_version:
                    continue
                if entry.get(ENTRY_MODEL) != (model or ""):
                    continue
                try:
                    candidate_vector = json.loads(entry.get(ENTRY_VECTOR, "[]"))
                except (json.JSONDecodeError, TypeError):
                    continue
                score = raw_cosine_similarity(vector, candidate_vector)
                if score > best_score and entry.get(ENTRY_SQL):
                    best_score = score
                    best_sql = entry[ENTRY_SQL]

            if best_sql is not None and best_score >= self.threshold:
                logger.info(
                    "Semantic cache hit (similarity=%.4f >= %.2f).", best_score, self.threshold
                )
                return best_sql
            if best_sql is not None:
                logger.info("Semantic cache miss: best similarity %.4f below %.2f.", best_score, self.threshold)
            return None
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Semantic cache lookup failed, continuing without it: %s", exc)
            return None

    async def store_with_vector(
        self,
        vector: list[float],
        *,
        question: str,
        model: str | None,
        semantic_version: str,
        sql: str,
    ) -> None:
        """Best-effort store of a validated SQL with its question embedding."""
        if not sql.strip() or not vector:
            return
        entry_id = uuid.uuid4().hex
        entry_key = f"{SEMANTIC_CACHE_PREFIX}{entry_id}"
        try:
            await self.client.hset(
                entry_key,
                mapping={
                    ENTRY_QUESTION: question,
                    ENTRY_MODEL: model or "",
                    ENTRY_VERSION: semantic_version,
                    ENTRY_SQL: sql,
                    ENTRY_VECTOR: json.dumps(vector),
                },
            )
            await self.client.expire(entry_key, self.ttl)
            score = max(time.time(), self._last_score + 1e-6)
            self._last_score = score
            await self.client.zadd(SEMANTIC_CACHE_INDEX_KEY, {entry_id: score})
            await self._evict_overflow()
        except Exception as exc:  # noqa: BLE001 - fail open on any Redis failure
            logger.warning("Semantic cache store failed: %s", exc)

    async def _evict_overflow(self) -> None:
        """Trim the oldest entries when the cache exceeds ``max_entries``."""
        count = int(await self.client.zcard(SEMANTIC_CACHE_INDEX_KEY) or 0)
        if count <= self.max_entries:
            return
        overflow = count - self.max_entries
        stale_ids = await self.client.zrevrange(SEMANTIC_CACHE_INDEX_KEY, -overflow, -1)
        for stale_id in stale_ids:
            await self.client.delete(f"{SEMANTIC_CACHE_PREFIX}{stale_id}")
        await self.client.zremrangebyrank(SEMANTIC_CACHE_INDEX_KEY, -overflow, -1)


def create_semantic_cache() -> SemanticCacheService | None:
    """Build the production semantic cache, or ``None`` when unavailable.

    Mirrors the retriever's embedding selection: the cache requires a real
    embedding provider (OpenAI). Without an API key it stays disabled and the
    pipeline keeps exact-match caching only.
    """
    from app.db.redis import get_redis_client
    from app.semantic.retrieval.embedding import OpenAIEmbeddingProvider

    settings = get_settings()
    if not settings.SEMANTIC_CACHE_ENABLED or not settings.OPENAI_API_KEY:
        return None
    provider = OpenAIEmbeddingProvider(
        api_key=settings.OPENAI_API_KEY,
        model=settings.EMBEDDING_MODEL,
    )
    return SemanticCacheService(get_redis_client(), provider)


def get_semantic_version_for_cache() -> str:
    """Expose the semantic layer digest used in cache keys."""
    return get_semantic_version()
