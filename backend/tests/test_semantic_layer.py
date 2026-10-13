"""Unit tests for the Semantic Layer definitions, YAML loaders, and validation engine."""

from pathlib import Path

import pytest

from app.semantic.loader import (
    SemanticFileNotFoundError,
    SemanticSchemaValidationError,
    SemanticYamlSyntaxError,
    load_data_sources,
    load_dimensions,
    load_semantic_layer,
)
from app.semantic.models import (
    DimensionMetadata,
    DimensionsCatalog,
    MetricMetadata,
    MetricsCatalog,
    SemanticCatalog,
    Synonyms,
)
from app.semantic.validator import SemanticValidator


@pytest.fixture
def catalog() -> SemanticCatalog:
    """Fixture providing the loaded repository semantic layer catalog."""
    return load_semantic_layer()


# ==============================================================================
# 1. VALID CONFIGURATION & SCHEMA TESTS
# ==============================================================================


def test_load_semantic_layer_success(catalog: SemanticCatalog) -> None:
    """Verify that all three YAML files load into SemanticCatalog without errors."""
    assert catalog.data_sources is not None
    assert catalog.dimensions is not None
    assert catalog.metrics is not None
    assert len(catalog.data_sources.tables) == 5
    assert len(catalog.dimensions.dimensions) >= 5
    assert len(catalog.metrics.metrics) >= 4


def test_semantic_layer_validation_passes(catalog: SemanticCatalog) -> None:
    """Verify that the production Semantic Layer passes all referential and syntax checks."""
    validator = SemanticValidator()
    result = validator.validate(catalog)

    if not result.is_valid:
        error_msgs = "\n".join(f"{e.code} ({e.target}): {e.message}" for e in result.errors)
        pytest.fail(f"Validation failed with errors:\n{error_msgs}")

    assert result.is_valid is True
    assert len(result.errors) == 0
    assert result.stats["tables"] == 5
    assert result.stats["relationships"] >= 7
    assert result.stats["join_restrictions"] >= 1


def test_database_schema_alignment_with_sqlalchemy(catalog: SemanticCatalog) -> None:
    """Verify that semantic layer tables and columns match SQLAlchemy business models 100%."""
    validator = SemanticValidator()
    result = validator.validate_against_sqlalchemy(catalog)

    if not result.is_valid:
        error_msgs = "\n".join(f"{e.code} ({e.target}): {e.message}" for e in result.errors)
        pytest.fail(f"SQLAlchemy alignment check failed:\n{error_msgs}")

    assert result.is_valid is True
    assert len(result.errors) == 0


def test_required_metrics_defined(catalog: SemanticCatalog) -> None:
    """Verify all explicitly requested metrics are defined with clear business definitions."""
    metric_map = catalog.metrics.metric_map

    # 1. total_revenue
    assert "total_revenue" in metric_map
    total_rev = metric_map["total_revenue"]
    assert total_rev.aggregation == "SUM"
    assert "business.order_items.price" in total_rev.expression
    assert "business.order_items" in total_rev.source_tables
    # Ensure merchandise revenue explicitly notes excluding freight
    assert "Excludes shipping" in total_rev.warnings[0] or "Excludes" in total_rev.description

    # 2. order_count
    assert "order_count" in metric_map
    order_cnt = metric_map["order_count"]
    assert order_cnt.aggregation == "COUNT DISTINCT"
    assert "COUNT(DISTINCT business.orders.order_id)" in order_cnt.expression

    # 3. total_freight
    assert "total_freight" in metric_map
    total_freight = metric_map["total_freight"]
    assert total_freight.aggregation == "SUM"
    assert "business.order_items.freight_value" in total_freight.expression

    # 4. total_gross_revenue & total_payment_value
    assert "total_gross_revenue" in metric_map
    assert "total_payment_value" in metric_map


