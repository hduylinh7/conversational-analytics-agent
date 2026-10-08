"""Pytest fixtures for data validation tests."""

from pathlib import Path
import sys

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from scripts.profile_data import DataProfiler
from scripts.validate_integrity import IntegrityValidator

settings = get_settings()

# Engine using NullPool to avoid event-loop mismatch across async tests
nullpool_engine: AsyncEngine = create_async_engine(
    settings.database_url,
    poolclass=NullPool,
)


@pytest.fixture
def test_engine() -> AsyncEngine:
    """Provide an AsyncEngine with NullPool for test isolation."""
    return nullpool_engine


@pytest.fixture
def profiler() -> DataProfiler:
    """Provide DataProfiler using the NullPool test engine."""
    return DataProfiler(db_engine=nullpool_engine)


@pytest.fixture
def validator() -> IntegrityValidator:
    """Provide IntegrityValidator using the NullPool test engine."""
    return IntegrityValidator(db_engine=nullpool_engine)
