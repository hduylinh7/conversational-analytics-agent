# SQL Guardrails & AST Validation Architecture

The **SQL Guardrails & AST Validation Module** ([`backend/app/security/`](file:///d:/conversational-analytics-agent/backend/app/security/)) serves as the security perimeter between the LLM-generated SQL queries and the analytical database.

It uses Abstract Syntax Tree (AST) inspection via `sqlglot` to enforce read-only semantics, validate catalog entity access, intercept dangerous functions, prevent SQL injection, and clamp or inject row limits without executing the query against PostgreSQL.

It also performs **Business Intent Validation** (§6): when the caller passes the
`RetrievalResult` resolved for the user's question, the guardrail additionally
checks that the candidate SQL conforms to the business contract the semantic
layer resolved — metric expressions verbatim, mandatory filters present, and
the resolved join-plan tables referenced — before execution.

---

## 1. Architecture Overview

```text
LLM Generated SQL (or user query)
               │
               ▼
┌────────────────────────────────────────────────────────┐
│               sqlglot PostgreSQL Parser               │
│ - Syntax validity validation                           │
│ - Single-statement enforcement                         │
│ - Rejection of piggybacked or multi-statement queries  │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│            AST Policy & Security Checks                │
│ 1. SELECT-Only Enforcement:                            │
│    - Root must be Select or Union                      │
│    - Zero mutating nodes (INSERT, UPDATE, DELETE,      │
│      DROP, ALTER, TRUNCATE, CREATE, INTO)              │
│    - Deep AST traversal (catches mutations in CTEs)    │
│ 2. Prohibited Function Detection:                      │
│    - File I/O (pg_read_file, lo_import)               │
│    - Denial of Service (pg_sleep, cancel_backend)      │
│    - External connection (dblink, system, eval)        │
│ 3. Table & Schema Allowlisting:                        │
│    - Whitelist validation against Semantic Catalog     │
│    - System schema rejection (pg_catalog, etc.)        │
│    - Virtual CTE alias resolution                      │
│ 4. Optional Strict Column Validation                   │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│             LIMIT Injection & Clamping                 │
│ - Missing LIMIT: Auto-inject default LIMIT (100)       │
│ - Excessive LIMIT: Clamp to max LIMIT (1000)           │
│ - Negative / Malformed LIMIT: Trigger violation        │
└───────────────────────┬────────────────────────────────┘
                        │
                        ▼
                 ValidationResult
 (is_valid, sanitized_sql, violations, warnings, tables, limits)
```

---

## 2. Guardrail Enforcement Rules

| Rule Code | Severity | Description |
| :--- | :--- | :--- |
| `SYNTAX_ERROR` | CRITICAL | Query cannot be parsed as valid PostgreSQL SQL or is completely empty. |
| `MULTIPLE_STATEMENTS` | CRITICAL | Multiple SQL statements detected (prevents SQL injection piggybacking). |
| `SELECT_ONLY` | CRITICAL | Attempted DDL or DML statements (`DROP`, `DELETE`, `UPDATE`, `INSERT`, `TRUNCATE`, `ALTER`, `CREATE`), including mutations hidden within CTEs or subqueries. |
| `DISALLOWED_OPERATION` | CRITICAL | Prohibited operations such as `SELECT ... INTO` or disallowed CTE constructs. |
| `DANGEROUS_FUNCTION` | CRITICAL | Invocation of file I/O, server management, or process execution functions (`pg_sleep`, `pg_read_file`, `set_config`, `system`, `dblink`, etc.). |
| `DISALLOWED_TABLE` | HIGH | Access to system schemas (`pg_catalog`, `information_schema`) or tables not defined in the semantic catalog. |
| `DISALLOWED_COLUMN` | MEDIUM | Columns not recognized in the semantic catalog (when strict column mode is active). |
| `LIMIT_EXCEEDED` | HIGH | Negative or invalid LIMIT clauses. |

---

## 3. Dynamic Limit Enforcement

Unbounded queries can exhaust database memory or lock shared resources. The guardrail module actively manipulates the AST before query execution:

1. **No LIMIT Specified**: Automatically injects `LIMIT 100` (or `SQL_DEFAULT_LIMIT`) and records an informational warning.
2. **Within Threshold**: A query specifying `LIMIT 50` is retained as-is (`original_limit = 50`, `applied_limit = 50`).
3. **Exceeding Threshold**: A query specifying `LIMIT 5000` is automatically clamped to `LIMIT 1000` (or `SQL_MAX_LIMIT`) in the AST, accompanied by a warning.

The output `sanitized_sql` is directly compiled from the safely modified AST, guaranteeing that the exact AST inspected is the one sent to the database.

---

## 4. Virtual CTE Alias Resolution

Analytical queries frequently employ Common Table Expressions (CTEs), especially when addressing **Chasm Traps** (e.g. pre-aggregating `order_items` and `payments` before joining):

```sql
WITH order_item_totals AS (
    SELECT order_id, SUM(price) AS item_revenue
    FROM business.order_items
    GROUP BY order_id
)
SELECT o.order_id, oit.item_revenue
FROM business.orders o
JOIN order_item_totals oit ON o.order_id = oit.order_id;
```

The validator:
1. Extracts all declared CTE aliases (`order_item_totals`).
2. Recognizes `order_item_totals` in subsequent `FROM` or `JOIN` clauses as a virtual local table, rather than rejecting it as an unauthorized database table.
3. Inspects the inner queries of all CTEs to ensure no mutating operations (e.g. `DELETE ... RETURNING *`) are concealed inside.

---

## 5. Usage & Integration

### A. Functional Interface

```python
from app.security import validate_sql

result = validate_sql("SELECT customer_city FROM business.customers")
if result.is_valid:
    # Safe to execute against PostgreSQL
    print(result.sanitized_sql)
else:
    # Log violations or feed back to LLM for SQL self-repair
    for v in result.violations:
        print(f"[{v.severity}] {v.rule}: {v.message}")
```

### B. Custom Configuration

```python
from app.security import GuardrailConfig, SQLGuardrailValidator

config = GuardrailConfig(
    max_limit=500,
    default_limit=50,
    enforce_limit=True,
    allowed_tables={"business.orders", "business.order_items"},
)
validator = SQLGuardrailValidator(config=config, strict_columns=True)
result = validator.validate(sql)
```

---

## 6. Business Intent Validation (SQL vs. Retrieval Contract)

Security checks alone cannot catch *semantically wrong but syntactically
valid* SQL (e.g. the LLM dropping a mandatory `order_status = 'delivered'`
filter). [`security/intent_checks.py`](file:///d:/conversational-analytics-agent/backend/app/security/intent_checks.py)
closes that gap by comparing the candidate SQL AST against the `RetrievalResult`
for the current turn — the same metadata the generation prompt was built from.

### A. Intent Conformance Rules

| Rule Code | Severity | Check |
| :--- | :--- | :--- |
| `INTENT_MISSING_FILTER` | HIGH (blocks) | Every mandatory filter of the selected metrics appears as a predicate in the candidate SQL (matched by column + operator + literal value; `IN ('x')` ≡ `= 'x'`, LIKE/ILIKE interchangeable; matched anywhere in the AST — including inside CTEs built for fan-out pre-aggregation). |
| `INTENT_METRIC_MISMATCH` | MEDIUM | No aggregate in the SQL canonically equals a selected metric's `expression` (compared after stripping aliases/table qualifiers via sqlglot). Catches `COUNT(o.order_id)` vs. the required `COUNT(DISTINCT o.order_id)`, `AVG` instead of `SUM`, etc. |
| `INTENT_REQUIRED_TABLE_MISSING` | MEDIUM | A table required by the selected metrics/dimensions/resolved join path is absent from `tables_referenced`. |

### B. Severity Semantics

- `INTENT_MISSING_FILTER` is a deterministic failure and **always blocks** (repairs through the existing guardrail repair loop).
- The other two rules are ambiguous by nature (LLMs may rename aliases, wrap CTEs, restructure equivalently), so by default they **degrade to warnings** (`result.warnings`) and are surfaced in the response. Set `intent_strict_mode=True` (or `SQL_INTENT_STRICT_MODE` env) to promote them to blocking violations.
- Fail-open: unparseable metric/filter definitions are **ignored** (logged as warnings) rather than guessed at, so broken metadata never causes an infinite repair loop.

### C. Activation

Intent checks only run when the caller forwards the retrieval context:

```python
result = validator.validate(sql, retrieval=retrieval_result)   # full checks
result = validator.validate(sql)                               # security-only (unchanged)
```

LangGraph wiring passes `state["retrieval"]` automatically
([`graph/nodes.py`](file:///d:/conversational-analytics-agent/backend/app/agents/graph/nodes.py));
`SQLGenerator.generate*` passes `request.retrieval_result`.

### D. Configuration

| Setting (env) | Default | Effect |
| :--- | :--- | :--- |
| `SQL_ENFORCE_INTENT_CHECKS` / `GuardrailConfig.enforce_intent_checks` | `True` | Run intent conformance when retrieval context is provided. |
| `SQL_INTENT_STRICT_MODE` / `GuardrailConfig.intent_strict_mode` | `False` | Promote ambiguous (MEDIUM) intent failures to blocking violations. |

---

## 7. Running Tests

The test suite covers valid queries, limit injection, limit clamping, mutating queries, injection attempts, system schemas, dangerous functions, syntax errors, and CTE handling:

```bash
cd backend
pytest tests/test_sql_guardrails.py -v
```

Business intent validation has its own suite (rule behavior, alias tolerance, strict-mode promotion, fail-open behavior, backward compatibility without retrieval context):

```bash
cd backend
pytest tests/test_intent_guardrails.py -v
```

