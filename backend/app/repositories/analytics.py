"""AnalyticsRepository: execution of guardrail-sanitized analytical queries against PostgreSQL.

Pipeline: defensive re-validation -> timeout-guarded execution -> capped row
fetching -> JSON-safe value serialization -> ExecutionResult.
"""

import asyncio
import math
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.postgres import engine
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.security.schemas import ValidationResult
from app.security.validator import validate_sql

logger = get_logger(__name__)

_MAX_SERIALIZED_BYTES_LENGTH = 256


def serialize_cell(value: Any) -> Any:  # noqa: PLR0911
    """Convert a database cell to a JSON-serializable primitive."""
    if value is None or isinstance(value, str | int | bool):
        return value

    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, datetime | date | time):
        return value.isoformat()

    if isinstance(value, uuid.UUID):
        return str(value)

    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value

    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        try:
            decoded = raw.decode("utf-8")
            if len(decoded) > _MAX_SERIALIZED_BYTES_LENGTH:
                return decoded[:_MAX_SERIALIZED_BYTES_LENGTH] + "..."
            return decoded
        except UnicodeDecodeError:
            return raw[:_MAX_SERIALIZED_BYTES_LENGTH].hex()

    return str(value)


def _sqlstate_code(exc: SQLAlchemyError) -> tuple[str | None, str]:
    """Extract (sqlstate, message) from a SQLAlchemy wrapped driver error."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "code", None) or getattr(orig, "sqlstate", None)
    message = str(orig) if orig is not None else str(exc)
    return (code, message)


class AnalyticsRepository:
    """Executes validated read-only analytical queries with timeout and row caps."""

    def __init__(self) -> None:
        """Initialize repository with application execution settings."""
        settings = get_settings()
        self.execution_timeout = settings.SQL_EXECUTION_TIMEOUT
        self.max_rows = settings.SQL_MAX_ROWS

    async def execute(self, sql: str, *, revalidate: bool = True) -> ExecutionResult:
        """Run a SELECT-only analytical query with defensive re-validation.

        Args:
            sql: SQL statement to execute, expected to have passed guardrails upstream.
            revalidate: If True, re-run AST guardrail validation before executing.

        Returns:
            ExecutionResult with rows and metrics, or a typed failure report.
        """
        limit_applied: int | None = None

        if revalidate:
            validation: ValidationResult = validate_sql(sql)
            if not validation.is_valid or validation.sanitized_sql is None:
                messages = [v.message for v in validation.violations]
                logger.warning("Execution rejected: re-validation failed: %s", messages)
                return ExecutionResult(
                    status=ExecutionStatus.VALIDATION_FAILED,
                    sql=sql,
                    limit_applied=validation.applied_limit,
                    error_code="VALIDATION_FAILED",
                    error_message=(
                        "; ".join(messages) or "Query failed guardrail re-validation."
                    ),
                )
            sql = validation.sanitized_sql
            limit_applied = validation.applied_limit

        # effective_cap + 1 rows are fetched inside _execute_with_timeout to detect truncation
        effective_cap = min(limit_applied, self.max_rows) if limit_applied else self.max_rows

        started = asyncio.get_running_loop().time()
        result = await self._execute_with_timeout(sql, effective_cap, limit_applied)
        result.execution_ms = (asyncio.get_running_loop().time() - started) * 1000.0
        return result

    async def _execute_with_timeout(
        self, sql: str, effective_cap: int, limit_applied: int | None
    ) -> ExecutionResult:
        """Acquire a connection and execute with a hard wall-clock timeout.

        Fetches effective_cap + 1 rows to distinguish truncation from an exact result.
        """
        fetch_count = effective_cap + 1
        try:
            async with asyncio.timeout(self.execution_timeout):
                async with engine.connect() as conn:
                    cursor = await conn.execute(text(sql))
                    keys = list(cursor.keys())
                    data = cursor.fetchmany(fetch_count) if cursor.returns_rows else []
        except TimeoutError:
            logger.warning(
                "Query execution exceeded %.1fs timeout: %s", self.execution_timeout, sql
            )
            return ExecutionResult(
                status=ExecutionStatus.TIMEOUT,
                sql=sql,
                limit_applied=limit_applied,
                error_code="EXECUTION_TIMEOUT",
                error_message=(
                    f"Query exceeded the {self.execution_timeout}s execution timeout. "
                    "Consider narrowing filters or reducing aggregation scope."
                ),
            )
        except asyncio.CancelledError:
            logger.warning("Query execution was cancelled: %s", sql)
            return ExecutionResult(
                status=ExecutionStatus.CANCELLED,
                sql=sql,
                limit_applied=limit_applied,
                error_code="EXECUTION_CANCELLED",
                error_message="Query execution was cancelled.",
            )
        except SQLAlchemyError as exc:
            sqlstate, message = _sqlstate_code(exc)
            logger.error("Query execution failed [SQLSTATE=%s]: %s", sqlstate, message)
            return ExecutionResult(
                status=ExecutionStatus.DATABASE_ERROR,
                sql=sql,
                limit_applied=limit_applied,
                error_code=sqlstate or "DATABASE_ERROR",
                error_message=message,
            )

        truncated = len(data) >= fetch_count
        rows = [[serialize_cell(cell) for cell in row] for row in data[:effective_cap]]

        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            sql=sql,
            columns=keys,
            rows=rows,
            row_count=len(rows),
            total_rows_fetched=len(data),
            truncated=truncated,
            limit_applied=limit_applied,
        )


_global_repository: AnalyticsRepository | None = None


def get_analytics_repository() -> AnalyticsRepository:
    """Return a singleton AnalyticsRepository instance."""
    global _global_repository
    if _global_repository is None:
        _global_repository = AnalyticsRepository()
    return _global_repository


async def execute_analytics_query(sql: str, *, revalidate: bool = True) -> ExecutionResult:
    """Convenience functional interface to execute a validated analytical query."""
    return await get_analytics_repository().execute(sql, revalidate=revalidate)
