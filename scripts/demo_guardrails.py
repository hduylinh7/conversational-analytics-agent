"""Interactive CLI test runner for the SQL AST Guardrails module."""

import argparse
import io
import json
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Configure stdout for UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.security import validate_sql


DEMO_CASES = [
    (
        "1. Valid Query without LIMIT (Auto-inject default LIMIT 100)",
        "SELECT order_id, order_status FROM business.orders WHERE order_status = 'delivered'",
    ),
    (
        "2. Valid Query with Excessive LIMIT (Auto-clamped to max 1000)",
        "SELECT customer_id, customer_city FROM business.customers LIMIT 5000",
    ),
    (
        "3. Valid Query with Common Table Expressions (CTE)",
        """WITH order_aggs AS (
    SELECT customer_id, COUNT(*) AS total_orders
    FROM business.orders
    GROUP BY customer_id
)
SELECT * FROM order_aggs WHERE total_orders > 5""",
    ),
    (
        "4. Blocked Mutating Query (DDL / DROP TABLE)",
        "DROP TABLE business.orders",
    ),
    (
        "5. Blocked Multi-Statement Injection (Piggybacked query)",
        "SELECT order_id FROM business.orders; DROP TABLE business.customers;",
    ),
    (
        "6. Blocked Dangerous Function (Denial of Service / pg_sleep)",
        "SELECT pg_sleep(10) FROM business.orders",
    ),
    (
        "7. Blocked Unauthorized System Schema (pg_catalog)",
        "SELECT * FROM pg_catalog.pg_tables",
    ),
]


def print_validation_report(title: str, sql: str) -> None:
    print("=" * 80)
    print(f"CASE: {title}")
    print("-" * 80)
    print(f"INPUT SQL:\n{sql}\n")
    res = validate_sql(sql)

    print(f"STATUS        : {'[PASSED] VALID' if res.is_valid else '[BLOCKED] INVALID'}")
    print(f"ORIGINAL LIMIT: {res.original_limit}")
    print(f"APPLIED LIMIT : {res.applied_limit}")
    print(f"TABLES FOUND  : {res.tables_referenced}")
    print(f"HAS CTE       : {res.has_cte}")

    if res.warnings:
        print("\nWARNINGS:")
        for w in res.warnings:
            print(f"  * {w}")

    if res.violations:
        print("\nVIOLATIONS DETECTED:")
        for v in res.violations:
            print(f"  - [{v.severity}] {v.rule}: {v.message}")

    if res.sanitized_sql:
        print("\nSANITIZED SQL:")
        print(res.sanitized_sql)
    print("=" * 80 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test SQL Guardrails and AST Validation for arbitrary queries."
    )
    parser.add_argument(
        "sql",
        nargs="?",
        default=None,
        help="Custom SQL query string to inspect. If omitted, runs a suite of demonstration test cases.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON output of ValidationResult.",
    )
    args = parser.parse_args()

    if args.sql:
        if args.json:
            res = validate_sql(args.sql)
            print(json.dumps(res.model_dump(), indent=2, ensure_ascii=False))
        else:
            print_validation_report("Custom User Query", args.sql)
    else:
        print("\n>>> RUNNING SQL GUARDRAIL DEMONSTRATION SUITE <<<\n")
        for title, sample_sql in DEMO_CASES:
            print_validation_report(title, sample_sql)


if __name__ == "__main__":
    main()

