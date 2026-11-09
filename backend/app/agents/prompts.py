"""Prompt templates for Text-to-SQL generation and self-repair feedback loops."""

from app.agents.schemas import SQLGenerationRequest
from app.semantic.retrieval.schemas import (
    ResolvedJoinPath,
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
)

SQL_SYSTEM_PROMPT = """\
You are an expert PostgreSQL analytics query writer for Brazilian e-commerce data.

Your job: convert the user's business question into ONE read-only SQL query \
using ONLY the semantic context provided below.

Hard rules:
1. Output EXACTLY ONE SQL statement wrapped in a ```sql fenced code block. \
No prose, no explanations, no second query.
2. SELECT-only: no INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, \
SELECT ... INTO, or system catalogs (pg_catalog, information_schema).
3. Reference only tables and columns listed in the SQL CONTEXT below. \
Always qualify tables as schema.table (e.g. business.orders).
4. Follow aggregate rules exactly:
   - Use the metric `expression` verbatim as the SELECT payload.
   - Apply every mandatory metric filter verbatim in WHERE.
   - Use the dimension `source_table.source_column` and its declared \
transformations for grouping/date extraction.
5. Follow the resolved join path: root FROM table first, then the given \
join steps with their join types and ON conditions. Never invent joins.
6. Heed every WARNING: when a fan-out or chasm-trap warning is shown \
(or the flat-join flag says the path is unsafe), pre-aggregate the \
fact tables in CTEs (WITH ... AS) before joining, so each row \
contributes exactly once to every SUM/COUNT.
7. GROUP BY every non-aggregated SELECT column and alias aggregates \
meaningfully. Add ORDER BY (aggregates descending first) when it makes \
the answer readable.
8. Never wrap an aggregate inside DISTINCT to hide fan-out.
"""


SQL_USER_CONTEXT_TEMPLATE = """\
### Business Question
{question}

### Retrieval Status
Status: {status} (confidence: {confidence:.2f})

### Metrics (compute all selected)
{metrics}

### Dimensions (group / slice by)
{dimensions}

### SQL CONTEXT
- Resolved join path (root FROM table first):
{join_path}
- Mandatory filters from retrieval:
{mandatory_filters}
- Retrieval warnings:
{retrieval_warnings}
{flat_join_note}

Write the complete SQL query now.
"""


SQL_REPAIR_FEEDBACK_TEMPLATE = """\
The previous SQL query failed security and ast guardrail checks \
(repair attempt {attempt}/{max_attempts}).

### Offending Query
```sql
{generated_sql}
```

### Guardrail Violations
{violations}

### Warnings
{warnings}

### Instruction
{extra_instruction}
Rewrite the query fixing ONLY the reported issues while preserving the \
original business intent. Respect the semantic context: use every selected \
metric expression verbatim and apply every mandatory filter. Reply with the \
corrected single query in a ```sql fenced block.
"""


SQL_EXECUTION_REPAIR_FEEDBACK_TEMPLATE = """\
The previous SQL query passed all guardrail checks but FAILED while \
executing against PostgreSQL (repair attempt {attempt}/{max_attempts}).

### Offending Query
```sql
{generated_sql}
```

### Database Error
- SQLSTATE: {error_code}
- Message: {error_message}
- Likely cause: {error_hint}

### Instruction
{error_hint} Rewrite the query to fix ONLY this error while preserving \
the original business intent. Reference only tables and columns from the \
semantic context given earlier. Reply with the corrected single query in \
a ```sql fenced block.
"""


# PostgreSQL SQLSTATE -> repair guidance shown to the LLM in execution repair prompts.
POSTGRES_ERROR_HINTS: dict[str, str] = {
    "42703": "The query references a column that does not exist on the table. "
    "Use ONLY columns from the semantic context and re-check spelling/aliases.",
    "42P01": "The query references a table that does not exist. "
    "Always qualify tables as schema.table and use only tables from the join path.",
    "42601": "The query contains a syntax error near a keyword or operator. "
    "Check the PostgreSQL grammar around the reported position.",
    "42883": "The query calls a function with wrong arguments or a non-existent function. "
    "Use only standard PostgreSQL functions with valid argument types.",
    "42702": "A column reference is ambiguous between multiple joined tables. "
    "Qualify every column with its table alias.",
    "42803": "An aggregate or non-grouped column appears outside GROUP BY rules. "
    "GROUP BY every non-aggregated SELECT column.",
    "42P10": "The GROUP BY expression does not match the SELECT list. "
    "Align GROUP BY with the non-aggregated SELECT columns.",
    "22012": "A division by zero occurred. Guard denominators with NULLIF(denominator, 0).",
    "22P02": "A literal value is not convertible to the target column type. "
    "Match literal formats to column data types.",
    "57014": "The query was cancelled because it exceeded the execution timeout. "
    "Restrict the time window and reduce the scanned row count.",
}


def describe_execution_error(error_code: str | None) -> str:
    """Return LLM repair guidance for a PostgreSQL SQLSTATE or execution error code."""
    if error_code is None:
        return "Inspect the database error message and fix the query accordingly."
    return POSTGRES_ERROR_HINTS.get(
        error_code,
        "Inspect the database error message and fix the query accordingly.",
    )


