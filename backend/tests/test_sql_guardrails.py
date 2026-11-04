"""Unit tests for the SQL AST Guardrails and Security module."""

import pytest

from app.security import (
    GuardrailConfig,
    SQLGuardrailValidator,
    ValidationResult,
    ViolationRule,
    ViolationSeverity,
    get_sql_validator,
    validate_sql,
)


@pytest.fixture
def validator() -> SQLGuardrailValidator:
    """Provide a standard SQLGuardrailValidator using repository SemanticCatalog."""
    config = GuardrailConfig(
        max_limit=1000,
        default_limit=100,
        enforce_limit=True,
    )
    return SQLGuardrailValidator(config=config)


# ==============================================================================
# 1. VALID QUERY & LIMIT ENFORCEMENT TESTS
# ==============================================================================


def test_valid_select_query_injects_default_limit(validator: SQLGuardrailValidator) -> None:
    """Verify that a valid SELECT query without LIMIT has default LIMIT injected."""
    sql = "SELECT order_id, order_status FROM business.orders"
    res = validator.validate(sql)

    assert res.is_valid is True
    assert len(res.violations) == 0
    assert res.applied_limit == 100
    assert res.sanitized_sql is not None
    assert "LIMIT 100" in res.sanitized_sql
    assert "business.orders" in res.tables_referenced
    assert any("No LIMIT clause detected" in w for w in res.warnings)


def test_valid_select_retains_acceptable_limit(validator: SQLGuardrailValidator) -> None:
    """Verify that an existing LIMIT within bounds is preserved."""
    sql = "SELECT customer_id FROM business.customers LIMIT 45"
    res = validator.validate(sql)

    assert res.is_valid is True
    assert res.original_limit == 45
    assert res.applied_limit == 45
    assert res.sanitized_sql is not None
    assert "LIMIT 45" in res.sanitized_sql


def test_excessive_limit_clamped_to_max(validator: SQLGuardrailValidator) -> None:
    """Verify that a LIMIT exceeding max_limit (1000) is clamped."""
    sql = "SELECT product_id FROM business.products LIMIT 5000"
    res = validator.validate(sql)

    assert res.is_valid is True
    assert res.original_limit == 5000
    assert res.applied_limit == 1000
    assert res.sanitized_sql is not None
    assert "LIMIT 1000" in res.sanitized_sql
    assert any("exceeded maximum allowed" in w for w in res.warnings)


def test_valid_multi_table_join_query(validator: SQLGuardrailValidator) -> None:
    """Verify that queries joining approved tables pass validation."""
    sql = """
    SELECT
        o.order_id,
        c.customer_state,
        oi.price
    FROM business.orders o
    JOIN business.customers c ON o.customer_id = c.customer_id
    JOIN business.order_items oi ON o.order_id = oi.order_id
    WHERE o.order_status = 'delivered'
    """
    res = validator.validate(sql)

    assert res.is_valid is True
    assert "business.orders" in res.tables_referenced
    assert "business.customers" in res.tables_referenced
    assert "business.order_items" in res.tables_referenced


def test_valid_cte_query(validator: SQLGuardrailValidator) -> None:
    """Verify that Common Table Expressions (CTEs) are supported and CTE aliases not flagged."""
    sql = """
    WITH monthly_orders AS (
        SELECT
            customer_id,
            COUNT(*) AS order_count
        FROM business.orders
        GROUP BY customer_id
    )
    SELECT *
    FROM monthly_orders
    WHERE order_count > 2
    """
    res = validator.validate(sql)

    assert res.is_valid is True
    assert res.has_cte is True
    assert "business.orders" in res.tables_referenced
    # 'monthly_orders' is a CTE alias and should not be treated as a missing physical table
    assert "business.monthly_orders" not in res.tables_referenced


def test_valid_union_all_query(validator: SQLGuardrailValidator) -> None:
    """Verify that UNION queries pass and limits are applied."""
    sql = """
    SELECT order_id FROM business.orders WHERE order_status = 'delivered'
    UNION ALL
    SELECT order_id FROM business.orders WHERE order_status = 'shipped'
    """
    res = validator.validate(sql)

    assert res.is_valid is True
    assert res.applied_limit == 100
    assert "business.orders" in res.tables_referenced


