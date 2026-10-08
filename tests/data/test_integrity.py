"""Tests for data integrity and business sanity validation pipeline."""

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

from scripts.validate_integrity import IntegrityValidator


@pytest.mark.asyncio
async def test_primary_key_validation(validator: IntegrityValidator) -> None:
    """Verify primary keys have 0 nulls, 0 duplicates, and 0 empty strings."""
    async with validator.engine.connect() as conn:
        pk_results = await validator.validate_primary_keys(conn)

    assert len(pk_results) == 5

    table_pk_map = {
        "business.customers": "customer_id",
        "business.orders": "order_id",
        "business.products": "product_id",
        "business.order_items": ["order_id", "order_item_id"],
        "business.payments": "payment_id",
    }

    for res in pk_results:
        tbl = res["table"]
        assert tbl in table_pk_map
        assert res["primary_key"] == table_pk_map[tbl]
        assert res["null_count"] == 0, f"PK in {tbl} contains NULLs"
        assert res["duplicate_count"] == 0, f"PK in {tbl} contains duplicates"
        assert res["empty_string_count"] == 0, f"PK in {tbl} contains empty strings"
        assert res["status"] == "PASS"


@pytest.mark.asyncio
async def test_foreign_key_referential_integrity(validator: IntegrityValidator) -> None:
    """Verify all foreign key relationships have zero orphan records."""
    async with validator.engine.connect() as conn:
        fk_results = await validator.validate_foreign_keys(conn)

    assert len(fk_results) == 4

    expected_relations = {
        "orders_to_customers",
        "order_items_to_orders",
        "order_items_to_products",
        "payments_to_orders",
    }
    assert {r["relation_name"] for r in fk_results} == expected_relations

    for res in fk_results:
        assert res["orphan_rows"] == 0, f"FK {res['relation_name']} has orphans"
        assert res["orphan_percentage"] == 0.0
        assert res["status"] == "PASS"

    # Specifically check order_items_to_products nullable handling
    oi_prod = next(r for r in fk_results if r["relation_name"] == "order_items_to_products")
    assert oi_prod["is_nullable"] is True
    assert oi_prod["total_child_rows"] == oi_prod["valid_references"] + oi_prod["null_references"]


@pytest.mark.asyncio
async def test_business_sanity_rules(validator: IntegrityValidator) -> None:
    """Verify all business sanity rules pass with zero violations."""
    async with validator.engine.connect() as conn:
        sanity_results = await validator.validate_business_sanity(conn)

    assert len(sanity_results) >= 16

    for rule in sanity_results:
        assert rule["violation_count"] == 0, f"Rule {rule['rule_id']} failed with {rule['violation_count']} violations"
        assert rule["status"] == "PASS"


@pytest.mark.asyncio
async def test_overall_integrity_validation_report(validator: IntegrityValidator) -> None:
    """Verify that validate_all() aggregates results and produces a PASS report."""
    report = await validator.validate_all()

    summary = report["summary"]
    assert summary["overall_status"] == "PASS"
    assert summary["failed_checks"] == 0
    assert summary["passed_checks"] == summary["total_checks"]
    assert summary["total_checks"] >= 25

    # Check that report is JSON serializable
    json_str = json.dumps(report)
    loaded = json.loads(json_str)
    assert loaded["summary"]["overall_status"] == "PASS"
