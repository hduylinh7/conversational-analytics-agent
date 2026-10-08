"""Tests for data profiling pipeline."""

import json
from pathlib import Path
import sys

import pytest

# Ensure repository root and backend directory are in sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.profile_data import DataProfileJSONEncoder, DataProfiler


@pytest.mark.asyncio
async def test_profiler_discovers_all_business_tables(profiler: DataProfiler) -> None:
    """Verify that all five business tables are profiled and contain records."""
    report = await profiler.profile_all()

    expected_tables = {
        "business.customers",
        "business.orders",
        "business.order_items",
        "business.products",
        "business.payments",
    }
    assert set(report["tables"].keys()) == expected_tables

    summary = report["summary"]
    assert summary["total_tables"] == 5
    assert summary["total_rows"] > 400_000

    for table_name in expected_tables:
        table_data = report["tables"][table_name]
        assert table_data["row_count"] > 0
        assert len(table_data["columns"]) > 0


@pytest.mark.asyncio
async def test_profiling_columns_metadata_and_structure(profiler: DataProfiler) -> None:
    """Verify standard metadata attributes exist for every profiled column."""
    report = await profiler.profile_all(["customers"])
    customers = report["tables"]["business.customers"]

    assert "customer_id" in customers["columns"]
    col_meta = customers["columns"]["customer_id"]

    assert "data_type" in col_meta
    assert "null_count" in col_meta
    assert "null_percentage" in col_meta
    assert "distinct_count" in col_meta

    assert col_meta["null_count"] == 0
    assert col_meta["null_percentage"] == 0.0
    assert col_meta["distinct_count"] == customers["row_count"]


@pytest.mark.asyncio
async def test_profiling_numeric_aggregations(profiler: DataProfiler) -> None:
    """Verify numeric min, max, and avg metrics are calculated correctly."""
    report = await profiler.profile_all(["order_items", "payments"])

    # order_items.price
    price_stats = report["tables"]["business.order_items"]["columns"]["price"]
    assert price_stats["min"] is not None
    assert price_stats["max"] is not None
    assert price_stats["avg"] is not None
    assert price_stats["min"] > 0
    assert price_stats["max"] >= price_stats["min"]
    assert price_stats["avg"] > 0

    # payments.payment_value
    pay_stats = report["tables"]["business.payments"]["columns"]["payment_value"]
    assert pay_stats["min"] is not None
    assert pay_stats["max"] is not None
    assert pay_stats["avg"] is not None
    assert pay_stats["min"] >= 0
    assert pay_stats["max"] >= pay_stats["min"]


@pytest.mark.asyncio
async def test_profiling_timestamp_aggregations(profiler: DataProfiler) -> None:
    """Verify timestamp min and max are formatted as ISO strings."""
    report = await profiler.profile_all(["orders"])
    order_ts = report["tables"]["business.orders"]["columns"]["order_purchase_timestamp"]

    assert "min_timestamp" in order_ts
    assert "max_timestamp" in order_ts
    assert order_ts["min_timestamp"] is not None
    assert order_ts["max_timestamp"] is not None
    assert order_ts["min_timestamp"] <= order_ts["max_timestamp"]


@pytest.mark.asyncio
async def test_profiling_categorical_top_values(profiler: DataProfiler) -> None:
    """Verify categorical frequency analysis collects top values and percentages."""
    report = await profiler.profile_all(["orders", "customers"])

    # orders.order_status
    status_stats = report["tables"]["business.orders"]["columns"]["order_status"]
    assert "top_values" in status_stats
    top_statuses = [item["value"] for item in status_stats["top_values"]]
    assert "delivered" in top_statuses
    assert status_stats["top_values"][0]["value"] == "delivered"

    # customers.customer_state
    state_stats = report["tables"]["business.customers"]["columns"]["customer_state"]
    assert "top_values" in state_stats
    top_states = [item["value"] for item in state_stats["top_values"]]
    assert "SP" in top_states


@pytest.mark.asyncio
async def test_profile_json_serializability(profiler: DataProfiler) -> None:
    """Verify that full profile dictionary serializes cleanly into valid JSON."""
    report = await profiler.profile_all(["customers", "payments"])
    json_str = json.dumps(report, cls=DataProfileJSONEncoder)
    loaded = json.loads(json_str)

    assert loaded["summary"]["total_tables"] == 2
    assert "business.customers" in loaded["tables"]
