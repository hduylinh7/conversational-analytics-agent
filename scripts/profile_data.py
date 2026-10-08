#!/usr/bin/env python3
"""
Data Profiling Script for Olist E-Commerce Business Tables.

Collects automated profile metrics for:
- business.customers
- business.orders
- business.order_items
- business.products
- business.payments

Collects:
- Table row counts
- Column PostgreSQL data types
- Null counts and null percentages
- Distinct counts
- Numeric columns: min, max, avg
- Timestamp columns: min_timestamp, max_timestamp
- Categorical columns: distinct count and top frequency values
"""

import argparse
import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
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


# Targeted business tables in dependency / business order
BUSINESS_TABLES = [
    "customers",
    "orders",
    "order_items",
    "products",
    "payments",
]

# Designated columns for specific analytical metric types
NUMERIC_COLUMNS = {
    "order_items": ["order_item_id", "price", "freight_value"],
    "products": [
        "product_name_lenght",
        "product_description_lenght",
        "product_photos_qty",
        "product_weight_g",
        "product_length_cm",
        "product_height_cm",
        "product_width_cm",
    ],
    "payments": ["payment_sequential", "payment_installments", "payment_value"],
}

TIMESTAMP_COLUMNS = {
    "orders": [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ],
    "order_items": ["shipping_limit_date"],
}

CATEGORICAL_COLUMNS = {
    "customers": ["customer_state", "customer_city"],
    "orders": ["order_status"],
    "payments": ["payment_type"],
    "products": ["product_category_name"],
}


class DataProfileJSONEncoder(json.JSONEncoder):
    """Custom JSON encoder handling Decimal, datetime, and date objects."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        return super().default(obj)


class DataProfiler:
    """Profiles PostgreSQL tables in the business schema."""

    def __init__(self, db_engine: Any = engine, schema: str = "business") -> None:
        self.engine = db_engine
        self.schema = schema

    async def get_table_columns_metadata(self, conn: Any, table_name: str) -> list[dict[str, Any]]:
        """Fetch column names and Postgres data types from information_schema."""
        query = text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = :table_name
            ORDER BY ordinal_position
        """)
        result = await conn.execute(query, {"schema": self.schema, "table_name": table_name})
        return [
            {
                "column_name": row[0],
                "data_type": row[1],
                "is_nullable": row[2] == "YES",
            }
            for row in result.fetchall()
        ]

    async def get_categorical_top_values(
        self, conn: Any, table_name: str, column_name: str, top_n: int = 5, total_rows: int = 0
    ) -> list[dict[str, Any]]:
        """Fetch top N most frequent values and their counts."""
        query = text(f"""
            SELECT "{column_name}"::text AS val, COUNT(*) AS count
            FROM "{self.schema}"."{table_name}"
            WHERE "{column_name}" IS NOT NULL
            GROUP BY "{column_name}"
            ORDER BY count DESC
            LIMIT :top_n
        """)
        result = await conn.execute(query, {"top_n": top_n})
        top_values = []
        for row in result.fetchall():
            cnt = int(row[1])
            pct = round((cnt / total_rows * 100), 2) if total_rows > 0 else 0.0
            top_values.append({
                "value": row[0],
                "count": cnt,
                "percentage": pct,
            })
        return top_values

    async def profile_table(self, conn: Any, table_name: str) -> dict[str, Any]:
        """Collect comprehensive profile metrics for a single table."""
        # 1. Total row count
        count_query = text(f'SELECT COUNT(*) FROM "{self.schema}"."{table_name}"')
        row_count = (await conn.execute(count_query)).scalar() or 0

        columns_meta = await self.get_table_columns_metadata(conn, table_name)
        columns_profile: dict[str, Any] = {}

        if not columns_meta:
            return {
                "table": f"{self.schema}.{table_name}",
                "row_count": row_count,
                "columns": {},
            }

        # 2. Build batched query for null counts, distinct counts, and numeric/date aggregations
        select_parts: list[str] = []
        col_type_map: dict[str, str] = {c["column_name"]: c["data_type"] for c in columns_meta}

        target_numerics = NUMERIC_COLUMNS.get(table_name, [])
        target_timestamps = TIMESTAMP_COLUMNS.get(table_name, [])

        for col in columns_meta:
            cname = col["column_name"]
            # Null count and distinct count for every column
            select_parts.append(
                f'COUNT(*) - COUNT("{cname}") AS "{cname}__null_count"'
            )
            select_parts.append(
                f'COUNT(DISTINCT "{cname}") AS "{cname}__distinct_count"'
            )

            # Numeric metrics if designated
            if cname in target_numerics:
                select_parts.append(f'MIN("{cname}") AS "{cname}__min"')
                select_parts.append(f'MAX("{cname}") AS "{cname}__max"')
                select_parts.append(f'AVG("{cname}") AS "{cname}__avg"')

            # Timestamp metrics if designated
            if cname in target_timestamps:
                select_parts.append(f'MIN("{cname}") AS "{cname}__min"')
                select_parts.append(f'MAX("{cname}") AS "{cname}__max"')

        batch_sql = f'SELECT {", ".join(select_parts)} FROM "{self.schema}"."{table_name}"'
        batch_result = (await conn.execute(text(batch_sql))).mappings().one()

        # 3. Assemble column profiles
        target_categoricals = CATEGORICAL_COLUMNS.get(table_name, [])

        for col in columns_meta:
            cname = col["column_name"]
            null_count = int(batch_result[f"{cname}__null_count"])
            distinct_count = int(batch_result[f"{cname}__distinct_count"])
            null_pct = round((null_count / row_count * 100), 2) if row_count > 0 else 0.0

            col_info: dict[str, Any] = {
                "data_type": col["data_type"],
                "null_count": null_count,
                "null_percentage": null_pct,
                "distinct_count": distinct_count,
            }

            # Add numeric aggregations if present
            if cname in target_numerics:
                min_val = batch_result[f"{cname}__min"]
                max_val = batch_result[f"{cname}__max"]
                avg_val = batch_result[f"{cname}__avg"]

                col_info["min"] = float(min_val) if isinstance(min_val, Decimal) else min_val
                col_info["max"] = float(max_val) if isinstance(max_val, Decimal) else max_val
                col_info["avg"] = round(float(avg_val), 2) if avg_val is not None else None

            # Add timestamp aggregations if present
            if cname in target_timestamps:
                min_ts = batch_result[f"{cname}__min"]
                max_ts = batch_result[f"{cname}__max"]
                col_info["min_timestamp"] = min_ts.isoformat() if min_ts else None
                col_info["max_timestamp"] = max_ts.isoformat() if max_ts else None

            # Add categorical top values if designated
            if cname in target_categoricals:
                top_limit = 10 if cname == "product_category_name" else 5
                top_vals = await self.get_categorical_top_values(
                    conn, table_name, cname, top_n=top_limit, total_rows=row_count
                )
                col_info["top_values"] = top_vals

            columns_profile[cname] = col_info

        return {
            "table": f"{self.schema}.{table_name}",
            "row_count": row_count,
            "columns": columns_profile,
        }

    async def profile_all(self, tables: list[str] | None = None) -> dict[str, Any]:
        """Profile all designated business tables."""
        target_tables = tables or BUSINESS_TABLES
        results: dict[str, Any] = {}
        total_rows = 0

        async with self.engine.connect() as conn:
            for tname in target_tables:
                profile = await self.profile_table(conn, tname)
                results[f"{self.schema}.{tname}"] = profile
                total_rows += profile["row_count"]

        return {
            "generated_at": datetime.now().isoformat(),
            "schema": self.schema,
            "summary": {
                "total_tables": len(target_tables),
                "total_rows": total_rows,
            },
            "tables": results,
        }


