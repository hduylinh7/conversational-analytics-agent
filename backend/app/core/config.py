from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
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
