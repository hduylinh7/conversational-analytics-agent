"""Pydantic models defining the Semantic Layer schema for metrics, dimensions, and data sources."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Synonyms(BaseModel):
    """Multilingual synonyms for metrics and dimensions (Vietnamese and English)."""

    model_config = ConfigDict(extra="forbid")

    vi: list[str] = Field(
        default_factory=list,
        description="Vietnamese keywords and phrases for natural language query matching.",
    )
    en: list[str] = Field(
        default_factory=list,
        description="English keywords and phrases for natural language query matching.",
    )

    @property
    def all(self) -> list[str]:
        """Return all synonyms across languages."""
        return self.vi + self.en


class ColumnMetadata(BaseModel):
    """Column schema metadata for a database table."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Column name in the physical database.")
    data_type: str = Field(..., description="SQL data type (e.g. VARCHAR(32), NUMERIC(12,2)).")
    description: str = Field(..., description="Business meaning of this column.")
    nullable: bool = Field(default=True, description="Whether column accepts NULL values.")
    is_primary_key: bool = Field(default=False, description="Whether column is part of table PK.")


class ForeignKeyMetadata(BaseModel):
    """Foreign key relationship metadata on a database table."""

    model_config = ConfigDict(extra="forbid")

    columns: list[str] = Field(..., description="Local columns forming the foreign key.")
    referenced_table: str = Field(..., description="Referenced target table (schema.table).")
    referenced_columns: list[str] = Field(
        ..., description="Columns in referenced target table."
    )
    on_delete: str = Field(default="NO ACTION", description="Referential action on delete.")


class TableMetadata(BaseModel):
    """Database table metadata including keys and available columns."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Fully qualified table name (schema.table).")
    alias: str = Field(..., description="Recommended SQL table alias.")
    description: str = Field(..., description="Business purpose of this table.")
    primary_key: list[str] = Field(..., description="Primary key column names.")
    foreign_keys: list[ForeignKeyMetadata] = Field(
        default_factory=list, description="Foreign keys defined on this table."
    )
    columns: list[ColumnMetadata] = Field(
        ..., description="Columns defined in this table."
    )

    @property
    def column_names(self) -> set[str]:
        """Return set of all column names."""
        return {col.name for col in self.columns}


class RelationshipMetadata(BaseModel):
    """Explicit join relationship between two tables with cardinality."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique identifier for this join relationship.")
    source_table: str = Field(..., description="Starting table (schema.table).")
    target_table: str = Field(..., description="Target table (schema.table).")
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"] = Field(
        ..., description="Relationship cardinality."
    )
    join_type: Literal["INNER", "LEFT", "RIGHT", "FULL"] = Field(
        default="INNER", description="Recommended SQL join type."
    )
    join_condition: str = Field(
        ..., description="SQL ON condition expression (e.g., tableA.id = tableB.id)."
    )
    description: str = Field(..., description="Business explanation of relationship.")
    is_safe_for_aggregation: bool = Field(
        default=True,
        description="Whether this join is safe for direct aggregation without row multiplication.",
    )
    warning: str | None = Field(
        default=None, description="Aggregation risk warning (e.g., fan-out)."
    )


class JoinRestrictionMetadata(BaseModel):
    """Restriction rule preventing invalid or Cartesian joins (e.g. Chasm traps)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique identifier for the join restriction.")
    forbidden_concurrent_joins: list[str] = Field(
        ...,
        description="List of tables that must NOT be joined concurrently in a flat query.",
    )
    severity: Literal["CRITICAL", "WARNING"] = Field(
        default="CRITICAL", description="Severity level of the restriction."
    )
    reason: str = Field(..., description="Detailed explanation of the risk.")
    remediation: str = Field(
        ..., description="Guidance on how to safely structure queries instead."
    )


class DatabaseMetadata(BaseModel):
    """Database connection and type metadata."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Database name.")
    type: str = Field(..., description="Database engine type (e.g., postgresql).")
    default_schema: str = Field(..., description="Default schema for analytics.")
    description: str | None = Field(default=None, description="Optional description.")


