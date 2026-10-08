#!/usr/bin/env python3
"""
Data Integrity & Business Sanity Validation Pipeline for Olist Business Tables.

Validates:
1. Primary Key Constraints:
   - customers.customer_id
   - orders.order_id
   - products.product_id
   - order_items.(order_id, order_item_id)
   - payments.payment_id
   Checks: NULL values, duplicate values, empty strings.

2. Foreign Key Referential Integrity:
   - orders.customer_id -> customers.customer_id
   - order_items.order_id -> orders.order_id
   - order_items.product_id -> products.product_id (handling nullable product_id correctly)
   - payments.order_id -> orders.order_id
   Calculates: total_child_rows, valid_references, null_references, orphan_rows, orphan_percentage.

3. Business Sanity Rules:
   - order_items: price >= 0, freight_value >= 0, order_item_id > 0
   - payments: payment_value >= 0, payment_installments >= 0, payment_sequential > 0, uniqueness (order_id, payment_sequential)
   - products: populated fields >= 0 (name_len, desc_len, photos_qty, weight_g, length_cm, height_cm, width_cm)
   - orders: delivered_date >= purchase_timestamp (when delivered), status in allowed set
"""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import text

# Add backend directory to sys.path to access app database connection
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.postgres import engine

# Ensure stdout/stderr handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


