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
