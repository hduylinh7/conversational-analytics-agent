"""Pydantic schemas and DTOs for sanitized SQL execution against PostgreSQL."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionStatus(StrEnum):
    """Outcome classification of a query execution attempt."""

    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    DATABASE_ERROR = "DATABASE_ERROR"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CANCELLED = "CANCELLED"


class ExecutionResult(BaseModel):
    """Structured report containing fetched rows, column metadata, and execution metrics."""

    status: ExecutionStatus = Field(
        ...,
        description="Final outcome of the execution attempt.",
    )
    sql: str = Field(
        ...,
        description="The SQL statement that was executed (sanitized if validation passed).",
    )
    columns: list[str] = Field(
        default_factory=list,
        description="Result column names ordered as returned by the database.",
    )
    rows: list[list[Any]] = Field(
        default_factory=list,
        description="Result rows as JSON-serializable arrays of values.",
    )
    row_count: int = Field(
        default=0,
        ge=0,
        description="Number of rows actually returned to the caller (after row cap).",
    )
    total_rows_fetched: int = Field(
        default=0,
        ge=0,
        description="Number of rows fetched before applying the hard row cap.",
    )
    truncated: bool = Field(
        default=False,
        description="True if the database returned more rows than the execution row cap.",
    )
    execution_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock execution duration in milliseconds.",
    )
    limit_applied: int | None = Field(
        default=None,
        description="ROW LIMIT value enforced in the sanitized SQL, if any.",
    )
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code or PostgreSQL SQLSTATE (TIMEOUT, 42703, ...).",
    )
    error_message: str | None = Field(
        default=None,
        description="Human-readable error description for debugging and LLM repair feedback.",
    )

    @property
    def is_success(self) -> bool:
        """True if the query executed and returned results without error."""
        return self.status == ExecutionStatus.SUCCESS

    @property
    def is_repairable(self) -> bool:
        """True if a failed result can be fed back into the LLM self-repair loop.

        Validation failures and database errors (undefined table/column, syntax
        errors, timeouts) are recoverable by regenerating SQL. Cancelled queries
        and non-retryable infrastructure failures are not.
        """
        return self.status in (ExecutionStatus.VALIDATION_FAILED, ExecutionStatus.DATABASE_ERROR, ExecutionStatus.TIMEOUT)
