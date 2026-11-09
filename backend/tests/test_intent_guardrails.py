"""Unit tests for business intent conformance checks in the SQL guardrails."""

from app.security import (
    GuardrailConfig,
    SQLGuardrailValidator,
    ViolationRule,
    validate_sql,
)
from app.semantic.models import FilterCondition
from app.semantic.retrieval.schemas import (
    RetrievalResult,
    RetrievalStatus,
    SelectedMetric,
)


def make_validator(**config_overrides) -> SQLGuardrailValidator:
    """Build a guardrail validator with intent checks enabled by default."""
    config = GuardrailConfig(
        max_limit=1000,
        default_limit=100,
        enforce_limit=True,
        **config_overrides,
    )
    return SQLGuardrailValidator(config=config)


def make_retrieval(
    *,
    expression: str = "SUM(o.order_total_price)",
    aggregation: str = "SUM",
    filters: list[str] | None = None,
    source_tables: list[str] | None = None,
) -> RetrievalResult:
    """Build a RetrievalResult fixture with one metric and optional mandatory filters."""
    metric = SelectedMetric(
        name="total_revenue",
        description="Revenue of delivered orders",
        expression=expression,
        aggregation=aggregation,
        source_tables=source_tables or ["business.orders"],
        filters=[
            FilterCondition(
                condition=condition,
                description="Revenue counts only delivered orders.",
            )
            for condition in (filters or ["o.order_status = 'delivered'"])
        ],
        score=0.9,
    )
    return RetrievalResult(
        question="total revenue of delivered orders",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.9,
        selected_metrics=[metric],
        selected_dimensions=[],
    )


def rule_codes(result) -> set[str]:
    """Extract rule codes from a ValidationResult."""
    return {
        violation.rule.value if hasattr(violation.rule, "value") else violation.rule
        for violation in result.violations
    }


# ==============================================================================
# 1. BACKWARD COMPATIBILITY (no retrieval context)
# ==============================================================================


def test_no_retrieval_keeps_security_only_behavior() -> None:
    """Without retrieval context, no intent violations may be produced."""
    validator = make_validator()
    result = validator.validate("SELECT order_id FROM business.orders")

    assert result.is_valid is True
    assert all(not code.startswith("INTENT_") for code in rule_codes(result))


def test_enforce_intent_checks_off_skips_intent_rules() -> None:
    """enforce_intent_checks=False disables intent conformance entirely."""
    validator = make_validator(enforce_intent_checks=False)
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders WHERE o.order_status = 'shipped'",
        retrieval=retrieval,
    )

    assert rule_codes(result).isdisjoint({"INTENT_MISSING_FILTER"})


