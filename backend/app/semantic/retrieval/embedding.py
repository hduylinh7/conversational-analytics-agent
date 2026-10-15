"""Embedding-based semantic similarity module with precomputed metadata caching and graceful fallback."""

import hashlib
import math
from abc import ABC, abstractmethod

from app.core.logging import get_logger
from app.semantic.models import DimensionMetadata, MetricMetadata
from app.semantic.retrieval.normalizer import tokenize

logger = get_logger(__name__)



def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Compute cosine similarity between two float vectors.

    Args:
        v1: First vector.
        v2: Second vector.

    Returns:
        Similarity score between 0.0 and 1.0.
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0

    dot = sum(a * b for a, b in zip(v1, v2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in v1))
    norm2 = math.sqrt(sum(b * b for b in v2))

    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0

    sim = dot / (norm1 * norm2)
    # Clamp to [0.0, 1.0] for retrieval scoring
    return max(0.0, min(1.0, (sim + 1.0) / 2.0))


class EmbeddingError(Exception):
    """Raised when an embedding provider fails to compute vectors."""

    pass


class BaseEmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    def embed_text(self, text: str) -> list[float]:
        """Generate a dense embedding vector for a single text string."""
        pass

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate dense embedding vectors for a list of text strings."""
        pass


class MockEmbeddingProvider(BaseEmbeddingProvider):
    """Deterministic, lightweight embedding provider for testing and offline environments.

    Uses token feature hashing to project text into a normalized 64-dimensional vector space,
    with an optional semantic mapping table for custom synonym/paraphrase testing.
    """

    def __init__(self, dim: int = 64, semantic_aliases: dict[str, str] | None = None) -> None:
        self.dim = dim
        self.semantic_aliases = semantic_aliases or {}

    def _text_to_vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        tokens = tokenize(text, remove_stopwords=True, stem=True)
        if not tokens:
            return vec

        # Resolve known semantic aliases (e.g. mapping custom paraphrases to target terms)
        resolved_tokens: list[str] = []
        for t in tokens:
            resolved_tokens.append(self.semantic_aliases.get(t, t))

        for t in resolved_tokens:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dim
            sign = 1.0 if ((h >> 8) & 1) == 0 else -1.0
            vec[idx] += sign

        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0.0:
            vec = [x / norm for x in vec]
        return vec

    def embed_text(self, text: str) -> list[float]:
        return self._text_to_vector(text)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self._text_to_vector(t) for t in texts]


class OpenAIEmbeddingProvider(BaseEmbeddingProvider):
    """OpenAI embedding provider using httpx to call the REST API."""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def embed_text(self, text: str) -> list[float]:
        batch = self.embed_batch([text])
        if not batch:
            raise EmbeddingError("Empty embedding returned from OpenAI API.")
        return batch[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import httpx

        if not self.api_key:
            raise EmbeddingError("OpenAI API key is missing or empty.")

        url = "https://api.openai.com/v1/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "input": texts,
            "model": self.model,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
                data_items = sorted(data["data"], key=lambda x: x["index"])
                return [item["embedding"] for item in data_items]
        except Exception as exc:
            logger.warning("OpenAI embedding API call failed: %s", exc)
            raise EmbeddingError(f"OpenAI embedding API call failed: {exc}") from exc


class EmbeddingEngine:
    """Manages precomputed metadata vector caching, similarity scoring, and graceful degradation."""

    def __init__(
        self,
        provider: BaseEmbeddingProvider | None = None,
        enabled: bool = False,
    ) -> None:
        """Initialize EmbeddingEngine.

        Args:
            provider: Concrete embedding provider instance or None.
            enabled: Whether embedding similarity search is activated.
        """
        self.provider = provider
        self.enabled = enabled and (provider is not None)
        self._metric_vectors: dict[str, list[float]] = {}
        self._dimension_vectors: dict[str, list[float]] = {}
        self._is_indexed = False

    @property
    def is_available(self) -> bool:
        """Return True if embedding provider is configured and enabled."""
        return self.enabled and (self.provider is not None)

    def build_metadata_index(
        self,
        metrics: list[MetricMetadata],
        dimensions: list[DimensionMetadata],
    ) -> None:
        """Precompute and cache embedding vectors for all metrics and dimensions.

        Args:
            metrics: List of defined MetricMetadata.
            dimensions: List of defined DimensionMetadata.
        """
        if not self.is_available or self.provider is None:
            return

        metric_texts: list[str] = []
        metric_names: list[str] = []
        for m in metrics:
            metric_names.append(m.name)
            syns = ", ".join(m.synonyms.all)
            metric_texts.append(f"{m.name}: {m.description}. Synonyms: {syns}")

        dimension_texts: list[str] = []
        dimension_names: list[str] = []
        for d in dimensions:
            dimension_names.append(d.name)
            syns = ", ".join(d.synonyms.all)
            trans = ", ".join(t.name for t in d.transformations)
            dimension_texts.append(
                f"{d.name}: {d.description}. Synonyms: {syns}. Transformations: {trans}"
            )

        try:
            if metric_texts:
                m_vecs = self.provider.embed_batch(metric_texts)
                for name, vec in zip(metric_names, m_vecs, strict=False):
                    self._metric_vectors[name] = vec

            if dimension_texts:
                d_vecs = self.provider.embed_batch(dimension_texts)
                for name, vec in zip(dimension_names, d_vecs, strict=False):
                    self._dimension_vectors[name] = vec

            self._is_indexed = True
            logger.info(
                "Successfully precomputed metadata embeddings for %d metrics and %d dimensions.",
                len(self._metric_vectors),
                len(self._dimension_vectors),
            )
        except Exception as exc:
            logger.warning(
                "Failed to precompute metadata embeddings: %s. Falling back to keyword search.",
                exc,
            )
            self._metric_vectors.clear()
            self._dimension_vectors.clear()
            self._is_indexed = False

    def search(
        self,
        question: str,
    ) -> tuple[dict[str, float], dict[str, float]]:
        """Compute cosine similarity scores for question against precomputed metadata vectors.

        Args:
            question: User natural language analytics question.

        Returns:
            Tuple of (metric_scores, dimension_scores) mapping item_name -> score.
            Returns empty dicts if embeddings are disabled or provider fails.
        """
        if not self.is_available or not self._is_indexed or self.provider is None:
            return {}, {}

        try:
            q_vec = self.provider.embed_text(question)
        except Exception as exc:
            logger.warning("Embedding query failed: %s. Continuing with keyword search.", exc)
            return {}, {}

        metric_scores: dict[str, float] = {}
        for m_name, m_vec in self._metric_vectors.items():
            sim = cosine_similarity(q_vec, m_vec)
            metric_scores[m_name] = round(sim, 4)

        dimension_scores: dict[str, float] = {}
        for d_name, d_vec in self._dimension_vectors.items():
            sim = cosine_similarity(q_vec, d_vec)
            dimension_scores[d_name] = round(sim, 4)

        return metric_scores, dimension_scores
