"""Business intent conformance checks (SQL candidate vs. retrieval contract).

The AST guardrails answer "is this query safe to run?". These checks answer the
complementary question "is this query the one the semantic layer resolved for
the user's question?". Every rule compares the candidate SQL AST against the
``RetrievalResult`` produced by the retriever:

- INTENT_MISSING_FILTER         (HIGH)    a mandatory metric filter predicate
                                          is absent from the candidate SQL
- INTENT_METRIC_MISMATCH        (SOFT)    no aggregate in the candidate matches
                                          the metric expression verbatim
- INTENT_REQUIRED_TABLE_MISSING (SOFT)    a table required by the selected
                                          metrics/dimensions/join path is not
                                          referenced

SOFT rules are surfaced as blocking violations only when the guardrail runs in
``intent_strict_mode``; otherwise they degrade to warnings so ambiguous matches
never stop execution. Unparseable filter/expressions are ignored (fail-open)
rather than guessed at, to avoid pointless repair loops.
"""

import sqlglot
from sqlglot import exp

from app.core.logging import get_logger
from app.security.schemas import SecurityViolation, ViolationRule, ViolationSeverity
from app.semantic.retrieval.schemas import RetrievalResult

logger = get_logger(__name__)

# Operator classes considered semantically equivalent when matching a mandatory
# filter predicate (candidate SQL is free to write IN (x) as = x, and to
# case-fold LIKE/ILIKE).
_OPERATOR_ALIASES: dict[str, set[str]] = {
    "LIKE": {"LIKE", "ILIKE"},
    "ILIKE": {"LIKE", "ILIKE"},
    "IN": {"IN", "EQ"},  # single-element IN -- equals
    "EQ": {"EQ", "IN"},  # single-element IN -- equals
}


def _normalize_value(value: str) -> str:
    """Normalize a literal value for comparison (case, quotes, whitespace)."""
    return value.strip().strip("'\"").lower()


def _extract_signature(predicate: exp.Expr) -> tuple[str, str, tuple[str, ...]] | None:
    """Build a comparable signature from one SQL predicate.

    Returns ``(column_name, operator, normalized_literals)`` or ``None`` when
    the predicate cannot be pinned to a single column (fail-open).
    """
    if isinstance(predicate, exp.Connector | exp.Unary | exp.Null):
        return None

    column_node = predicate.find(exp.Column)
    if column_node is None:
        return None

    column = column_node.name.lower()
    literals = tuple(
        sorted(
            _normalize_value(str(literal.this))
            # ``this`` is a bool for TRUE/FALSE literals and None for NULL
            for literal in predicate.find_all(exp.Literal)
            if literal.this is not None
        )
    )
    return column, type(predicate).__name__.upper(), literals


def _operator_equivalent(filter_op: str, candidate_op: str) -> bool:
    """Whether two operator codes may substitute for each other."""
    if filter_op == candidate_op:
        return True
    return candidate_op in _OPERATOR_ALIASES.get(filter_op, set())


def _check_missing_filters(
    ast: exp.Expr,
    retrieval: RetrievalResult,
) -> list[SecurityViolation]:
    """HIGH: every mandatory filter predicate must exist in the candidate SQL."""
    violations: list[SecurityViolation] = []
    candidate_signatures: list[tuple[str, str, tuple[str, ...]]] = []
    for predicate in ast.find_all(exp.Predicate):
        signature = _extract_signature(predicate)
        if signature is not None:
            candidate_signatures.append(signature)

    seen_conditions: set[str] = set()
    # Metric-level filters are the source of truth; retrieval-level mandatory
    # filters (aggregated by the join resolver) are merged in for safety.
    conditions = [
        condition
        for metric in retrieval.selected_metrics
        for condition in metric.filters
    ] + list(retrieval.mandatory_filters)
    for condition in conditions:
        if condition.condition in seen_conditions:
            continue
        seen_conditions.add(condition.condition)

        try:
            filter_ast = sqlglot.parse_one(condition.condition, dialect="postgres")
        except Exception:
            logger.warning("Ignored unparseable mandatory filter: %s", condition.condition)
            continue

        signature = _extract_signature(filter_ast)
        if signature is None:
            continue

        if not any(
            signature[0] == candidate[0]
            and _operator_equivalent(signature[1], candidate[1])
            and signature[2] == candidate[2]
            for candidate in candidate_signatures
        ):
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.INTENT_MISSING_FILTER,
                    message=(
                        f"Missing mandatory filter `{condition.condition}`. "
                        f"All filters required by the selected metrics must appear "
                        f"verbatim in the query ({condition.description})."
                    ),
                    severity=ViolationSeverity.HIGH,
                    details={"filter": condition.condition},
                )
            )
    return violations