def test_validate_sql_function_accepts_retrieval() -> None:
    """Top-level validate_sql convenience forwards retrieval into intent checks."""
    retrieval = make_retrieval()
    result = validate_sql(
        "SELECT SUM(o.order_total_price) FROM business.orders",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER in [
        v.rule for v in result.violations
    ]


# ==============================================================================
# 2. INTENT_MISSING_FILTER (HIGH -- always blocks)
# ==============================================================================


def test_missing_mandatory_filter_blocks() -> None:
    """A HIGH intent violation blocks execution when the filter is absent."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders",
        retrieval=retrieval,
    )

    assert result.is_valid is False
    assert ViolationRule.INTENT_MISSING_FILTER in [v.rule for v in result.violations]
    violation = next(v for v in result.violations if v.rule == ViolationRule.INTENT_MISSING_FILTER)
    assert violation.severity.value == "HIGH"
    assert violation.details.get("filter") == "o.order_status = 'delivered'"


def test_wrong_filter_value_blocks() -> None:
    """A filter with a different literal does not satisfy the mandatory filter."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders "
        "WHERE o.order_status = 'shipped'",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER in [v.rule for v in result.violations]


def test_present_mandatory_filter_passes_with_alias_variants() -> None:
    """A filter satisfied through any alias/qualification is accepted."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT sum(orders.order_total_price) FROM business.orders orders "
        "WHERE orders.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER not in rule_codes(result)
    assert result.is_valid is True


def test_in_filter_satisfied_by_equality() -> None:
    """A single-element IN filter is equivalent to an equality predicate."""
    validator = make_validator()
    retrieval = make_retrieval(filters=["o.order_status IN ('delivered')"])

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER not in rule_codes(result)


def test_mandatory_filter_inside_cte_counts() -> None:
    """Filters applied inside a WITH clause satisfy the contract (fan-out pattern)."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "WITH delivered AS (SELECT order_id FROM business.orders "
        "WHERE order_status = 'delivered') "
        "SELECT COUNT(*) FROM delivered",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER not in rule_codes(result)


def test_unparseable_filter_fails_open() -> None:
    """An unparseable mandatory filter is ignored, never guessed at."""
    validator = make_validator()
    retrieval = make_retrieval(filters=["order_status = 'delivered AND bogus syntax here because"])

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_MISSING_FILTER not in rule_codes(result)
    assert result.is_valid is True


# ==============================================================================
# 3. INTENT_METRIC_MISMATCH (MEDIUM -- warns by default, blocks in strict mode)
# ==============================================================================


def test_metric_expression_drift_warns_by_default() -> None:
    """Aggregation drift (missing DISTINCT) degrades to a non-blocking warning."""
    validator = make_validator()
    retrieval = make_retrieval(expression="COUNT(DISTINCT o.order_id)", aggregation="COUNT_DISTINCT")

    result = validator.validate(
        "SELECT COUNT(o.order_id) FROM business.orders WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert result.is_valid is True  # ambiguous failure -> warning, not block
    assert any("[INTENT_METRIC_MISMATCH]" in w for w in result.warnings)
    assert ViolationRule.INTENT_METRIC_MISMATCH not in rule_codes(result)


def test_metric_expression_drift_blocks_in_strict_mode() -> None:
    """intent_strict_mode=True promotes the drift warning to a blocking violation."""
    validator = make_validator(intent_strict_mode=True)
    retrieval = make_retrieval(expression="COUNT(DISTINCT o.order_id)", aggregation="COUNT_DISTINCT")

    result = validator.validate(
        "SELECT COUNT(o.order_id) FROM business.orders WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert result.is_valid is False
    assert ViolationRule.INTENT_METRIC_MISMATCH in [v.rule for v in result.violations]


def test_metric_expression_match_across_form_variants() -> None:
    """Alias, casing, and qualifier differences do not count as drift."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT sum(o.order_total_price) AS revenue FROM business.orders "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_METRIC_MISMATCH not in rule_codes(result)
    assert not any("INTENT_METRIC_MISMATCH" in w for w in result.warnings)


def test_wrong_aggregation_function_is_drift() -> None:
    """Swapping SUM for AVG violates the metric definition."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT AVG(o.order_total_price) FROM business.orders "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert any("INTENT_METRIC_MISMATCH" in w for w in result.warnings) or (
        ViolationRule.INTENT_METRIC_MISMATCH in rule_codes(result)
    )


# ==============================================================================
# 4. INTENT_REQUIRED_TABLE_MISSING (MEDIUM)
# ==============================================================================


def test_missing_required_table_is_flagged() -> None:
    """A table required by the metric but absent from SQL is flagged."""
    validator = make_validator()
    retrieval = make_retrieval(source_tables=["business.orders", "business.order_items"])

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders o "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert any("INTENT_REQUIRED_TABLE_MISSING" in w for w in result.warnings) or (
        ViolationRule.INTENT_REQUIRED_TABLE_MISSING in rule_codes(result)
    )


def test_required_tables_resolved_to_default_schema() -> None:
    """Unqualified source tables resolve against the guardrail default schema."""
    validator = make_validator()
    retrieval = make_retrieval(source_tables=["orders"])  # no schema prefix

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders o "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert ViolationRule.INTENT_REQUIRED_TABLE_MISSING not in rule_codes(result)
    assert not any("INTENT_REQUIRED_TABLE_MISSING" in w for w in result.warnings)


def test_strict_mode_blocks_missing_required_table() -> None:
    """In strict mode the missing-table warning becomes a blocking violation."""
    validator = make_validator(intent_strict_mode=True)
    retrieval = make_retrieval(source_tables=["business.orders", "business.order_items"])

    result = validator.validate(
        "SELECT SUM(o.order_total_price) FROM business.orders o "
        "WHERE o.order_status = 'delivered'",
        retrieval=retrieval,
    )

    assert result.is_valid is False
    assert ViolationRule.INTENT_REQUIRED_TABLE_MISSING in [v.rule for v in result.violations]


# ==============================================================================
# 5. END-TO-END happy path
# ==============================================================================


def test_compliant_query_fully_passes() -> None:
    """A query honoring expression, filter, and tables passes with no intent noise."""
    validator = make_validator()
    retrieval = make_retrieval()

    result = validator.validate(
        "SELECT SUM(o.order_total_price) AS revenue FROM business.orders o "
        "WHERE o.order_status = 'delivered' LIMIT 100",
        retrieval=retrieval,
    )

    assert result.is_valid is True
    assert all(not code.startswith("INTENT_") for code in rule_codes(result))
    assert result.sanitized_sql is not None