def test_unqualified_table_resolves_to_default_schema(validator: SQLGuardrailValidator) -> None:
    """Verify that unqualified table 'orders' is mapped to 'business.orders'."""
    sql = "SELECT order_id FROM orders"
    res = validator.validate(sql)

    assert res.is_valid is True
    assert "business.orders" in res.tables_referenced


# ==============================================================================
# 2. SELECT-ONLY & MUTATING STATEMENTS REJECTION TESTS
# ==============================================================================


@pytest.mark.parametrize(
    "mutating_sql",
    [
        "DELETE FROM business.orders",
        "DROP TABLE business.orders",
        "TRUNCATE business.orders",
        "UPDATE business.orders SET order_status = 'canceled'",
        "INSERT INTO business.orders (order_id) VALUES ('123')",
        "ALTER TABLE business.orders ADD COLUMN temp_col int",
        "CREATE TABLE hack (id int)",
    ],
)
def test_reject_mutating_sql_statements(validator: SQLGuardrailValidator, mutating_sql: str) -> None:
    """Verify that DDL and DML operations are strictly blocked."""
    res = validator.validate(mutating_sql)

    assert res.is_valid is False
    assert res.sanitized_sql is None
    rules = [v.rule for v in res.violations]
    assert ViolationRule.SELECT_ONLY in rules


def test_reject_select_into(validator: SQLGuardrailValidator) -> None:
    """Verify that SELECT INTO is blocked because it creates a new table."""
    sql = "SELECT * INTO stolen_orders FROM business.orders"
    res = validator.validate(sql)

    assert res.is_valid is False
    rules = [v.rule for v in res.violations]
    assert ViolationRule.DISALLOWED_OPERATION in rules or ViolationRule.SELECT_ONLY in rules


def test_reject_mutation_inside_cte(validator: SQLGuardrailValidator) -> None:
    """Verify that mutating operations embedded inside CTEs are caught."""
    sql = """
    WITH malicious AS (
        DELETE FROM business.orders RETURNING *
    )
    SELECT * FROM malicious
    """
    res = validator.validate(sql)

    assert res.is_valid is False
    rules = [v.rule for v in res.violations]
    assert ViolationRule.SELECT_ONLY in rules


# ==============================================================================
# 3. MULTIPLE STATEMENTS & SQL INJECTION TESTS
# ==============================================================================


def test_reject_multiple_statements(validator: SQLGuardrailValidator) -> None:
    """Verify that piggybacked statements (SQL injection attempt) are blocked."""
    sql = "SELECT order_id FROM business.orders; DROP TABLE business.customers;"
    res = validator.validate(sql)

    assert res.is_valid is False
    rules = [v.rule for v in res.violations]
    assert ViolationRule.MULTIPLE_STATEMENTS in rules


def test_single_statement_with_comments_and_semicolon(validator: SQLGuardrailValidator) -> None:
    """Verify that trailing comments and single semicolon are accepted."""
    sql = "SELECT order_id FROM business.orders; -- benign comment"
    res = validator.validate(sql)

    assert res.is_valid is True
    assert res.applied_limit == 100


# ==============================================================================
# 4. UNAUTHORIZED TABLES & SYSTEM SCHEMAS TESTS
# ==============================================================================


def test_reject_system_schemas(validator: SQLGuardrailValidator) -> None:
    """Verify that querying pg_catalog or information_schema is rejected."""
    sql1 = "SELECT * FROM pg_catalog.pg_tables"
    res1 = validator.validate(sql1)
    assert res1.is_valid is False
    assert any(v.rule == ViolationRule.DISALLOWED_TABLE for v in res1.violations)

    sql2 = "SELECT * FROM information_schema.columns"
    res2 = validator.validate(sql2)
    assert res2.is_valid is False
    assert any(v.rule == ViolationRule.DISALLOWED_TABLE for v in res2.violations)


