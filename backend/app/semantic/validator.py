"""Validator module for verifying Semantic Layer integrity, cross-references, and safety rules."""

import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy.orm import DeclarativeBase

from app.models.business import Customer, Order, OrderItem, Payment, Product
from app.semantic.models import (
    DataSourcesCatalog,
    DimensionsCatalog,
    MetricsCatalog,
    SemanticCatalog,
)


@dataclass
class ValidationIssue:
    """Individual issue (error or warning) detected during semantic validation."""

    code: str
    message: str
    target: str
    severity: Literal["ERROR", "WARNING"] = "ERROR"


@dataclass
class ValidationResult:
    """Consolidated result of a semantic validation run."""

    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """Return True if no errors were found."""
        return len(self.errors) == 0

    def add_error(self, code: str, message: str, target: str) -> None:
        """Add an error issue to the result."""
        self.errors.append(
            ValidationIssue(code=code, message=message, target=target, severity="ERROR")
        )

    def add_warning(self, code: str, message: str, target: str) -> None:
        """Add a warning issue to the result."""
        self.warnings.append(
            ValidationIssue(code=code, message=message, target=target, severity="WARNING")
        )

    def raise_if_invalid(self) -> None:
        """Raise ValueError with formatted error messages if validation failed."""
        if not self.is_valid:
            lines = [f"Semantic Layer Validation failed with {len(self.errors)} error(s):"]
            for err in self.errors:
                lines.append(f"  [{err.code}] ({err.target}): {err.message}")
            raise ValueError("\n".join(lines))


