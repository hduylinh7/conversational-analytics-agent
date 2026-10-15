"""Semantic Retrieval package providing hybrid metadata search, ranking, and join resolution."""

from app.semantic.retrieval.embedding import (
    BaseEmbeddingProvider,
    EmbeddingEngine,
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
)
from app.semantic.retrieval.join_resolver import JoinResolver
from app.semantic.retrieval.keyword import KeywordEngine
from app.semantic.retrieval.normalizer import (
    extract_ngrams,
    normalize_text,
    stem_token,
    strip_vietnamese_diacritics,
    tokenize,
)
from app.semantic.retrieval.retriever import (
    SemanticRetriever,
    get_semantic_retriever,
    retrieve,
)
from app.semantic.retrieval.schemas import (
    JoinStep,
    ResolvedJoinPath,
    RetrievalConfig,
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
    SelectionExplanation,
)

__all__ = [
    # Main retrieval interface & engine
    "SemanticRetriever",
    "get_semantic_retriever",
    "retrieve",
    # Schemas & DTOs
    "RetrievalStatus",
    "RetrievalConfig",
    "RetrievalResult",
    "SelectedMetric",
    "SelectedDimension",
    "SelectionExplanation",
    "JoinStep",
    "ResolvedJoinPath",
    # Sub-engines
    "KeywordEngine",
    "EmbeddingEngine",
    "JoinResolver",
    # Embedding Providers
    "BaseEmbeddingProvider",
    "MockEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    # Normalizer utilities
    "normalize_text",
    "strip_vietnamese_diacritics",
    "tokenize",
    "stem_token",
    "extract_ngrams",
]
