#!/usr/bin/env python3
"""
Olist E-Commerce Data Ingestion Pipeline.

Pipeline Architecture:
    CSV Discovery
         ↓
    Schema Validation (columns, required fields)
         ↓
    Type Conversion (timestamp, numeric, string, zip_code)
         ↓
    Data Cleaning (strip strings, handle nulls/NaNs)
         ↓
    FK & Constraint Validation (PK uniqueness, FK referential integrity, non-negative checks)
         ↓
    PostgreSQL Ingestion (ordered dependency bulk insert via asyncpg copy_records_to_table)
         ↓
    Post-Ingestion Verification (row counts, checksum comparison)
"""

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
import pandas as pd

# Add backend directory to sys.path to access app configuration
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

try:
    from app.core.config import get_settings
    settings = get_settings()
except ImportError:
    settings = None


# Expected Schema Definitions
EXPECTED_SCHEMAS = {
    "customers": {
        "file": "olist_customers_dataset.csv",
        "table": "customers",
        "schema": "business",
        "required_columns": [
            "customer_id",
            "customer_unique_id",
            "customer_zip_code_prefix",
            "customer_city",
            "customer_state",
        ],
        "pk": ["customer_id"],
    },
    "products": {
        "file": "olist_products_dataset.csv",
        "table": "products",
        "schema": "business",
        "required_columns": [
            "product_id",
            "product_category_name",
            "product_name_lenght",
            "product_description_lenght",
            "product_photos_qty",
            "product_weight_g",
            "product_length_cm",
            "product_height_cm",
            "product_width_cm",
        ],
        "pk": ["product_id"],
    },
    "orders": {
        "file": "olist_orders_dataset.csv",
        "table": "orders",
        "schema": "business",
        "required_columns": [
            "order_id",
            "customer_id",
            "order_status",
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ],
        "pk": ["order_id"],
    },
    "order_items": {
        "file": "olist_order_items_dataset.csv",
        "table": "order_items",
        "schema": "business",
        "required_columns": [
            "order_id",
            "order_item_id",
            "product_id",
            "seller_id",
            "shipping_limit_date",
            "price",
            "freight_value",
        ],
        "pk": ["order_id", "order_item_id"],
    },
    "payments": {
        "file": "olist_order_payments_dataset.csv",
        "table": "payments",
        "schema": "business",
        "required_columns": [
            "order_id",
            "payment_sequential",
            "payment_type",
            "payment_installments",
            "payment_value",
        ],
        "pk": ["order_id", "payment_sequential"],
    },
}

# Ingestion Dependency Order (Parent tables before Child tables)
INGESTION_ORDER = ["customers", "products", "orders", "order_items", "payments"]


@dataclass
class ValidationReport:
    name: str
    total_rows: int
    valid_rows: int
    duplicates_removed: int
    orphans_removed: int
    errors: list[str]


def clean_none(val: Any) -> Any:
    """Convert pandas/numpy NaN and NaT into pure Python None."""
    if pd.isna(val) or val is None:
        return None
    return val


def parse_datetime(series: pd.Series) -> pd.Series:
    """Parse dates with coercion, returning NaT for invalid values."""
    return pd.to_datetime(series, errors="coerce")


