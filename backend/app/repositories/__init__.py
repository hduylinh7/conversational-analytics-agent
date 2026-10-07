"""Repositories module: Data access layer and database persistence.

Intended future responsibilities:
- AnalyticsRepository: Direct execution of sanitized analytical queries against PostgreSQL.
- AuditRepository: Store user session questions, generated SQL, execution metrics, and logs.
- SchemaRepository: Query PostgreSQL information schema, table definitions, and column metadata.
"""

__all__: list[str] = []