def _render_metrics(metrics: list[SelectedMetric]) -> str:
    """Render selected metrics with expressions, filters, and warnings for the prompt."""
    if not metrics:
        return "- (none matched — return a query that best estimates the intent)"
    lines: list[str] = []
    for metric in metrics:
        lines.append(
            f"- Metric `{metric.name}` [aggregation: {metric.aggregation}, "
            f"tables: {', '.join(metric.source_tables)}]"
        )
        lines.append(f"  SELECT expression (use verbatim): {metric.expression}")
        for condition in metric.filters:
            lines.append(f"  Required filter: {condition.condition}")
        for warning in metric.warnings:
            lines.append(f"  WARNING: {warning}")
    return "\n".join(lines)


def _render_dimensions(dimensions: list[SelectedDimension]) -> str:
    """Render selected dimensions with sources, transformers, and allowed values."""
    if not dimensions:
        return "- (none matched)"
    lines: list[str] = []
    for dimension in dimensions:
        lines.append(
            f"- Dimension `{dimension.name}`: "
            f"{dimension.source_table}.{dimension.source_column}"
            f" (table alias: {dimension.source_table})"
        )
        for transformation in dimension.transformations:
            lines.append(f"  Transformation `{transformation.name}`: {transformation.expression}")
        if dimension.allowed_values:
            lines.append(f"  Allowed values: {', '.join(dimension.allowed_values[:15])}")
    return "\n".join(lines)


def _render_join_path(join_path: ResolvedJoinPath | None) -> str:
    """Render the graph-resolved join sequence as ordered procedural steps."""
    if join_path is None:
        return "- (no explicit path resolved; JOIN only on tables sharing declared foreign keys)"
    lines = [f"- Root FROM: {join_path.root_table}"]
    for idx, step in enumerate(join_path.steps, start=1):
        safety = (
            "safe for aggregation" if step.is_safe_for_aggregation else "UNSAFE for aggregation"
        )
        lines.append(
            f"  {idx}. {step.join_type} JOIN {step.to_table} ON {step.join_condition} "
            f"[{step.cardinality}, {safety}]"
        )
        if step.warning:
            lines.append(f"     WARNING: {step.warning}")
    if join_path.intermediate_tables:
        lines.append(f"- Bridge tables required: {', '.join(join_path.intermediate_tables)}")
    if join_path.warnings:
        for warning in join_path.warnings:
            lines.append(f"- PATH WARNING: {warning}")
    return "\n".join(lines)


def _render_filters(filters: list) -> str:  # noqa: ANN401 - list[FilterCondition]
    """Render warehouse-level mandatory filters."""
    if not filters:
        return "- (none)"
    return "\n".join(f"- [{condition.description}] {condition.condition}" for condition in filters)


def _render_warnings(warnings: list[str]) -> str:
    """Render retrieval warnings as bullet list."""
    if not warnings:
        return "- (none)"
    return "\n".join(f"- {warning}" for warning in warnings)


def render_system_prompt() -> str:
    """Return the static system prompt defining SQL generation rules."""
    return SQL_SYSTEM_PROMPT


def render_user_prompt(
    question: str,
    retrieval_result: RetrievalResult,
) -> str:
    """Render the business question plus semantic retrieval context for the LLM."""
    unsafe_join_note = ""
    if not retrieval_result.is_safe_for_direct_execution:
        unsafe_join_note = (
            "- Flat-join aggregation is UNSAFE for this question (chasm trap or fan-out). "
            "Pre-aggregate fact tables in CTEs before joining them."
        )
    return SQL_USER_CONTEXT_TEMPLATE.format(
        question=question,
        status=retrieval_result.status.value
        if isinstance(retrieval_result.status, RetrievalStatus)
        else str(retrieval_result.status),
        confidence=retrieval_result.confidence_score,
        metrics=_render_metrics(retrieval_result.selected_metrics),
        dimensions=_render_dimensions(retrieval_result.selected_dimensions),
        join_path=_render_join_path(retrieval_result.resolved_join_path),
        mandatory_filters=_render_filters(retrieval_result.mandatory_filters),
        retrieval_warnings=_render_warnings(retrieval_result.warnings),
        flat_join_note=unsafe_join_note,
    )


def render_repair_feedback_prompt(
    generated_sql: str,
    violations: list[str],
    warnings: list[str],
    attempt: int,
    max_attempts: int,
) -> str:
    """Render corrective feedback listing guardrail violations for the repair turn."""
    bullet_violations = "\n".join(f"- {v}" for v in violations) or "- (none reported)"
    bullet_warnings = "\n".join(f"- {w}" for w in warnings) or "- (none)"
    return SQL_REPAIR_FEEDBACK_TEMPLATE.format(
        attempt=attempt,
        max_attempts=max_attempts,
        generated_sql=generated_sql,
        violations=bullet_violations,
        warnings=bullet_warnings,
        extra_instruction="Fix every CRITICAL and HIGH violation before resubmitting.",
    )


def render_execution_repair_feedback_prompt(
    generated_sql: str,
    error_code: str | None,
    error_message: str | None,
    attempt: int,
    max_attempts: int,
) -> str:
    """Render corrective feedback for a failed PostgreSQL execution turn."""
    return SQL_EXECUTION_REPAIR_FEEDBACK_TEMPLATE.format(
        attempt=attempt,
        max_attempts=max_attempts,
        generated_sql=generated_sql,
        error_code=error_code or "UNKNOWN",
        error_message=error_message or "(no message reported)",
        error_hint=describe_execution_error(error_code),
    )


def build_initial_messages(request: SQLGenerationRequest) -> list[dict[str, str]]:
    """Build the chat messages for the first generation turn."""
    return [
        {"role": "system", "content": render_system_prompt()},
        {"role": "user", "content": render_user_prompt(request.question, request.retrieval_result)},
    ]