# Ensure stdout/stderr handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")


def print_profile_summary(report: dict[str, Any]) -> None:
    """Print a clean human-readable summary to console."""
    print("\n" + "=" * 78)
    print("                      DATA PROFILING SUMMARY")
    print("=" * 78)
    summary = report["summary"]
    print(f"Generated At: {report['generated_at']}")
    print(f"Schema:       {report['schema']}")
    print(f"Total Tables: {summary['total_tables']} | Total Rows: {summary['total_rows']:,}")
    print("-" * 78)

    for table_full, tbl in report["tables"].items():
        print(f"\n[Table: {table_full}] - {tbl['row_count']:,} rows")
        print(f"{'Column':<32} {'Type':<18} {'Nulls (%)':<14} {'Distinct':<10}")
        print("-" * 78)
        for col_name, cinfo in tbl["columns"].items():
            null_str = f"{cinfo['null_count']:,} ({cinfo['null_percentage']}%)"
            dist_str = f"{cinfo['distinct_count']:,}"
            dtype = cinfo["data_type"][:17]
            print(f"{col_name:<32} {dtype:<18} {null_str:<14} {dist_str:<10}")

            if "min" in cinfo and cinfo["min"] is not None:
                print(f"    * Numeric: min={cinfo['min']}, max={cinfo['max']}, avg={cinfo['avg']}")
            if "min_timestamp" in cinfo and cinfo["min_timestamp"] is not None:
                print(f"    * Timestamp: {cinfo['min_timestamp']} -> {cinfo['max_timestamp']}")
            if "top_values" in cinfo and cinfo["top_values"]:
                top_str = ", ".join(f"{v['value']}: {v['count']:,}" for v in cinfo["top_values"][:3])
                print(f"    * Top: {top_str}")

    print("\n" + "=" * 78 + "\n")


async def run_profiling(
    output_path: Path | None = None,
    tables: list[str] | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    """Execute profiling and write report to JSON file."""
    profiler = DataProfiler()
    report = await profiler.profile_all(tables)

    if not quiet:
        print_profile_summary(report)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, cls=DataProfileJSONEncoder)
        print(f"Report saved to: {output_path.resolve()}")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile Olist business tables in PostgreSQL.")
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=REPO_ROOT / "reports" / "data_profile.json",
        help="Path to output JSON file (default: reports/data_profile.json)",
    )
    parser.add_argument(
        "--table",
        "-t",
        nargs="+",
        help="Specific tables to profile (default: all 5 business tables)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress terminal output",
    )
    args = parser.parse_args()

    asyncio.run(
        run_profiling(
            output_path=args.output,
            tables=args.table,
            quiet=args.quiet,
        )
    )


if __name__ == "__main__":
    main()
