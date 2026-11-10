from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # General App Settings
    APP_NAME: str = Field(default="Conversational Analytics Agent")
    APP_ENV: Literal["development", "testing", "production"] = Field(default="development")
    LOG_LEVEL: str = Field(default="INFO")

    # PostgreSQL Database Configuration
    POSTGRES_HOST: str = Field(default="postgres")
    POSTGRES_PORT: int = Field(default=5432)
    POSTGRES_DB: str = Field(default="analytics")
    POSTGRES_USER: str = Field(default="analytics")
    POSTGRES_PASSWORD: str = Field(default="analytics")

    # Redis Configuration
    REDIS_HOST: str = Field(default="redis")
    REDIS_PORT: int = Field(default=6379)

    # Backend Network Configuration
    BACKEND_HOST: str = Field(default="0.0.0.0")
    BACKEND_PORT: int = Field(default=8000)

    # Frontend URL (for CORS)
    FRONTEND_URL: str = Field(default="http://localhost:3000")

    # Semantic Layer & Retrieval Configuration
    SEMANTIC_LAYER_DIR: str | None = Field(
        default=None,
        description="Optional custom directory for semantic layer YAML files.",
    )
    RETRIEVAL_KEYWORD_WEIGHT: float = Field(
        default=0.6,
        description="Weight of keyword matching in hybrid retrieval (0.0 to 1.0).",
    )
    RETRIEVAL_EMBEDDING_WEIGHT: float = Field(
        default=0.4,
        description="Weight of embedding similarity in hybrid retrieval (0.0 to 1.0).",
    )
    RETRIEVAL_ENABLE_EMBEDDINGS: bool = Field(
        default=False,
        description="Whether embedding similarity search is enabled.",
    )
    RETRIEVAL_TOP_K_METRICS: int = Field(
        default=3,
        description="Default maximum candidate metrics to return.",
    )
    RETRIEVAL_TOP_K_DIMENSIONS: int = Field(
        default=5,
        description="Default maximum candidate dimensions to return.",
    )
    RETRIEVAL_MIN_CONFIDENCE_SCORE: float = Field(
        default=0.25,
        description="Minimum score threshold for candidate selection.",
    )
    OPENAI_API_KEY: str | None = Field(
        default=None,
        description="API key for optional external embedding provider.",
    )
    EMBEDDING_MODEL: str = Field(
        default="text-embedding-3-small",
        description="Embedding model name.",
    )

    # SQL Security & Guardrail Configuration
    SQL_MAX_LIMIT: int = Field(
        default=1000,
        description="Maximum row LIMIT allowed in generated SQL queries.",
    )
    SQL_DEFAULT_LIMIT: int = Field(
        default=100,
        description="Default row LIMIT injected into generated SQL queries when none is provided.",
    )
    SQL_ENFORCE_LIMIT: bool = Field(
        default=True,
        description="Whether to enforce row limits on all generated SQL queries.",
    )
    SQL_ENFORCE_INTENT_CHECKS: bool = Field(
        default=True,
        description="Whether business intent conformance checks (SQL vs. retrieval contract) run on generated SQL.",
    )
    SQL_INTENT_STRICT_MODE: bool = Field(
        default=False,
        description="When True, ambiguous intent conformance failures (metric mismatch, missing required table) block execution instead of warning.",
    )

    # LLM & Text-to-SQL Generation Configuration
    OPENROUTER_API_KEY: str | None = Field(
        default=None,
        description="API key for OpenRouter LLM gateway.",
    )
    GROQ_API_KEY: str | None = Field(
        default=None,
        description="API key for Groq LLM inference platform.",
    )
    GROQ_BASE_URL: str = Field(
        default="https://api.groq.com/openai/v1",
        description="Base URL for Groq OpenAI-compatible API.",
    )
    LLM_MODEL: str = Field(
        default="google/gemini-2.5-flash",
        description="Model identifier for Text-to-SQL generation.",
    )
    LLM_PROVIDER: str = Field(
        default="openrouter",
        description="LLM provider: 'openrouter', 'groq', 'openai', or 'mock'.",
    )
    LLM_BASE_URL: str = Field(
        default="https://openrouter.ai/api/v1",
        description="Base URL for OpenRouter / OpenAI API.",
    )
    LLM_TEMPERATURE: float = Field(
        default=0.0,
        description="Temperature for deterministic SQL generation.",
    )
    SQL_MAX_REPAIR_ATTEMPTS: int = Field(
        default=3,
        description="Maximum self-repair iteration attempts when validation fails.",
    )
    SQL_MAX_EXECUTION_REPAIR_ATTEMPTS: int = Field(
        default=2,
        description="Maximum self-repair iteration attempts when PostgreSQL execution fails "
        "(TIMEOUT, DATABASE_ERROR). Shares the chat history with the generation loop.",
    )

    # LangGraph Orchestration Configuration
    GRAPH_RECURSION_LIMIT: int = Field(
        default=50,
        ge=10,
        description="Hard cap on LangGraph supersteps, preventing runaway orchestration loops.",
    )

    # LangGraph Checkpointing Configuration (multi-turn conversation memory)
    CHECKPOINTER_BACKEND: Literal["memory", "redis"] = Field(
        default="memory",
        description=(
            "LangGraph checkpointer backend: 'memory' (per-process, dev/testing) "
            "or 'redis' (persistent across restarts, shared by replicas)."
        ),
    )

    # SSE Streaming Configuration
    SSE_HEARTBEAT_INTERVAL_SECONDS: float = Field(
        default=15.0,
        ge=1.0,
        description=(
            "Interval in seconds between SSE keep-alive comments on /analyze/stream "
            "while the pipeline is silent (long LLM calls), defeating proxy timeouts."
        ),
    )

    # Redis Caching Configuration (prompt cache + SQL result cache)
    CACHE_ENABLED: bool = Field(
        default=True,
        description="Master switch for Redis-backed prompt cache and SQL result cache.",
    )
    PROMPT_CACHE_TTL_SECONDS: int = Field(
        default=3600,
        ge=1,
        description="TTL in seconds for cached (validated) generated SQL keyed by prompt digest.",
    )
    RESULT_CACHE_TTL_SECONDS: int = Field(
        default=900,
        ge=1,
        description="TTL in seconds for cached successful SQL execution results.",
    )

    # Semantic Cache Configuration (question-embedding similarity cache)
    SEMANTIC_CACHE_ENABLED: bool = Field(
        default=True,
        description=(
            "Enable question-embedding semantic cache: embed first-turn questions and "
            "replay validated SQL for near-identical questions. Requires OPENAI_API_KEY; "
            "degrades silently to exact-match caching when unavailable."
        ),
    )
    SEMANTIC_CACHE_THRESHOLD: float = Field(
        default=0.95,
        ge=0.5,
        le=1.0,
        description="Minimum raw cosine similarity between question embeddings to replay cached SQL.",
    )
    SEMANTIC_CACHE_TTL_SECONDS: int = Field(
        default=86400,
        ge=1,
        description="TTL in seconds for semantic cache entries.",
    )
    SEMANTIC_CACHE_MAX_ENTRIES: int = Field(
        default=500,
        ge=1,
        description="Maximum semantic cache entries kept per (model, semantic version); oldest evicted.",
    )

    # Rate Limiting Configuration (Redis token bucket on /analyze endpoints)
    RATE_LIMIT_ENABLED: bool = Field(
        default=True,
        description="Whether Redis-backed token bucket rate limiting middleware is active.",
    )
    RATE_LIMIT_RPM: int = Field(
        default=30,
        ge=1,
        description="Sustained requests per minute allowed per client IP on /analyze endpoints.",
    )
    RATE_LIMIT_BURST: int = Field(
        default=10,
        ge=1,
        description="Max burst size (token bucket capacity) permitted per client IP.",
    )

    # SQL Execution Configuration
    SQL_EXECUTION_TIMEOUT: float = Field(
        default=10.0,
        description="Maximum seconds a validated analytical query may run before cancellation.",
        gt=0.0,
    )
    SQL_MAX_ROWS: int = Field(
        default=5000,
        description="Hard cap on fetched result rows, independent of the SQL LIMIT clause.",
        ge=1,
    )

    @property
    def database_url(self) -> str:
        """Construct the async SQLAlchemy PostgreSQL connection string."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_url(self) -> str:
        """Construct the Redis connection URL."""
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()
