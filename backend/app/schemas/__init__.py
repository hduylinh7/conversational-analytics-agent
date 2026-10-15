"""Pydantic request and response schemas for API validation and serialization.

This package defines data validation models, DTOs, and serialization schemas.
Future additions:
- QueryRequest / QueryResponse (natural language questions)
- SqlExecutionResult
- ClarificationRequest
"""

from app.schemas.health import DependencyStatus, HealthResponse, ReadinessResponse
from app.schemas.semantic import (
    DataSourcesCatalog,
    DimensionMetadata,
    DimensionsCatalog,
    JoinStep,
    MetricMetadata,
    MetricsCatalog,
    ResolvedJoinPath,
    RetrievalConfig,
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
    SelectionExplanation,
    SemanticCatalog,
)

__all__ = [
    "HealthResponse",
    "ReadinessResponse",
    "DependencyStatus",
    "DataSourcesCatalog",
    "DimensionMetadata",
    "DimensionsCatalog",
    "MetricMetadata",
    "MetricsCatalog",
    "SemanticCatalog",
    "RetrievalStatus",
    "RetrievalConfig",
    "RetrievalResult",
    "SelectedMetric",
    "SelectedDimension",
    "SelectionExplanation",
    "JoinStep",
    "ResolvedJoinPath",
]