def _canonical_expression(node: exp.Expr) -> str:
    """Render an expression canonically: aliases and table qualifiers stripped."""
    node = node.copy()
    if isinstance(node, exp.Alias):
        node = node.this
    for column in node.find_all(exp.Column):
        column.set("table", None)
        column.set("db", None)
    return node.sql(dialect="postgres")


def _check_metric_conformance(
    ast: exp.Expr,
    retrieval: RetrievalResult,
) -> list[SecurityViolation]:
    """SOFT: each selected metric expression must appear among the aggregates."""
    candidate_aggregates: list[str] = [
        _canonical_expression(aggregate)
        for aggregate in ast.find_all(exp.AggFunc)
    ]
    if not candidate_aggregates:
        candidate_aggregates = [
            _canonical_expression(aggregate)
            for aggregate in ast.find_all(exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)
        ]

    violations: list[SecurityViolation] = []
    for metric in retrieval.selected_metrics:
        try:
            metric_canonical = _canonical_expression(
                sqlglot.parse_one(metric.expression, dialect="postgres")
            )
        except Exception:
            logger.warning(
                "Skipping intent conformance for unparseable metric expression: %s",
                metric.expression,
            )
            continue

        if metric_canonical not in candidate_aggregates:
            detail = (
                f"Candidate aggregates: {candidate_aggregates or ['(none)']}."
            )
            violations.append(
                SecurityViolation(
                    rule=ViolationRule.INTENT_METRIC_MISMATCH,
                    message=(
                        f"Aggregation does not match metric `{metric.name}` verbatim. "
                        f"Expected `{metric_canonical}` (aggregation {metric.aggregation}). "
                        f"{detail}"
                    ),
                    severity=ViolationSeverity.MEDIUM,
                    details={"metric": metric.name, "expected": metric_canonical},
                )
            )
    return violations


def _required_tables(retrieval: RetrievalResult, default_schema: str) -> set[str]:
    """Collect the fully-qualified table set implied by the retrieval contract."""
    def normalize(table: str) -> str:
        table = table.strip().lower()
        if "." not in table:
            return f"{default_schema}.{table}"
        return table

    required: set[str] = set()
    for metric in retrieval.selected_metrics:
        required.update(normalize(table) for table in metric.source_tables)
    for dimension in retrieval.selected_dimensions:
        required.add(normalize(dimension.source_table))

    join_path = retrieval.resolved_join_path
    if join_path is not None:
        required.add(normalize(join_path.root_table))
        required.update(normalize(table) for table in join_path.required_tables)
        required.update(normalize(table) for table in join_path.intermediate_tables)
    return required


def _check_required_tables(
    ast: exp.Expr,
    retrieval: RetrievalResult,
    tables_referenced: set[str],
    default_schema: str,
) -> list[SecurityViolation]:
    """SOFT: every table required by the join plan must be referenced."""
    required = _required_tables(retrieval, default_schema)
    referenced = {table.lower() for table in tables_referenced}
    missing = sorted(required - referenced)
    if not missing:
        return []

    return [
        SecurityViolation(
            rule=ViolationRule.INTENT_REQUIRED_TABLE_MISSING,
            message=(
                f"Tables required by the resolved join plan are not referenced: "
                f"{', '.join(missing)}. Follow the resolved join path from the "
                f"semantic context."
            ),
            severity=ViolationSeverity.MEDIUM,
            details={"missing_tables": missing},
        )
    ]


def check_business_intent(
    ast: exp.Expr,
    retrieval: RetrievalResult,
    *,
    tables_referenced: set[str] | None = None,
    default_schema: str = "business",
) -> list[SecurityViolation]:
    """Run every intent conformance rule against the candidate AST.

    Args:
        ast: Parsed candidate SQL statement.
        retrieval: RetrievalResult resolved for the user's question.
        tables_referenced: Fully-qualified table names referenced by the SQL
            (already computed by the guardrail validator; required for the
            required-tables rule).
        default_schema: Schema assumed for unqualified table names.

    Returns:
        Constituent SecurityViolation list. Rules tagged HIGH block execution;
        MEDIUM rules are downgraded to warnings by the caller unless the
        guardrail runs in strict mode.
    """
    if not retrieval.selected_metrics and not retrieval.selected_dimensions:
        return []

    violations: list[SecurityViolation] = []

    violations.extend(_check_missing_filters(ast, retrieval))
    violations.extend(_check_metric_conformance(ast, retrieval))
    if tables_referenced is not None:
        violations.extend(
            _check_required_tables(ast, retrieval, tables_referenced, default_schema)
        )

    return violations
