"""Tests for AnalyticsRepository: sanitization, serialization, timeout, and error mapping."""

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.db.postgres import engine
from app.repositories.analytics import AnalyticsRepository, serialize_cell
from app.repositories.schemas import ExecutionStatus


@pytest.fixture(autouse=True)
async def dispose_engine() -> AsyncGenerator[None, None]:
    """Dispose the app engine pool so pooled connections cannot leak across event loops."""
    yield
    await engine.dispose()


@pytest.fixture
def repository() -> AnalyticsRepository:
    """Provide a fresh repository instance per test."""
    return AnalyticsRepository()


@pytest.mark.asyncio
async def test_execute_simple_select_success(repository: AnalyticsRepository) -> None:
    """Verify a valid sanitized SELECT returns rows, columns, and timing."""
    result = await repository.execute("SELECT 1 AS one")

    assert result.status == ExecutionStatus.SUCCESS
    assert result.error_code is None
    assert result.columns == ["one"]
    assert result.rows == [[1]]
    assert result.row_count == 1
    assert result.execution_ms >= 0.0


@pytest.mark.asyncio
async def test_execute_rejects_mutating_statement(repository: AnalyticsRepository) -> None:
    """Verify re-validation blocks mutating SQL before database execution."""
    result = await repository.execute("DROP TABLE business.orders")

    assert result.status == ExecutionStatus.VALIDATION_FAILED
    assert result.rows == []


@pytest.mark.asyncio
async def test_execute_rejects_insert_statement(repository: AnalyticsRepository) -> None:
    """Verify re-validation blocks INSERT statements."""
    result = await repository.execute(
        "INSERT INTO app.users (email) VALUES ('x@y.z')"
    )

    assert result.status == ExecutionStatus.VALIDATION_FAILED
    assert result.error_code == "VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_execute_revalidates_and_applies_limit(repository: AnalyticsRepository) -> None:
    """Verify LIMIT injection flows into the execution limit metadata."""
    result = await repository.execute("SELECT 1 AS one LIMIT 5")

    assert result.status == ExecutionStatus.SUCCESS
    assert result.limit_applied == 5


@pytest.mark.asyncio
async def test_truncation_detection_with_small_limit(repository: AnalyticsRepository) -> None:
    """Verify rows beyond the hard row cap are detected but dropped.

    Truncation is driven by the repository row cap, not the SQL LIMIT: the
    sanitized SQL is executed with its LIMIT applied server-side, so the
    database can never return more rows than the LIMIT clause allows.
    """
    repository.max_rows = 2
    result = await repository.execute("SELECT generate_series(1, 5) AS g")

    assert result.status == ExecutionStatus.SUCCESS
    assert result.rows == [[1], [2]]
    assert result.row_count == 2
    assert result.total_rows_fetched == 3
    assert result.truncated is True


@pytest.mark.asyncio
async def test_truncation_false_when_rows_below_limit(repository: AnalyticsRepository) -> None:
    """Verify truncated stays False when the result fits under the row cap."""
    repository.max_rows = 10
    result = await repository.execute("SELECT generate_series(1, 3) AS g")

    assert result.status == ExecutionStatus.SUCCESS
    assert result.row_count == 3
    assert result.truncated is False


@pytest.mark.asyncio
async def test_execution_timeout_maps_to_timeout_status(repository: AnalyticsRepository) -> None:
    """Verify a long-running query is cancelled at the configured timeout."""
    with patch.object(repository, "execution_timeout", 0.5):
        result = await repository.execute("SELECT pg_sleep(5)", revalidate=False)

    assert result.status == ExecutionStatus.TIMEOUT
    assert result.error_code == "EXECUTION_TIMEOUT"
    assert result.error_message is not None
    assert "timeout" in result.error_message.lower()


@pytest.mark.asyncio
async def test_undefined_table_maps_to_database_error(repository: AnalyticsRepository) -> None:
    """Verify a bad table reference surfaces SQLSTATE 42P01."""
    result = await repository.execute(
        "SELECT * FROM business.nonexistent_table_xyz", revalidate=False
    )

    assert result.status == ExecutionStatus.DATABASE_ERROR
    assert result.error_code == "42P01"
    assert result.rows == []


@pytest.mark.asyncio
async def test_undefined_column_maps_to_database_error(repository: AnalyticsRepository) -> None:
    """Verify unknown column reference surfaces SQLSTATE 42703."""
    result = await repository.execute(
        "SELECT nonexistent_col FROM business.orders", revalidate=False
    )

    assert result.status == ExecutionStatus.DATABASE_ERROR
    assert result.error_code == "42703"


def test_serialize_cell_primitives() -> None:
    """Verify JSON-native primitives pass through unchanged."""
    assert serialize_cell(None) is None
    assert serialize_cell(7) == 7
    assert serialize_cell("hello") == "hello"
    assert serialize_cell(True) is True


def test_serialize_cell_decimal() -> None:
    """Verify Decimal converts to float for JSON transport."""
    assert serialize_cell(Decimal("10.55")) == 10.55


def test_serialize_cell_datetime() -> None:
    """Verify datetime converts to ISO 8601 string."""
    stamp = datetime(2026, 1, 1, 12, 30, 45)
    assert serialize_cell(stamp) == "2026-01-01T12:30:45"


def test_serialize_cell_uuid() -> None:
    """Verify UUID converts to canonical string."""
    value = uuid.uuid4()
    assert serialize_cell(value) == str(value)


def test_serialize_cell_special_floats() -> None:
    """Verify NaN and infinity map to null for strict JSON compliance."""
    assert serialize_cell(float("nan")) is None
    assert serialize_cell(float("inf")) is None
    assert serialize_cell(float("-inf")) is None


def test_serialize_cell_bytes() -> None:
    """Verify bytes decode to UTF-8 text."""
    assert serialize_cell(b"hello") == "hello"


def test_serialize_cell_binary_fallback_hex() -> None:
    """Verify non-UTF-8 bytes fall back to hex encoding."""
    assert serialize_cell(b"\xff\xfe\x00\x01") == "fffe0001"


@pytest.mark.asyncio
async def test_execution_ms_is_positive_on_error(repository: AnalyticsRepository) -> None:
    """Verify timing metadata is recorded even on failure paths."""
    result = await repository.execute(
        "SELECT * FROM business.nonexistent_table_xyz", revalidate=False
    )

    assert result.execution_ms >= 0.0