def test_required_dimensions_defined(catalog: SemanticCatalog) -> None:
    """Verify all explicitly requested dimensions are defined and match existing columns."""
    dim_map = catalog.dimensions.dimension_map

    expected_dims = [
        ("order_date", "business.orders", "order_purchase_timestamp"),
        ("order_status", "business.orders", "order_status"),
        ("product_category", "business.products", "product_category_name"),
        ("customer_state", "business.customers", "customer_state"),
        ("payment_type", "business.payments", "payment_type"),
    ]

    for dim_name, expected_table, expected_column in expected_dims:
        assert dim_name in dim_map, f"Missing required dimension: {dim_name}"
        dim = dim_map[dim_name]
        assert dim.source_table == expected_table
        assert dim.source_column == expected_column
        assert dim.filterable is True
        assert dim.groupable is True


def test_multilingual_synonyms_coverage(catalog: SemanticCatalog) -> None:
    """Verify that every metric and dimension defines both Vietnamese and English synonyms."""
    for metric in catalog.metrics.metrics:
        assert len(metric.synonyms.vi) > 0, f"Metric '{metric.name}' missing Vietnamese synonyms"
        assert len(metric.synonyms.en) > 0, f"Metric '{metric.name}' missing English synonyms"

    for dim in catalog.dimensions.dimensions:
        assert len(dim.synonyms.vi) > 0, f"Dimension '{dim.name}' missing Vietnamese synonyms"
        assert len(dim.synonyms.en) > 0, f"Dimension '{dim.name}' missing English synonyms"


def test_chasm_trap_safety_check(catalog: SemanticCatalog) -> None:
    """Verify that joining order_items and payments concurrently triggers a join restriction issue."""
    validator = SemanticValidator()

    # Combining order_items and payments must be flagged
    unsafe_tables = ["business.orders", "business.order_items", "business.payments"]
    issues = validator.check_query_tables_safety(unsafe_tables, catalog)
    assert len(issues) == 1
    assert issues[0].code == "chasm_trap_order_items_and_payments"
    assert issues[0].severity == "ERROR"
    assert "Chasm Trap" in issues[0].message

    # Safe joins must not trigger issues
    safe_tables = ["business.orders", "business.order_items", "business.products"]
    safe_issues = validator.check_query_tables_safety(safe_tables, catalog)
    assert len(safe_issues) == 0


# ==============================================================================
# 2. INVALID CONFIGURATION & ERROR HANDLING TESTS
# ==============================================================================


def test_validator_catches_duplicate_metric(catalog: SemanticCatalog) -> None:
    """Verify validator flags duplicate metric identifiers."""
    validator = SemanticValidator()

    # Clone metrics catalog with a duplicated entry
    dupe_metric = catalog.metrics.metrics[0].model_copy()
    corrupt_metrics = MetricsCatalog(
        version="1.0",
        metrics=catalog.metrics.metrics + [dupe_metric],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=catalog.dimensions,
        metrics=corrupt_metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "DUPLICATE_METRIC" for e in result.errors)


def test_validator_catches_duplicate_dimension(catalog: SemanticCatalog) -> None:
    """Verify validator flags duplicate dimension identifiers."""
    validator = SemanticValidator()

    dupe_dim = catalog.dimensions.dimensions[0].model_copy()
    corrupt_dims = DimensionsCatalog(
        version="1.0",
        dimensions=catalog.dimensions.dimensions + [dupe_dim],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=corrupt_dims,
        metrics=catalog.metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "DUPLICATE_DIMENSION" for e in result.errors)


def test_validator_catches_nonexistent_table_in_dimension(catalog: SemanticCatalog) -> None:
    """Verify validator flags dimension referencing a non-existent table."""
    validator = SemanticValidator()

    corrupt_dim = DimensionMetadata(
        name="ghost_dimension",
        description="Ghost test dimension",
        synonyms=Synonyms(vi=["ma"], en=["ghost"]),
        source_table="business.ghost_table",
        source_column="ghost_col",
        data_type="VARCHAR",
    )
    corrupt_dims = DimensionsCatalog(
        version="1.0",
        dimensions=catalog.dimensions.dimensions + [corrupt_dim],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=corrupt_dims,
        metrics=catalog.metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "DIMENSION_UNKNOWN_TABLE" for e in result.errors)