class IntegrityValidator:
    """Validates primary keys, foreign keys, and business sanity rules in PostgreSQL."""

    def __init__(self, db_engine: Any = engine, schema: str = "business") -> None:
        self.engine = db_engine
        self.schema = schema

    async def validate_primary_keys(self, conn: Any) -> list[dict[str, Any]]:
        """Validate PK completeness, uniqueness, and empty string absence."""
        pk_definitions = [
            {
                "table": "customers",
                "pk_columns": ["customer_id"],
                "is_composite": False,
                "is_string": True,
            },
            {
                "table": "orders",
                "pk_columns": ["order_id"],
                "is_composite": False,
                "is_string": True,
            },
            {
                "table": "products",
                "pk_columns": ["product_id"],
                "is_composite": False,
                "is_string": True,
            },
            {
                "table": "order_items",
                "pk_columns": ["order_id", "order_item_id"],
                "is_composite": True,
                "is_string": False,
            },
            {
                "table": "payments",
                "pk_columns": ["payment_id"],
                "is_composite": False,
                "is_string": False,
            },
        ]

        results = []

        for item in pk_definitions:
            table = item["table"]
            pk_cols = item["pk_columns"]
            full_table = f"{self.schema}.{table}"

            # 1. Row count
            row_cnt = (
                await conn.execute(text(f'SELECT COUNT(*) FROM "{self.schema}"."{table}"'))
            ).scalar() or 0

            # 2. Null count in PK columns
            null_predicates = " OR ".join(f'"{col}" IS NULL' for col in pk_cols)
            null_sql = f'SELECT COUNT(*) FROM "{self.schema}"."{table}" WHERE {null_predicates}'
            null_count = (await conn.execute(text(null_sql))).scalar() or 0

            # 3. Duplicate count
            col_list_str = ", ".join(f'"{col}"' for col in pk_cols)
            dup_sql = f"""
                SELECT COALESCE(SUM(grp_cnt - 1), 0)
                FROM (
                    SELECT {col_list_str}, COUNT(*) AS grp_cnt
                    FROM "{self.schema}"."{table}"
                    GROUP BY {col_list_str}
                    HAVING COUNT(*) > 1
                ) sub
            """
            duplicate_count = int((await conn.execute(text(dup_sql))).scalar() or 0)

            # 4. Empty string count (for string PKs)
            empty_string_count = 0
            if item["is_string"]:
                empty_predicates = " OR ".join(f'TRIM("{col}") = \'\'' for col in pk_cols)
                empty_sql = f'SELECT COUNT(*) FROM "{self.schema}"."{table}" WHERE {empty_predicates}'
                empty_string_count = (await conn.execute(text(empty_sql))).scalar() or 0

            passed = (null_count == 0) and (duplicate_count == 0) and (empty_string_count == 0)

            results.append({
                "table": full_table,
                "primary_key": pk_cols if item["is_composite"] else pk_cols[0],
                "is_composite": item["is_composite"],
                "total_rows": row_cnt,
                "null_count": null_count,
                "duplicate_count": duplicate_count,
                "empty_string_count": empty_string_count,
                "status": "PASS" if passed else "FAIL",
            })

        return results

    async def validate_foreign_keys(self, conn: Any) -> list[dict[str, Any]]:
        """Validate foreign key referential integrity with support for nullable references."""
        fk_definitions = [
            {
                "relation_name": "orders_to_customers",
                "child_table": "orders",
                "child_col": "customer_id",
                "parent_table": "customers",
                "parent_col": "customer_id",
                "is_nullable": False,
            },
            {
                "relation_name": "order_items_to_orders",
                "child_table": "order_items",
                "child_col": "order_id",
                "parent_table": "orders",
                "parent_col": "order_id",
                "is_nullable": False,
            },
            {
                "relation_name": "order_items_to_products",
                "child_table": "order_items",
                "child_col": "product_id",
                "parent_table": "products",
                "parent_col": "product_id",
                "is_nullable": True,
            },
            {
                "relation_name": "payments_to_orders",
                "child_table": "payments",
                "child_col": "order_id",
                "parent_table": "orders",
                "parent_col": "order_id",
                "is_nullable": False,
            },
        ]

        results = []

        for fk in fk_definitions:
            child_tbl = fk["child_table"]
            child_col = fk["child_col"]
            parent_tbl = fk["parent_table"]
            parent_col = fk["parent_col"]

            # SQL query explicitly distinguishing valid references, null references, and orphans
            query_sql = f"""
                SELECT
                    COUNT(*) AS total_child_rows,
                    COUNT(CASE WHEN c."{child_col}" IS NOT NULL AND p."{parent_col}" IS NOT NULL THEN 1 END) AS valid_references,
                    COUNT(CASE WHEN c."{child_col}" IS NULL THEN 1 END) AS null_references,
                    COUNT(CASE WHEN c."{child_col}" IS NOT NULL AND p."{parent_col}" IS NULL THEN 1 END) AS orphan_rows
                FROM "{self.schema}"."{child_tbl}" c
                LEFT JOIN "{self.schema}"."{parent_tbl}" p
                    ON c."{child_col}" = p."{parent_col}"
            """
            row = (await conn.execute(text(query_sql))).mappings().one()

            total_child = int(row["total_child_rows"])
            valid_refs = int(row["valid_references"])
            null_refs = int(row["null_references"])
            orphans = int(row["orphan_rows"])

            orphan_pct = round((orphans / total_child * 100), 4) if total_child > 0 else 0.0
            # Pass condition: 0 orphans. (Null references are allowed if column is nullable)
            passed = (orphans == 0)
            if not fk["is_nullable"] and null_refs > 0:
                passed = False

            results.append({
                "relation_name": fk["relation_name"],
                "child_table": f"{self.schema}.{child_tbl}",
                "child_column": child_col,
                "parent_table": f"{self.schema}.{parent_tbl}",
                "parent_column": parent_col,
                "is_nullable": fk["is_nullable"],
                "total_child_rows": total_child,
                "valid_references": valid_refs,
                "null_references": null_refs,
                "orphan_rows": orphans,
                "orphan_percentage": orphan_pct,
                "status": "PASS" if passed else "FAIL",
            })

        return results

    async def validate_business_sanity(self, conn: Any) -> list[dict[str, Any]]:
        """Validate domain sanity rules across tables."""
        sanity_rules = [
            # order_items rules
            {
                "rule_id": "order_items_price_non_negative",
                "table": "order_items",
                "description": "Item price must be non-negative (price >= 0)",
                "violation_condition": "price < 0",
            },
            {
                "rule_id": "order_items_freight_value_non_negative",
                "table": "order_items",
                "description": "Freight value must be non-negative (freight_value >= 0)",
                "violation_condition": "freight_value < 0",
            },
            {
                "rule_id": "order_items_item_id_positive",
                "table": "order_items",
                "description": "Order item ID must be strictly positive (order_item_id > 0)",
                "violation_condition": "order_item_id <= 0",
            },
            # payments rules
            {
                "rule_id": "payments_value_non_negative",
                "table": "payments",
                "description": "Payment value must be non-negative (payment_value >= 0)",
                "violation_condition": "payment_value < 0",
            },
            {
                "rule_id": "payments_installments_non_negative",
                "table": "payments",
                "description": "Payment installments must be non-negative (payment_installments >= 0)",
                "violation_condition": "payment_installments < 0",
            },
            {
                "rule_id": "payments_sequential_positive",
                "table": "payments",
                "description": "Payment sequential must be strictly positive (payment_sequential > 0)",
                "violation_condition": "payment_sequential <= 0",
            },
            {
                "rule_id": "payments_order_seq_unique",
                "table": "payments",
                "description": "Payment sequence per order must be unique (order_id, payment_sequential)",
                "custom_query": f"""
                    SELECT COALESCE(SUM(grp_cnt - 1), 0)
                    FROM (
                        SELECT order_id, payment_sequential, COUNT(*) AS grp_cnt
                        FROM "{self.schema}"."payments"
                        GROUP BY order_id, payment_sequential
                        HAVING COUNT(*) > 1
                    ) sub
                """,
            },
            # products rules (only for populated non-null rows)
            {
                "rule_id": "products_name_length_non_negative",
                "table": "products",
                "description": "Product name length must be non-negative when present",
                "violation_condition": "product_name_lenght IS NOT NULL AND product_name_lenght < 0",
            },
            {
                "rule_id": "products_description_length_non_negative",
                "table": "products",
                "description": "Product description length must be non-negative when present",
                "violation_condition": "product_description_lenght IS NOT NULL AND product_description_lenght < 0",
            },
            {
                "rule_id": "products_photos_qty_non_negative",
                "table": "products",
                "description": "Product photos quantity must be non-negative when present",
                "violation_condition": "product_photos_qty IS NOT NULL AND product_photos_qty < 0",
            },
            {
                "rule_id": "products_weight_g_non_negative",
                "table": "products",
                "description": "Product weight must be non-negative when present (product_weight_g >= 0)",
                "violation_condition": "product_weight_g IS NOT NULL AND product_weight_g < 0",
            },
            {
                "rule_id": "products_length_cm_non_negative",
                "table": "products",
                "description": "Product length must be non-negative when present (product_length_cm >= 0)",
                "violation_condition": "product_length_cm IS NOT NULL AND product_length_cm < 0",
            },
            {
                "rule_id": "products_height_cm_non_negative",
                "table": "products",
                "description": "Product height must be non-negative when present (product_height_cm >= 0)",
                "violation_condition": "product_height_cm IS NOT NULL AND product_height_cm < 0",
            },
            {
                "rule_id": "products_width_cm_non_negative",
                "table": "products",
                "description": "Product width must be non-negative when present (product_width_cm >= 0)",
                "violation_condition": "product_width_cm IS NOT NULL AND product_width_cm < 0",
            },
            # orders rules
            {
                "rule_id": "orders_delivery_after_purchase",
                "table": "orders",
                "description": "Delivered customer timestamp must be >= purchase timestamp",
                "violation_condition": "order_delivered_customer_date IS NOT NULL AND order_delivered_customer_date < order_purchase_timestamp",
            },
            {
                "rule_id": "orders_valid_order_status",
                "table": "orders",
                "description": "Order status must belong to standard allowed lifecycle values",
                "violation_condition": "order_status NOT IN ('delivered', 'shipped', 'canceled', 'unavailable', 'invoiced', 'processing', 'created', 'approved')",
            },
        ]

        results = []

        for rule in sanity_rules:
            table = rule["table"]
            full_table = f"{self.schema}.{table}"

            if "custom_query" in rule:
                violation_count = int((await conn.execute(text(rule["custom_query"]))).scalar() or 0)
            else:
                condition = rule["violation_condition"]
                check_sql = f'SELECT COUNT(*) FROM "{self.schema}"."{table}" WHERE {condition}'
                violation_count = int((await conn.execute(text(check_sql))).scalar() or 0)

            passed = (violation_count == 0)

            results.append({
                "rule_id": rule["rule_id"],
                "table": full_table,
                "description": rule["description"],
                "violation_count": violation_count,
                "status": "PASS" if passed else "FAIL",
            })

        return results

    async def validate_all(self) -> dict[str, Any]:
        """Execute full suite of integrity validations."""
        async with self.engine.connect() as conn:
            pk_results = await self.validate_primary_keys(conn)
            fk_results = await self.validate_foreign_keys(conn)
            sanity_results = await self.validate_business_sanity(conn)

        total_checks = len(pk_results) + len(fk_results) + len(sanity_results)
        passed_checks = sum(
            1
            for r in (pk_results + fk_results + sanity_results)
            if r["status"] == "PASS"
        )
        failed_checks = total_checks - passed_checks
        overall_status = "PASS" if failed_checks == 0 else "FAIL"

        return {
            "generated_at": datetime.now().isoformat(),
            "schema": self.schema,
            "summary": {
                "total_checks": total_checks,
                "passed_checks": passed_checks,
                "failed_checks": failed_checks,
                "overall_status": overall_status,
            },
            "primary_key_checks": pk_results,
            "foreign_key_checks": fk_results,
            "business_sanity_checks": sanity_results,
        }