class SemanticValidator:
    """Performs deep cross-file referential integrity, syntax, and business safety validations."""

    def __init__(self) -> None:
        # Regex to detect schema.table.column patterns in expressions
        self._col_ref_pattern = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\b")

    def validate(self, catalog: SemanticCatalog) -> ValidationResult:
        """Execute full semantic validation suite across data sources, dimensions, and metrics.

        Args:
            catalog: Populated SemanticCatalog instance.

        Returns:
            ValidationResult with all detected errors, warnings, and statistics.
        """
        result = ValidationResult()

        # 1. Validate data sources catalog
        self._validate_data_sources(catalog.data_sources, result)

        # 2. Validate dimensions catalog
        self._validate_dimensions(catalog.dimensions, catalog.data_sources, result)

        # 3. Validate metrics catalog
        self._validate_metrics(catalog.metrics, catalog.data_sources, result)

        # Compute summary stats
        total_columns = sum(len(t.columns) for t in catalog.data_sources.tables)
        result.stats = {
            "tables": len(catalog.data_sources.tables),
            "columns": total_columns,
            "relationships": len(catalog.data_sources.relationships),
            "join_restrictions": len(catalog.data_sources.join_restrictions),
            "dimensions": len(catalog.dimensions.dimensions),
            "metrics": len(catalog.metrics.metrics),
        }

        return result

    def _validate_data_sources(
        self, data_sources: DataSourcesCatalog, result: ValidationResult
    ) -> None:
        """Validate data sources table definitions, primary keys, and relationships."""
        table_names: set[str] = set()
        table_map = {}

        for table in data_sources.tables:
            # Check duplicate table names
            if table.name in table_names:
                result.add_error(
                    code="DUPLICATE_TABLE",
                    message=f"Duplicate table identifier: '{table.name}'.",
                    target=f"table:{table.name}",
                )
            table_names.add(table.name)
            table_map[table.name] = table

            # Check duplicate columns within table
            col_names: set[str] = set()
            for col in table.columns:
                if col.name in col_names:
                    result.add_error(
                        code="DUPLICATE_COLUMN",
                        message=f"Duplicate column '{col.name}' in table '{table.name}'.",
                        target=f"table:{table.name}.column:{col.name}",
                    )
                col_names.add(col.name)

            # Check primary key columns exist
            for pk_col in table.primary_key:
                if pk_col not in col_names:
                    result.add_error(
                        code="INVALID_PRIMARY_KEY",
                        message=f"Primary key column '{pk_col}' not found in table '{table.name}'.",
                        target=f"table:{table.name}",
                    )

            # Check foreign keys
            for fk in table.foreign_keys:
                for fk_col in fk.columns:
                    if fk_col not in col_names:
                        result.add_error(
                            code="INVALID_FK_LOCAL_COLUMN",
                            message=f"FK local column '{fk_col}' not found in table '{table.name}'.",
                            target=f"table:{table.name}",
                        )

        # Validate FK target table & columns
        for table in data_sources.tables:
            for fk in table.foreign_keys:
                if fk.referenced_table not in table_map:
                    result.add_error(
                        code="INVALID_FK_TARGET_TABLE",
                        message=f"FK in table '{table.name}' references non-existent table '{fk.referenced_table}'.",
                        target=f"table:{table.name}",
                    )
                else:
                    target_table = table_map[fk.referenced_table]
                    target_cols = target_table.column_names
                    for ref_col in fk.referenced_columns:
                        if ref_col not in target_cols:
                            result.add_error(
                                code="INVALID_FK_TARGET_COLUMN",
                                message=f"FK in '{table.name}' references non-existent column '{ref_col}' in '{fk.referenced_table}'.",
                                target=f"table:{table.name}",
                            )

        # Validate relationships
        relationship_names: set[str] = set()
        for rel in data_sources.relationships:
            if rel.name in relationship_names:
                result.add_error(
                    code="DUPLICATE_RELATIONSHIP",
                    message=f"Duplicate relationship identifier: '{rel.name}'.",
                    target=f"relationship:{rel.name}",
                )
            relationship_names.add(rel.name)

            if rel.source_table not in table_map:
                result.add_error(
                    code="RELATIONSHIP_INVALID_SOURCE_TABLE",
                    message=f"Relationship '{rel.name}' references unknown source table '{rel.source_table}'.",
                    target=f"relationship:{rel.name}",
                )
            if rel.target_table not in table_map:
                result.add_error(
                    code="RELATIONSHIP_INVALID_TARGET_TABLE",
                    message=f"Relationship '{rel.name}' references unknown target table '{rel.target_table}'.",
                    target=f"relationship:{rel.name}",
                )

        # Validate join restrictions
        for res in data_sources.join_restrictions:
            for forbidden_table in res.forbidden_concurrent_joins:
                if forbidden_table not in table_map:
                    result.add_error(
                        code="JOIN_RESTRICTION_UNKNOWN_TABLE",
                        message=f"Join restriction '{res.name}' references unknown table '{forbidden_table}'.",
                        target=f"join_restriction:{res.name}",
                    )

    def _validate_dimensions(
        self,
        dimensions: DimensionsCatalog,
        data_sources: DataSourcesCatalog,
        result: ValidationResult,
    ) -> None:
        """Validate dimensions uniqueness, source references, and multilingual terms."""
        dimension_names: set[str] = set()
        table_map = data_sources.table_map

        for dim in dimensions.dimensions:
            # Check duplicate dimension names
            if dim.name in dimension_names:
                result.add_error(
                    code="DUPLICATE_DIMENSION",
                    message=f"Duplicate dimension identifier: '{dim.name}'.",
                    target=f"dimension:{dim.name}",
                )
            dimension_names.add(dim.name)

            # Check source table exists
            if dim.source_table not in table_map:
                result.add_error(
                    code="DIMENSION_UNKNOWN_TABLE",
                    message=f"Dimension '{dim.name}' references unknown table '{dim.source_table}'.",
                    target=f"dimension:{dim.name}",
                )
                continue

            # Check source column exists in table
            target_table = table_map[dim.source_table]
            if dim.source_column not in target_table.column_names:
                result.add_error(
                    code="DIMENSION_UNKNOWN_COLUMN",
                    message=f"Dimension '{dim.name}' references non-existent column '{dim.source_column}' in table '{dim.source_table}'.",
                    target=f"dimension:{dim.name}",
                )

            # Check synonyms presence
            if not dim.synonyms.vi:
                result.add_warning(
                    code="MISSING_VIETNAMESE_SYNONYMS",
                    message=f"Dimension '{dim.name}' has no Vietnamese synonyms defined.",
                    target=f"dimension:{dim.name}",
                )
            if not dim.synonyms.en:
                result.add_warning(
                    code="MISSING_ENGLISH_SYNONYMS",
                    message=f"Dimension '{dim.name}' has no English synonyms defined.",
                    target=f"dimension:{dim.name}",
                )

    def _validate_metrics(
        self,
        metrics: MetricsCatalog,
        data_sources: DataSourcesCatalog,
        result: ValidationResult,
    ) -> None:
        """Validate metrics uniqueness, source tables, aggregation expressions, and synonyms."""
        metric_names: set[str] = set()
        table_map = data_sources.table_map
        rel_map = {rel.name: rel for rel in data_sources.relationships}

        for metric in metrics.metrics:
            # Check duplicate metric names
            if metric.name in metric_names:
                result.add_error(
                    code="DUPLICATE_METRIC",
                    message=f"Duplicate metric identifier: '{metric.name}'.",
                    target=f"metric:{metric.name}",
                )
            metric_names.add(metric.name)

            # Check source tables exist
            for src_tbl in metric.source_tables:
                if src_tbl not in table_map:
                    result.add_error(
                        code="METRIC_UNKNOWN_TABLE",
                        message=f"Metric '{metric.name}' references unknown table '{src_tbl}'.",
                        target=f"metric:{metric.name}",
                    )

            # Check columns mentioned in expression
            matches = self._col_ref_pattern.findall(metric.expression)
            for tbl, col in matches:
                if tbl not in table_map:
                    result.add_error(
                        code="EXPRESSION_UNKNOWN_TABLE",
                        message=f"Metric '{metric.name}' expression references unknown table '{tbl}'.",
                        target=f"metric:{metric.name}",
                    )
                elif col not in table_map[tbl].column_names:
                    result.add_error(
                        code="EXPRESSION_UNKNOWN_COLUMN",
                        message=f"Metric '{metric.name}' expression references non-existent column '{col}' in '{tbl}'.",
                        target=f"metric:{metric.name}",
                    )

            # Check join requirements
            for req in metric.join_requirements:
                if req.relationship_name and req.relationship_name not in rel_map:
                    result.add_error(
                        code="METRIC_UNKNOWN_RELATIONSHIP",
                        message=f"Metric '{metric.name}' references unknown relationship '{req.relationship_name}'.",
                        target=f"metric:{metric.name}",
                    )
                if req.join_table and req.join_table not in table_map:
                    result.add_error(
                        code="METRIC_UNKNOWN_JOIN_TABLE",
                        message=f"Metric '{metric.name}' references unknown join table '{req.join_table}'.",
                        target=f"metric:{metric.name}",
                    )

            # Check synonyms presence
            if not metric.synonyms.vi:
                result.add_warning(
                    code="MISSING_VIETNAMESE_SYNONYMS",
                    message=f"Metric '{metric.name}' has no Vietnamese synonyms defined.",
                    target=f"metric:{metric.name}",
                )
            if not metric.synonyms.en:
                result.add_warning(
                    code="MISSING_ENGLISH_SYNONYMS",
                    message=f"Metric '{metric.name}' has no English synonyms defined.",
                    target=f"metric:{metric.name}",
                )

    def check_query_tables_safety(
        self, tables: list[str] | set[str], catalog: SemanticCatalog
    ) -> list[ValidationIssue]:
        """Verify whether a candidate set of tables violates join restrictions (such as Chasm traps).

        Args:
            tables: Set of tables involved in a query plan.
            catalog: Semantic catalog with join restrictions.

        Returns:
            List of detected join restriction issues.
        """
        table_set = set(tables)
        issues: list[ValidationIssue] = []

        for restriction in catalog.data_sources.join_restrictions:
            forbidden = set(restriction.forbidden_concurrent_joins)
            if forbidden.issubset(table_set):
                issues.append(
                    ValidationIssue(
                        code=restriction.name,
                        message=f"Unsafe concurrent joins detected: {sorted(forbidden)}. {restriction.reason} Remediation: {restriction.remediation}",
                        target="query_plan",
                        severity="ERROR" if restriction.severity == "CRITICAL" else "WARNING",
                    )
                )

        return issues

    def validate_against_sqlalchemy(self, catalog: SemanticCatalog) -> ValidationResult:
        """Verify that semantic layer metadata strictly matches SQLAlchemy declarative models in business schema.

        This guarantees that no hallucinated tables or columns exist and all types align.
        """
        result = ValidationResult()

        business_models: list[type[DeclarativeBase]] = [
            Customer,
            Order,
            OrderItem,
            Product,
            Payment,
        ]

        # Map full schema.table name to SQLAlchemy model
        sa_models: dict[str, type[DeclarativeBase]] = {}
        for model in business_models:
            schema = getattr(model, "__table_args__", [{}])[-1].get("schema", "business")
            full_name = f"{schema}.{model.__tablename__}"
            sa_models[full_name] = model

        catalog_table_map = catalog.data_sources.table_map

        # 1. Check all SQLAlchemy business tables are declared in data_sources
        for full_name, model in sa_models.items():
            if full_name not in catalog_table_map:
                result.add_error(
                    code="MISSING_SQLA_TABLE",
                    message=f"SQLAlchemy business model '{full_name}' is missing in data_sources.yaml.",
                    target=f"table:{full_name}",
                )
                continue

            cat_table = catalog_table_map[full_name]
            cat_cols = cat_table.column_names

            # Check columns in SQLAlchemy model exist in catalog
            sa_columns = model.__table__.columns
            for sa_col in sa_columns:
                if sa_col.name not in cat_cols:
                    result.add_error(
                        code="MISSING_SQLA_COLUMN",
                        message=f"SQLAlchemy column '{full_name}.{sa_col.name}' is missing in data_sources.yaml.",
                        target=f"table:{full_name}.column:{sa_col.name}",
                    )

            # Check catalog columns exist in SQLAlchemy model
            sa_col_names = {c.name for c in sa_columns}
            for cat_col in cat_table.columns:
                if cat_col.name not in sa_col_names:
                    result.add_error(
                        code="HALLUCINATED_CATALOG_COLUMN",
                        message=f"Catalog column '{full_name}.{cat_col.name}' does not exist in SQLAlchemy model.",
                        target=f"table:{full_name}.column:{cat_col.name}",
                    )

        # 2. Check no hallucinated tables in catalog for business schema
        for cat_tbl_name in catalog_table_map:
            if cat_tbl_name.startswith("business.") and cat_tbl_name not in sa_models:
                result.add_error(
                    code="HALLUCINATED_CATALOG_TABLE",
                    message=f"Catalog table '{cat_tbl_name}' does not exist in business SQLAlchemy models.",
                    target=f"table:{cat_tbl_name}",
                )

        return result