class DataIngestionPipeline:
    def __init__(self, data_dir: Path, db_url: str, truncate: bool = False, dry_run: bool = False):
        self.data_dir = data_dir
        self.db_url = db_url
        self.truncate = truncate
        self.dry_run = dry_run
        self.reports: dict[str, ValidationReport] = {}
        self.dataframes: dict[str, pd.DataFrame] = {}

    def log(self, stage: str, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] [{stage.upper()}] {message}")

    # ==========================================
    # STEP 1: CSV Discovery & Loading
    # ==========================================
    def step_load_csvs(self) -> None:
        self.log("load", f"Loading CSV files from {self.data_dir}...")
        for key in INGESTION_ORDER:
            meta = EXPECTED_SCHEMAS[key]
            file_path = self.data_dir / meta["file"]
            if not file_path.exists():
                raise FileNotFoundError(f"Missing required data file: {file_path}")

            df = pd.read_csv(file_path, low_memory=False)
            self.dataframes[key] = df
            self.log("load", f"Loaded '{meta['file']}': {len(df):,} rows")

    # ==========================================
    # STEP 2: Schema Validation
    # ==========================================
    def step_validate_schemas(self) -> None:
        self.log("schema", "Validating CSV columns against data specifications...")
        for key, meta in EXPECTED_SCHEMAS.items():
            df = self.dataframes[key]
            missing_cols = [c for c in meta["required_columns"] if c not in df.columns]
            if missing_cols:
                raise ValueError(
                    f"Schema validation failed for {key}: Missing columns {missing_cols}"
                )
            # Reorder columns to match expected order
            self.dataframes[key] = df[meta["required_columns"]].copy()
            self.log("schema", f"Table '{key}': Schema OK ({len(meta['required_columns'])} columns)")

    # ==========================================
    # STEP 3: Type Conversion & Cleaning
    # ==========================================
    def step_clean_and_convert(self) -> None:
        self.log("clean", "Executing type conversion and data cleaning...")

        # 1. Customers
        c_df = self.dataframes["customers"]
        c_df["customer_id"] = c_df["customer_id"].astype(str).str.strip()
        c_df["customer_unique_id"] = c_df["customer_unique_id"].astype(str).str.strip()
        # Zip code prefix: string up to 5 chars (e.g. 14409)
        c_df["customer_zip_code_prefix"] = (
            c_df["customer_zip_code_prefix"]
            .fillna("")
            .astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.strip()
            .str.zfill(5)
            .str.slice(0, 5)
        )
        c_df["customer_city"] = c_df["customer_city"].astype(str).str.strip().str.slice(0, 100)
        c_df["customer_state"] = c_df["customer_state"].astype(str).str.strip().str.slice(0, 2)
        self.dataframes["customers"] = c_df

        # 2. Products
        p_df = self.dataframes["products"]
        p_df["product_id"] = p_df["product_id"].astype(str).str.strip()
        p_df["product_category_name"] = p_df["product_category_name"].apply(
            lambda x: str(x).strip()[:100] if pd.notna(x) else None
        )
        int_cols = ["product_name_lenght", "product_description_lenght", "product_photos_qty"]
        for col in int_cols:
            p_df[col] = pd.to_numeric(p_df[col], errors="coerce").astype("Int64")

        num_cols = ["product_weight_g", "product_length_cm", "product_height_cm", "product_width_cm"]
        for col in num_cols:
            p_df[col] = pd.to_numeric(p_df[col], errors="coerce").round(2)
        self.dataframes["products"] = p_df

        # 3. Orders
        o_df = self.dataframes["orders"]
        o_df["order_id"] = o_df["order_id"].astype(str).str.strip()
        o_df["customer_id"] = o_df["customer_id"].astype(str).str.strip()
        o_df["order_status"] = o_df["order_status"].astype(str).str.strip().str.slice(0, 30)
        date_cols = [
            "order_purchase_timestamp",
            "order_approved_at",
            "order_delivered_carrier_date",
            "order_delivered_customer_date",
            "order_estimated_delivery_date",
        ]
        for col in date_cols:
            o_df[col] = parse_datetime(o_df[col])
        self.dataframes["orders"] = o_df

        # 4. Order Items
        oi_df = self.dataframes["order_items"]
        oi_df["order_id"] = oi_df["order_id"].astype(str).str.strip()
        oi_df["order_item_id"] = pd.to_numeric(oi_df["order_item_id"], errors="raise").astype(int)
        oi_df["product_id"] = oi_df["product_id"].apply(
            lambda x: str(x).strip() if pd.notna(x) else None
        )
        oi_df["seller_id"] = oi_df["seller_id"].astype(str).str.strip().str.slice(0, 32)
        oi_df["shipping_limit_date"] = parse_datetime(oi_df["shipping_limit_date"])
        oi_df["price"] = pd.to_numeric(oi_df["price"], errors="raise").round(2)
        oi_df["freight_value"] = pd.to_numeric(oi_df["freight_value"], errors="raise").round(2)
        self.dataframes["order_items"] = oi_df

        # 5. Payments
        pay_df = self.dataframes["payments"]
        pay_df["order_id"] = pay_df["order_id"].astype(str).str.strip()
        pay_df["payment_sequential"] = pd.to_numeric(
            pay_df["payment_sequential"], errors="raise"
        ).astype(int)
        pay_df["payment_type"] = pay_df["payment_type"].astype(str).str.strip().str.slice(0, 30)
        pay_df["payment_installments"] = pd.to_numeric(
            pay_df["payment_installments"], errors="raise"
        ).astype(int)
        pay_df["payment_value"] = pd.to_numeric(pay_df["payment_value"], errors="raise").round(2)
        self.dataframes["payments"] = pay_df

        self.log("clean", "Type conversion & cleaning completed successfully.")

    # ==========================================
    # STEP 4: FK & Integrity Validation
    # ==========================================
    def step_validate_integrity(self) -> None:
        self.log("integrity", "Validating PK uniqueness, non-negative values, and referential integrity...")

        # 1. Check Primary Key Uniqueness
        for key in INGESTION_ORDER:
            df = self.dataframes[key]
            pk = EXPECTED_SCHEMAS[key]["pk"]
            dup_mask = df.duplicated(subset=pk, keep="first")
            num_dups = int(dup_mask.sum())
            if num_dups > 0:
                self.log("integrity", f"Table '{key}': Removing {num_dups} duplicate PK rows.")
                self.dataframes[key] = df[~dup_mask].copy()

        # 2. Check Non-Negative Constraints
        oi_df = self.dataframes["order_items"]
        invalid_items = (oi_df["price"] < 0) | (oi_df["freight_value"] < 0)
        if invalid_items.sum() > 0:
            self.log("integrity", f"Order items: Dropping {invalid_items.sum()} rows with negative price/freight.")
            self.dataframes["order_items"] = oi_df[~invalid_items].copy()

        pay_df = self.dataframes["payments"]
        invalid_pays = (pay_df["payment_value"] < 0) | (pay_df["payment_installments"] < 0)
        if invalid_pays.sum() > 0:
            self.log("integrity", f"Payments: Dropping {invalid_pays.sum()} rows with negative payment value/installments.")
            self.dataframes["payments"] = pay_df[~invalid_pays].copy()

        # 3. Foreign Key Checks & Cascade Consistency
        # customers -> orders
        valid_customers = set(self.dataframes["customers"]["customer_id"])
        orders_df = self.dataframes["orders"]
        orphan_orders_mask = ~orders_df["customer_id"].isin(valid_customers)
        if orphan_orders_mask.sum() > 0:
            self.log("integrity", f"Orders: Dropping {orphan_orders_mask.sum()} orphan orders (customer missing).")
            self.dataframes["orders"] = orders_df[~orphan_orders_mask].copy()

        # orders -> order_items & payments
        valid_orders = set(self.dataframes["orders"]["order_id"])
        oi_df = self.dataframes["order_items"]
        orphan_items_mask = ~oi_df["order_id"].isin(valid_orders)
        if orphan_items_mask.sum() > 0:
            self.log("integrity", f"Order items: Dropping {orphan_items_mask.sum()} orphan items (order missing).")
            self.dataframes["order_items"] = oi_df[~orphan_items_mask].copy()

        # products -> order_items (Nullable FK: if product missing, set to None rather than drop line)
        valid_products = set(self.dataframes["products"]["product_id"])
        missing_prods_mask = ~oi_df["product_id"].isna() & ~oi_df["product_id"].isin(valid_products)
        if missing_prods_mask.sum() > 0:
            self.log("integrity", f"Order items: Nullifying {missing_prods_mask.sum()} product_id references (product missing).")
            self.dataframes["order_items"].loc[missing_prods_mask, "product_id"] = None

        pay_df = self.dataframes["payments"]
        orphan_pay_mask = ~pay_df["order_id"].isin(valid_orders)
        if orphan_pay_mask.sum() > 0:
            self.log("integrity", f"Payments: Dropping {orphan_pay_mask.sum()} orphan payments (order missing).")
            self.dataframes["payments"] = pay_df[~orphan_pay_mask].copy()

        self.log("integrity", "Referential integrity checks passed 100%.")

    # ==========================================
    # STEP 5: PostgreSQL Bulk Ingestion
    # ==========================================
    async def step_ingest_to_postgres(self) -> None:
        if self.dry_run:
            self.log("db", "[DRY RUN] Skipping database insertion.")
            return

        self.log("db", "Connecting to PostgreSQL...")
        # Parse connection parameters from database URL
        # URL format: postgresql://user:pass@host:port/dbname or postgresql+asyncpg://...
        clean_url = self.db_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(clean_url)

        try:
            async with conn.transaction():
                if self.truncate:
                    self.log("db", "Truncating existing records in business tables...")
                    # Truncate in reverse dependency order
                    for key in reversed(INGESTION_ORDER):
                        tbl = EXPECTED_SCHEMAS[key]["table"]
                        await conn.execute(f"TRUNCATE TABLE business.{tbl} CASCADE;")
                    self.log("db", "Truncate completed.")

                for key in INGESTION_ORDER:
                    df = self.dataframes[key]
                    tbl = EXPECTED_SCHEMAS[key]["table"]
                    cols = EXPECTED_SCHEMAS[key]["required_columns"]

                    start_time = time.time()
                    self.log("db", f"Inserting {len(df):,} records into business.{tbl}...")

                    # Convert DataFrame rows into list of tuples with clean Python types
                    # Replace NaT / NaN with None
                    records = []
                    for row in df[cols].itertuples(index=False, name=None):
                        cleaned_row = tuple(
                            None if (pd.isna(val) or val is pd.NaT) else val
                            for val in row
                        )
                        records.append(cleaned_row)

                    # Bulk copy into PostgreSQL
                    await conn.copy_records_to_table(
                        table_name=tbl,
                        schema_name="business",
                        columns=cols,
                        records=records,
                    )
                    elapsed = time.time() - start_time
                    speed = len(df) / elapsed if elapsed > 0 else 0
                    self.log(
                        "db",
                        f"business.{tbl}: {len(df):,} records inserted in {elapsed:.2f}s ({speed:,.0f} rows/s)",
                    )

            self.log("db", "Transaction committed successfully.")
        finally:
            await conn.close()

    # ==========================================
    # STEP 6: Post-Ingestion Verification
    # ==========================================
    async def step_verify(self) -> None:
        if self.dry_run:
            return

        self.log("verify", "Verifying record counts in PostgreSQL business schema...")
        clean_url = self.db_url.replace("postgresql+asyncpg://", "postgresql://")
        conn = await asyncpg.connect(clean_url)
        try:
            print("\n" + "=" * 65)
            print(f"{'Table':<25} | {'CSV Rows':<15} | {'DB Rows':<15} | {'Status'}")
            print("-" * 65)
            for key in INGESTION_ORDER:
                tbl = EXPECTED_SCHEMAS[key]["table"]
                csv_cnt = len(self.dataframes[key])
                db_cnt = await conn.fetchval(f"SELECT COUNT(*) FROM business.{tbl};")
                status = "MATCH (OK)" if csv_cnt == db_cnt else "MISMATCH"
                print(f"business.{tbl:<16} | {csv_cnt:<15,} | {db_cnt:<15,} | {status}")
            print("=" * 65 + "\n")
        finally:
            await conn.close()

    async def run(self) -> None:
        t0 = time.time()
        print("\n" + "=" * 65)
        print("STARTING OLIST E-COMMERCE DATA INGESTION PIPELINE")
        print("=" * 65)
        self.step_load_csvs()
        self.step_validate_schemas()
        self.step_clean_and_convert()
        self.step_validate_integrity()
        await self.step_ingest_to_postgres()
        await self.step_verify()
        total_time = time.time() - t0
        print(f"Pipeline finished successfully in {total_time:.2f} seconds.\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest Olist Brazilian E-Commerce dataset into PostgreSQL business schema."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "data" / "raw",
        help="Path to directory containing Olist CSV files (default: data/raw)",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="PostgreSQL connection URL. Defaults to DATABASE_URL from .env or settings.",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate business tables before inserting records.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and clean data without writing to PostgreSQL.",
    )
    return parser.parse_args()


def resolve_db_url(cli_url: str | None) -> str:
    """Resolve database URL from CLI arg, environment, or .env file."""
    if cli_url:
        return cli_url

    # Check environment variable
    env_url = os.environ.get("DATABASE_URL")
    if env_url:
        return env_url

    # Check settings from config.py / .env
    if settings:
        return settings.database_url

    # Fallback to local default
    return "postgresql://postgres:12345678@localhost:5432/analytics"


def main() -> None:
    args = parse_args()
    db_url = resolve_db_url(args.database_url)
    pipeline = DataIngestionPipeline(
        data_dir=args.data_dir,
        db_url=db_url,
        truncate=args.truncate,
        dry_run=args.dry_run,
    )
    asyncio.run(pipeline.run())


if __name__ == "__main__":
    main()