def print_integrity_summary(report: dict[str, Any]) -> None:
    """Print structured integrity report to console."""
    print("\n" + "=" * 80)
    print("                     DATA INTEGRITY VALIDATION REPORT")
    print("=" * 80)
    summary = report["summary"]
    status_tag = f"[{summary['overall_status']}]"
    print(f"Generated At:   {report['generated_at']}")
    print(f"Overall Status: {status_tag} ({summary['passed_checks']}/{summary['total_checks']} checks passed)")
    print("-" * 80)

    # 1. Primary Keys
    print("\n--- 1. PRIMARY KEY VALIDATION ---")
    print(f"{'Table':<26} {'Key Column(s)':<24} {'Nulls':<7} {'Dups':<7} {'Empty':<7} {'Status':<6}")
    print("-" * 80)
    for pk in report["primary_key_checks"]:
        key_str = ", ".join(pk["primary_key"]) if pk["is_composite"] else str(pk["primary_key"])
        print(
            f"{pk['table']:<26} {key_str:<24} {pk['null_count']:<7} {pk['duplicate_count']:<7} {pk['empty_string_count']:<7} [{pk['status']}]"
        )

    # 2. Foreign Keys
    print("\n--- 2. FOREIGN KEY REFERENTIAL INTEGRITY ---")
    print(f"{'Relation':<26} {'Child -> Parent':<28} {'Orphans':<9} {'Null FK':<9} {'Status':<6}")
    print("-" * 80)
    for fk in report["foreign_key_checks"]:
        ref_str = f"{fk['child_column']} -> {fk['parent_table'].split('.')[-1]}"
        print(
            f"{fk['relation_name']:<26} {ref_str:<28} {fk['orphan_rows']:<9} {fk['null_references']:<9} [{fk['status']}]"
        )

    # 3. Business Sanity
    print("\n--- 3. BUSINESS SANITY CHECKS ---")
    print(f"{'Rule ID':<38} {'Table':<22} {'Violations':<12} {'Status':<6}")
    print("-" * 80)
    for rule in report["business_sanity_checks"]:
        print(f"{rule['rule_id']:<38} {rule['table']:<22} {rule['violation_count']:<12} [{rule['status']}]")

    print("\n" + "=" * 80)
    print(f"FINAL RESULT: {summary['overall_status']} (Passed {summary['passed_checks']}/{summary['total_checks']})")
    print("=" * 80 + "\n")


async def run_integrity_validation(
    output_path: Path | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Execute integrity validation and write report to JSON."""
    validator = IntegrityValidator()
    report = await validator.validate_all()

    if not quiet:
        print_integrity_summary(report)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"Integrity report saved to: {output_path.resolve()}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate data integrity & business sanity in PostgreSQL.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=REPO_ROOT / "reports" / "integrity_report.json",
        help="Path to output JSON file (default: reports/integrity_report.json)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress terminal output",
    )
    args = parser.parse_args()

    report = asyncio.run(
        run_integrity_validation(
            output_path=args.output,
            quiet=args.quiet,
        )
    )

    # Exit with code 1 if any integrity check failed
    if report["summary"]["overall_status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