def test_reject_non_catalog_table(validator: SQLGuardrailValidator) -> None:
    """Verify that tables outside the semantic catalog allowlist are rejected."""
    sql = "SELECT * FROM app.users"
    res = validator.validate(sql)

    assert res.is_valid is False
    assert any(v.rule == ViolationRule.DISALLOWED_TABLE for v in res.violations)


# ==============================================================================
# 5. DANGEROUS FUNCTIONS TESTS
# ==============================================================================


@pytest.mark.parametrize(
    "func_sql",
    [
        "SELECT pg_sleep(10) FROM business.orders",
        "SELECT pg_read_file('config.json') FROM business.orders",
        "SELECT current_setting('server_version')",
        "SELECT set_config('work_mem', '1GB', false)",
        "SELECT version()",
        "SELECT dblink_connect('myconn')",
    ],
)
def test_reject_dangerous_functions(validator: SQLGuardrailValidator, func_sql: str) -> None:
    """Verify that dangerous system and file I/O functions are rejected."""
    res = validator.validate(func_sql)

    assert res.is_valid is False
    rules = [v.rule for v in res.violations]
    assert ViolationRule.DANGEROUS_FUNCTION in rules


# ==============================================================================
# 6. SYNTAX ERRORS & EDGE CASES
# ==============================================================================


def test_reject_empty_sql(validator: SQLGuardrailValidator) -> None:
    """Verify that empty SQL strings are rejected."""
    res = validator.validate("   ")
    assert res.is_valid is False
    assert any(v.rule == ViolationRule.SYNTAX_ERROR for v in res.violations)


def test_reject_malformed_syntax(validator: SQLGuardrailValidator) -> None:
    """Verify that malformed SQL syntax returns a clear SYNTAX_ERROR violation."""
    res = validator.validate("SELECT FROM WHERE 123;;;")
    assert res.is_valid is False
    assert any(v.rule == ViolationRule.SYNTAX_ERROR for v in res.violations)


def test_reject_negative_limit(validator: SQLGuardrailValidator) -> None:
    """Verify that negative limits trigger violation."""
    sql = "SELECT order_id FROM business.orders LIMIT -5"
    res = validator.validate(sql)
    assert res.is_valid is False
    assert any(v.rule == ViolationRule.LIMIT_EXCEEDED for v in res.violations)


# ==============================================================================
# 7. STRICT COLUMN CHECKING TESTS
# ==============================================================================


def test_strict_column_checking() -> None:
    """Verify that strict column mode rejects uncataloged columns."""
    strict_val = SQLGuardrailValidator(strict_columns=True)

    # Valid column
    good_sql = "SELECT order_id, order_status FROM business.orders"
    good_res = strict_val.validate(good_sql)
    assert good_res.is_valid is True

    # Invalid nonexistent column
    bad_sql = "SELECT order_id, nonexistent_password_hash FROM business.orders"
    bad_res = strict_val.validate(bad_sql)
    assert bad_res.is_valid is False
    assert any(v.rule == ViolationRule.DISALLOWED_COLUMN for v in bad_res.violations)


# ==============================================================================
# 8. CONVENIENCE API & JSON SERIALIZATION TESTS
# ==============================================================================


def test_validate_sql_top_level_function() -> None:
    """Verify convenience top-level validate_sql function and get_sql_validator."""
    val1 = get_sql_validator()
    val2 = get_sql_validator()
    assert val1 is val2

    res = validate_sql("SELECT customer_city FROM business.customers")
    assert isinstance(res, ValidationResult)
    assert res.is_valid is True
    assert res.applied_limit == 100

    bad_res = validate_sql("DROP TABLE business.orders")
    assert bad_res.is_valid is False
    assert any(v.severity == ViolationSeverity.CRITICAL for v in bad_res.violations)


def test_validation_result_json_serialization(validator: SQLGuardrailValidator) -> None:
    """Verify that ValidationResult can be dumped to JSON."""
    res = validator.validate("SELECT order_id FROM business.orders")
    json_str = res.model_dump_json()

    assert isinstance(json_str, str)
    assert "business.orders" in json_str
    assert "is_valid" in json_str