class DataSourcesCatalog(BaseModel):
    """Root model for data_sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0", description="Schema version.")
    database: DatabaseMetadata = Field(..., description="Database information.")
    tables: list[TableMetadata] = Field(..., description="Catalog of database tables.")
    relationships: list[RelationshipMetadata] = Field(
        default_factory=list, description="Explicit relationships between tables."
    )
    join_restrictions: list[JoinRestrictionMetadata] = Field(
        default_factory=list, description="Rules preventing unsafe joins."
    )

    @property
    def table_map(self) -> dict[str, TableMetadata]:
        """Return mapping from table name to TableMetadata."""
        return {table.name: table for table in self.tables}


class TransformationMetadata(BaseModel):
    """Transformation or formatting rule for a dimension."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Transformation identifier (e.g. date, month, year).")
    expression: str = Field(..., description="SQL transformation expression.")
    description: str = Field(..., description="Explanation of transformation result.")


class DimensionMetadata(BaseModel):
    """Business dimension definition for filtering, grouping, or slicing."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique dimension identifier.")
    description: str = Field(..., description="Business meaning of the dimension.")
    synonyms: Synonyms = Field(
        ..., description="Vietnamese and English terms used in user prompts."
    )
    source_table: str = Field(..., description="Actual source database table (schema.table).")
    source_column: str = Field(..., description="Actual source column name.")
    data_type: str = Field(..., description="SQL data type of dimension.")
    filterable: bool = Field(default=True, description="Whether dimension supports filtering.")
    groupable: bool = Field(default=True, description="Whether dimension supports grouping.")
    transformations: list[TransformationMetadata] = Field(
        default_factory=list, description="Formatting or extraction transformations."
    )
    allowed_values: list[str] = Field(
        default_factory=list, description="Enumerated valid values if applicable."
    )
    notes: str | None = Field(default=None, description="Special analytical notes or caveats.")


class DimensionsCatalog(BaseModel):
    """Root model for dimensions.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0", description="Schema version.")
    dimensions: list[DimensionMetadata] = Field(
        ..., description="List of defined business dimensions."
    )

    @property
    def dimension_map(self) -> dict[str, DimensionMetadata]:
        """Return mapping from dimension name to DimensionMetadata."""
        return {dim.name: dim for dim in self.dimensions}


class FilterCondition(BaseModel):
    """Mandatory or default filter condition for a metric."""

    model_config = ConfigDict(extra="forbid")

    condition: str = Field(..., description="SQL WHERE clause predicate.")
    description: str = Field(..., description="Business justification for filter.")


class JoinRequirement(BaseModel):
    """Table join requirement for calculating a metric."""

    model_config = ConfigDict(extra="forbid")

    relationship_name: str | None = Field(
        default=None, description="Identifier of relationship from data_sources.yaml."
    )
    join_table: str | None = Field(
        default=None, description="Table to join if relationship not named."
    )
    join_condition: str | None = Field(
        default=None, description="SQL ON condition."
    )
    join_type: str = Field(default="INNER", description="SQL join type.")


class MetricMetadata(BaseModel):
    """Business metric definition with SQL aggregation expression."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Unique metric identifier.")
    description: str = Field(..., description="Clear business definition.")
    synonyms: Synonyms = Field(
        ..., description="Vietnamese and English terms used in user questions."
    )
    expression: str = Field(..., description="SQL aggregation expression.")
    source_tables: list[str] = Field(
        ..., description="Tables required to calculate this metric."
    )
    aggregation: Literal["SUM", "COUNT", "COUNT DISTINCT", "AVG", "MIN", "MAX", "RATIO"] = Field(
        ..., description="Aggregation method."
    )
    join_requirements: list[JoinRequirement] = Field(
        default_factory=list, description="Required relationships and join conditions."
    )
    filters: list[FilterCondition] = Field(
        default_factory=list, description="Mandatory calculation constraints."
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Known limitations, calculation risks, or double-counting warnings.",
    )

    @field_validator("source_tables")
    @classmethod
    def validate_source_tables_not_empty(cls, v: list[str]) -> list[str]:
        """Ensure source_tables contains at least one table."""
        if not v:
            raise ValueError("source_tables cannot be empty.")
        return v


class MetricsCatalog(BaseModel):
    """Root model for metrics.yaml."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(default="1.0", description="Schema version.")
    metrics: list[MetricMetadata] = Field(
        ..., description="List of defined business metrics."
    )

    @property
    def metric_map(self) -> dict[str, MetricMetadata]:
        """Return mapping from metric name to MetricMetadata."""
        return {metric.name: metric for metric in self.metrics}


class SemanticCatalog(BaseModel):
    """Complete Semantic Layer Catalog holding data sources, dimensions, and metrics."""

    model_config = ConfigDict(extra="forbid")

    data_sources: DataSourcesCatalog
    dimensions: DimensionsCatalog
    metrics: MetricsCatalog
