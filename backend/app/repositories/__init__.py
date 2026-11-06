"""Repositories module: Data access layer and database persistence.

Intended future responsibilities:
- AnalyticsRepository: Direct execution of sanitized analytical queries against PostgreSQL.
- AuditRepository: Store user session questions, generated SQL, execution metrics, and logs.
- SchemaRepository: Query PostgreSQL information schema, table definitions, and column metadata.
"""

from app.repositories.analytics import (
    AnalyticsRepository,
    execute_analytics_query,
    get_analytics_repository,
)
from app.repositories.schemas import ExecutionResult, ExecutionStatus

__all__: list[str] = [
    "AnalyticsRepository",
    "ExecutionResult",
    "ExecutionStatus",
    "execute_analytics_query",
    "get_analytics_repository",
]
