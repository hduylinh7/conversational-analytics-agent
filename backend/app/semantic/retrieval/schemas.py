"""Pydantic schemas and data structures for Semantic Retrieval results and configurations."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.semantic.models import (
    FilterCondition,
    RelationshipMetadata,
    TransformationMetadata,
)


class RetrievalStatus(str, Enum):
    """Execution status and sufficiency of retrieved metadata."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_METADATA = "INSUFFICIENT_METADATA"
    AMBIGUOUS = "AMBIGUOUS"
    UNSAFE_JOIN = "UNSAFE_JOIN"


class SelectionExplanation(BaseModel):
    """Detailed justification for selecting a metric or dimension candidate."""

    model_config = ConfigDict(extra="forbid")

    target_type: Literal["metric", "dimension"] = Field(
        ..., description="Type of semantic candidate."
    )
    target_name: str = Field(..., description="Unique name of selected metric or dimension.")
    matched_field: str = Field(
        ..., description="Field that matched: identifier, exact_synonym, partial_synonym, description, or embedding."
    )
    matched_term: str = Field(..., description="Specific term, phrase, or keyword matched.")
    keyword_score: float = Field(default=0.0, ge=0.0, le=1.0, description="Keyword match score.")
    embedding_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Embedding cosine similarity score."
    )
    combined_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Combined hybrid ranking score."
    )
    reason: str = Field(..., description="Human-readable explanation of selection.")


class SelectedMetric(BaseModel):
    """Retrieved business metric with SQL expression, calculation rules, and score."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique metric identifier.")
    description: str = Field(..., description="Business definition of the metric.")
    expression: str = Field(..., description="SQL aggregation expression.")
    aggregation: str = Field(..., description="Aggregation function (SUM, COUNT DISTINCT, etc.).")
    source_tables: list[str] = Field(..., description="Required tables for this metric.")
    filters: list[FilterCondition] = Field(
        default_factory=list, description="Mandatory calculation filters."
    )
    warnings: list[str] = Field(
        default_factory=list, description="Metric-specific calculation warnings."
    )
    score: float = Field(..., ge=0.0, le=1.0, description="Retrieval confidence score.")
    explanation: SelectionExplanation | None = Field(
        default=None, description="Detailed selection explanation."
    )


class SelectedDimension(BaseModel):
    """Retrieved business dimension for filtering, grouping, or slicing with score."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique dimension identifier.")
    description: str = Field(..., description="Business definition of the dimension.")
    source_table: str = Field(..., description="Source table name (schema.table).")
    source_column: str = Field(..., description="Source column name in table.")
    data_type: str = Field(..., description="SQL data type.")
    transformations: list[TransformationMetadata] = Field(
        default_factory=list, description="Available date/text transformations."
    )
    allowed_values: list[str] = Field(
        default_factory=list, description="Enumerated valid values if applicable."
    )
    score: float = Field(..., ge=0.0, le=1.0, description="Retrieval confidence score.")
    explanation: SelectionExplanation | None = Field(
        default=None, description="Detailed selection explanation."
    )


class JoinStep(BaseModel):
    """A single hop in a resolved join path between tables."""

    model_config = ConfigDict(extra="forbid")

    from_table: str = Field(..., description="Source table in this join hop.")
    to_table: str = Field(..., description="Target table in this join hop.")
    join_type: str = Field(default="INNER", description="Recommended SQL join type.")
    join_condition: str = Field(..., description="Explicit SQL ON condition from metadata.")
    relationship_name: str = Field(..., description="Name of relationship from data_sources.yaml.")
    cardinality: str = Field(..., description="Cardinality (many_to_one, one_to_many, etc.).")
    is_safe_for_aggregation: bool = Field(
        default=True, description="Whether this join is safe for direct aggregation."
    )
    warning: str | None = Field(default=None, description="Aggregation or fan-out warning.")


class ResolvedJoinPath(BaseModel):
    """Resolved join path connecting all required tables using declared relationships."""

    model_config = ConfigDict(extra="forbid")

    root_table: str = Field(..., description="Primary FROM table for the query.")
    required_tables: list[str] = Field(..., description="Tables directly required by metrics/dimensions.")
    intermediate_tables: list[str] = Field(
        default_factory=list,
        description="Bridge tables added to connect required tables via declared relationships.",
    )
    steps: list[JoinStep] = Field(
        default_factory=list, description="Ordered join steps to connect all tables."
    )
    relationships: list[RelationshipMetadata] = Field(
        default_factory=list, description="Declarative relationship metadata models used."
    )
    is_safe_for_direct_execution: bool = Field(
        default=True,
        description="False if chasm trap, cartesian product, or missing join path detected.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="All warnings associated with this join path (e.g. fan-out, chasm traps).",
    )


class RetrievalConfig(BaseModel):
    """Runtime configuration options for semantic retrieval scoring and limits."""

    model_config = ConfigDict(extra="forbid")

    keyword_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    embedding_weight: float = Field(default=0.4, ge=0.0, le=1.0)
    min_score_threshold: float = Field(default=0.25, ge=0.0, le=1.0)
    top_k_metrics: int = Field(default=3, ge=1)
    top_k_dimensions: int = Field(default=5, ge=1)
    enable_embeddings: bool = Field(default=False)
    embedding_model: str = Field(default="text-embedding-3-small")


class RetrievalResult(BaseModel):
    """Structured output returned by the Semantic Retriever for downstream Text-to-SQL generation."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., description="Original user natural language question.")
    status: RetrievalStatus = Field(..., description="Sufficiency and safety status of retrieval.")
    confidence_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Overall confidence score for the retrieval."
    )
    selected_metrics: list[SelectedMetric] = Field(
        default_factory=list, description="Selected business metrics ranked by relevance."
    )
    selected_dimensions: list[SelectedDimension] = Field(
        default_factory=list, description="Selected business dimensions ranked by relevance."
    )
    required_tables: list[str] = Field(
        default_factory=list, description="All tables required to compute metrics and dimensions."
    )
    required_columns: list[str] = Field(
        default_factory=list, description="Specific columns referenced in expressions and dimensions."
    )
    join_paths: list[RelationshipMetadata] = Field(
        default_factory=list, description="Applicable declarative join relationships."
    )
    resolved_join_path: ResolvedJoinPath | None = Field(
        default=None, description="Detailed graph-resolved join sequence."
    )
    mandatory_filters: list[FilterCondition] = Field(
        default_factory=list, description="Mandatory metric filter predicates."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Calculation caveats, fan-out warnings, or join restriction violations.",
    )
    is_safe_for_direct_execution: bool = Field(
        default=True,
        description="Whether query can be executed safely as a flat join without subqueries.",
    )
    selection_explanations: list[SelectionExplanation] = Field(
        default_factory=list, description="Audit trail of why each candidate was selected."
    )
