"""Pydantic schemas and data transfer objects for SQL Guardrails and AST Validation."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ViolationSeverity(StrEnum):
    """Severity classification for guardrail violations."""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ViolationRule(StrEnum):
    """Categorical rules enforced by the SQL AST Guardrails."""

    SYNTAX_ERROR = "SYNTAX_ERROR"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    SELECT_ONLY = "SELECT_ONLY"
    DISALLOWED_TABLE = "DISALLOWED_TABLE"
    DISALLOWED_COLUMN = "DISALLOWED_COLUMN"
    DANGEROUS_FUNCTION = "DANGEROUS_FUNCTION"
    DISALLOWED_OPERATION = "DISALLOWED_OPERATION"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"

    # Business intent conformance rules (SQL candidate vs. retrieval contract).
    INTENT_MISSING_FILTER = "INTENT_MISSING_FILTER"
    INTENT_METRIC_MISMATCH = "INTENT_METRIC_MISMATCH"
    INTENT_REQUIRED_TABLE_MISSING = "INTENT_REQUIRED_TABLE_MISSING"


class SecurityViolation(BaseModel):
    """Details of a single security or integrity rule violation."""

    model_config = ConfigDict(frozen=True)

    rule: ViolationRule | str = Field(
        ...,
        description="The rule code that was violated.",
    )
    message: str = Field(
        ...,
        description="Human-readable explanation of why the rule was violated.",
    )
    severity: ViolationSeverity = Field(
        default=ViolationSeverity.HIGH,
        description="Severity level of the violation.",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Structured contextual information (e.g. offending table or function name).",
    )


class GuardrailConfig(BaseModel):
    """Runtime configuration for SQL Guardrail validation and sanitization."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_limit: int = Field(
        default=1000,
        description="Maximum permitted row LIMIT in analytical queries.",
        ge=1,
    )
    default_limit: int = Field(
        default=100,
        description="Default row LIMIT injected when no limit clause is present.",
        ge=1,
    )
    enforce_limit: bool = Field(
        default=True,
        description="Whether to enforce and inject row limits.",
    )
    default_schema: str = Field(
        default="business",
        description="Default PostgreSQL schema name for unqualified table references.",
    )
    allowed_tables: set[str] | None = Field(
        default=None,
        description="Whitelist of schema-qualified table names (e.g. 'business.orders'). If None, derived from catalog.",
    )
    allowed_columns: set[str] | None = Field(
        default=None,
        description="Optional whitelist of allowed column names. If None, column allowlisting is relaxed.",
    )
    disallowed_functions: set[str] = Field(
        default_factory=lambda: {
            # File system access
            "pg_read_file",
            "pg_read_binary_file",
            "pg_write_file",
            "pg_write_binary_file",
            "lo_import",
            "lo_export",
            "copy",
            # Denial of service / Process control
            "pg_sleep",
            "pg_sleep_for",
            "pg_sleep_until",
            "pg_terminate_backend",
            "pg_cancel_backend",
            "pg_reload_conf",
            "pg_rotate_logfile",
            # Remote / External execution
            "dblink",
            "dblink_exec",
            "dblink_connect",
            "dblink_send_query",
            "system",
            "sh",
            "cmd",
            "eval",
            # Settings / Admin manipulation
            "set_config",
            "current_setting",
            "alter_system",
            "version",
            "currentversion",
            "query_to_xml",
            "pg_tablespace_size",
        },
        description="Set of disallowed lowercased function names.",
    )
    disallowed_schemas: set[str] = Field(
        default_factory=lambda: {
            "pg_catalog",
            "information_schema",
            "pg_toast",
        },
        description="System schemas strictly barred from direct user queries.",
    )
    allow_ctes: bool = Field(
        default=True,
        description="Whether Common Table Expressions (WITH clauses) are permitted.",
    )
    enforce_intent_checks: bool = Field(
        default=True,
        description="Whether business intent conformance checks (SQL vs. retrieval contract) run when retrieval context is provided.",
    )
    intent_strict_mode: bool = Field(
        default=False,
        description="When True, AMBIGUOUS intent conformance failures (METRIC_MISMATCH, REQUIRED_TABLE_MISSING) block execution instead of warning.",
    )
    dialect: str = Field(
        default="postgres",
        description="Target SQL dialect for sqlglot parsing and transpilation.",
    )


class ValidationResult(BaseModel):
    """Structured report returned after inspecting and sanitizing a SQL query."""

    is_valid: bool = Field(
        ...,
        description="True if the query passes all security and integrity checks.",
    )
    original_sql: str = Field(
        ...,
        description="The raw input SQL query.",
    )
    sanitized_sql: str | None = Field(
        default=None,
        description="The safe, canonical SQL query with enforced LIMIT if valid, None if invalid.",
    )
    violations: list[SecurityViolation] = Field(
        default_factory=list,
        description="List of security or policy violations detected.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Non-blocking caveats, advice, or notices (e.g. limit clamping).",
    )
    tables_referenced: list[str] = Field(
        default_factory=list,
        description="Deduplicated list of fully-qualified table names referenced in the query.",
    )
    columns_referenced: list[str] = Field(
        default_factory=list,
        description="Deduplicated list of column names extracted from the query AST.",
    )
    original_limit: int | None = Field(
        default=None,
        description="The original row limit parsed from the query, if any.",
    )
    applied_limit: int | None = Field(
        default=None,
        description="The final row limit enforced in the sanitized query.",
    )
    has_cte: bool = Field(
        default=False,
        description="Whether the query utilizes Common Table Expressions (WITH clauses).",
    )