def test_validator_catches_nonexistent_column_in_dimension(catalog: SemanticCatalog) -> None:
    """Verify validator flags dimension referencing a non-existent column in a valid table."""
    validator = SemanticValidator()

    corrupt_dim = DimensionMetadata(
        name="invalid_column_dim",
        description="Dimension pointing to invalid column",
        synonyms=Synonyms(vi=["cột sai"], en=["invalid col"]),
        source_table="business.orders",
        source_column="non_existent_column_xyz",
        data_type="VARCHAR",
    )
    corrupt_dims = DimensionsCatalog(
        version="1.0",
        dimensions=catalog.dimensions.dimensions + [corrupt_dim],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=corrupt_dims,
        metrics=catalog.metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "DIMENSION_UNKNOWN_COLUMN" for e in result.errors)


def test_validator_catches_nonexistent_table_in_metric(catalog: SemanticCatalog) -> None:
    """Verify validator flags metric referencing a non-existent table."""
    validator = SemanticValidator()

    corrupt_metric = MetricMetadata(
        name="ghost_metric",
        description="Metric referencing unknown table",
        synonyms=Synonyms(vi=["chỉ số ảo"], en=["ghost metric"]),
        expression="SUM(business.ghost_table.amount)",
        source_tables=["business.ghost_table"],
        aggregation="SUM",
    )
    corrupt_metrics = MetricsCatalog(
        version="1.0",
        metrics=catalog.metrics.metrics + [corrupt_metric],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=catalog.dimensions,
        metrics=corrupt_metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "METRIC_UNKNOWN_TABLE" for e in result.errors)


def test_validator_catches_nonexistent_column_in_metric_expression(
    catalog: SemanticCatalog,
) -> None:
    """Verify validator flags metric whose expression references a non-existent column."""
    validator = SemanticValidator()

    corrupt_metric = MetricMetadata(
        name="bad_expression_metric",
        description="Metric referencing unknown column in expression",
        synonyms=Synonyms(vi=["biểu thức sai"], en=["bad expression"]),
        expression="SUM(business.order_items.hallucinated_column)",
        source_tables=["business.order_items"],
        aggregation="SUM",
    )
    corrupt_metrics = MetricsCatalog(
        version="1.0",
        metrics=catalog.metrics.metrics + [corrupt_metric],
    )
    corrupt_catalog = SemanticCatalog(
        data_sources=catalog.data_sources,
        dimensions=catalog.dimensions,
        metrics=corrupt_metrics,
    )

    result = validator.validate(corrupt_catalog)
    assert result.is_valid is False
    assert any(e.code == "EXPRESSION_UNKNOWN_COLUMN" for e in result.errors)


def test_loader_raises_for_missing_file(tmp_path: Path) -> None:
    """Verify loader raises SemanticFileNotFoundError when a YAML file does not exist."""
    with pytest.raises(SemanticFileNotFoundError):
        load_semantic_layer(tmp_path)


def test_loader_raises_for_malformed_yaml(tmp_path: Path) -> None:
    """Verify loader raises SemanticYamlSyntaxError when YAML syntax is invalid."""
    bad_yaml_file = tmp_path / "data_sources.yaml"
    bad_yaml_file.write_text("this: is: invalid: [yaml syntax", encoding="utf-8")

    with pytest.raises(SemanticYamlSyntaxError):
        load_data_sources(bad_yaml_file)


def test_loader_raises_for_schema_validation_error(tmp_path: Path) -> None:
    """Verify loader raises SemanticSchemaValidationError when required schema fields are missing."""
    invalid_schema_file = tmp_path / "dimensions.yaml"
    # Missing required 'dimensions' list
    invalid_schema_file.write_text("version: '1.0'\nwrong_field: 123", encoding="utf-8")

    with pytest.raises(SemanticSchemaValidationError):
        load_dimensions(invalid_schema_file)
