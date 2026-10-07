"""Services module: Business and application domain logic.

Intended future responsibilities:
- AnalyticsService: Coordinate agent runs, query execution, and cache hits.
- SemanticService: Manage metric definitions, business dimensions, and semantic mappings.
- ExecutionService: Read-only database query execution with timeout management.
- CacheService: Cache SQL results and schema lookups in Redis.
- AuditService: Record user interactions, generated SQL, execution times, and errors.
"""

__all__: list[str] = []
