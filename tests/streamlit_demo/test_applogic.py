"""Unit tests for the pure presentation helpers of the Streamlit demo UI."""

from __future__ import annotations

import pandas as pd
import pytest

from frontend.streamlit import applogic

ANALYZE_PAYLOAD = {
    "question": "Which state has the most orders?",
    "conversation_id": None,
    "success": True,
    "failed_stage": None,
    "failure_reason": None,
    "retrieval_status": "SUCCESS",
    "confidence_score": 0.87,
    "final_sql": "SELECT customer_state, COUNT(*) AS orders FROM business.orders LIMIT 10",
    "raw_sql": "SELECT customer_state, count(*) AS orders FROM orders LIMIT 10",
    "explanation": None,
    "repair_attempts": 0,
    "model_used": "google/gemini-2.5-flash",
    "warnings": [],
    "execution": {
        "status": "SUCCESS",
        "sql": "SELECT customer_state, COUNT(*) AS orders FROM business.orders LIMIT 10",
        "columns": ["customer_state", "orders"],
        "rows": [["SP", 41746], ["MG", 11600]],
        "row_count": 2,
        "total_rows_fetched": 2,
        "truncated": False,
        "execution_ms": 18.4,
        "limit_applied": 10,
        "error_code": None,
        "error_message": None,
    },
}


def payload_mutations(**overrides: object) -> dict[str, object]:
    """Return a copy of the payload with fields replaced (None values allowed)."""
    merged = dict(ANALYZE_PAYLOAD)
    merged.update(overrides)
    return merged


# ---------------------------------------------------------------------------
# build_dataframe
# ---------------------------------------------------------------------------


def test_build_dataframe_casts_numeric_columns() -> None:
    execution = {**ANALYZE_PAYLOAD["execution"], "rows": [["SP", "41746"], ["MG", "11600"]]}
    df = applogic.build_dataframe(execution)

    assert list(df.columns) == ["customer_state", "orders"]
    assert df["orders"].dtype.kind in "if"
    assert int(df.loc[0, "orders"]) == 41746


def test_build_dataframe_empty_rows_and_none_execution() -> None:
    assert applogic.build_dataframe(None).empty
    assert applogic.build_dataframe({"columns": ["a"], "rows": []}).empty


# ---------------------------------------------------------------------------
# Chart suitability
# ---------------------------------------------------------------------------


def test_chartable_results_offer_dimension_and_metric_columns() -> None:
    df = applogic.build_dataframe(ANALYZE_PAYLOAD["execution"])

    assert applogic.is_chartable(df)
    assert "customer_state" in applogic.dimension_columns(df)
    assert "orders" in applogic.metric_columns(df)


def test_single_column_results_not_chartable() -> None:
    df = pd.DataFrame({"orders": [1, 2, 3]})
    assert not applogic.is_chartable(df)


def test_pure_numeric_columns_not_chartable() -> None:
    df = pd.DataFrame({"revenue": [1.0, 2.0], "cost": [0.5, 0.9]})
    assert not applogic.is_chartable(df)


def test_percent_strings_count_as_metrics() -> None:
    df = pd.DataFrame({"channel": ["card", "voucher"], "pct": ["70.5%", "29.5%"]})

    assert "pct" in applogic.metric_columns(df)
    chart_frame = applogic.prepare_chart_frame(df, "channel", "pct")
    assert chart_frame.loc[0, "metric"] == pytest.approx(70.5)


def test_prepare_chart_frame_aggregates_duplicate_dimensions() -> None:
    df = pd.DataFrame({"state": ["SP", "SP", "MG"], "orders": [10, 5, 7]})

    frame = applogic.prepare_chart_frame(df, "state", "orders")
    assert len(frame) == 2
    sp = frame[frame["dimension"] == "SP"]["metric"].iloc[0]
    assert sp == pytest.approx(15)


# ---------------------------------------------------------------------------
# pipeline_stages
# ---------------------------------------------------------------------------


def _statuses(response: dict[str, object]) -> dict[str, str]:
    return {stage.name: stage.status for stage in applogic.pipeline_stages(response)}


def test_all_stages_reported_done_on_success() -> None:
    statuses = _statuses(ANALYZE_PAYLOAD)

    assert statuses == {
        "Semantic Retrieval": "done",
        "SQL Generation": "done",
        "SQL Validation": "done",
        "SQL Execution": "done",
        "Finalization": "done",
    }


def test_retrieval_failure_marks_downstream_skipped() -> None:
    payload = payload_mutations(
        success=False,
        failed_stage="retrieval",
        failure_reason="no match",
        retrieval_status="INSUFFICIENT_METADATA",
        final_sql=None,
        raw_sql=None,
        execution=None,
    )

    statuses = _statuses(payload)
    assert statuses["Semantic Retrieval"] == "failed"
    assert statuses["SQL Generation"] == "skipped"
    assert statuses["SQL Validation"] == "skipped"
    assert statuses["SQL Execution"] == "skipped"
    assert statuses["Finalization"] == "done"


def test_llm_provider_failure_skips_validation() -> None:
    payload = payload_mutations(
        success=False,
        failed_stage="generation",
        failure_reason="LLM provider error: timeout",
        raw_sql=None,
        final_sql=None,
        execution=None,
    )

    statuses = _statuses(payload)
    assert statuses["SQL Generation"] == "failed"
    assert statuses["SQL Validation"] == "skipped"
    assert statuses["SQL Execution"] == "skipped"


def test_guardrail_budget_exhaustion_marks_validation_failed() -> None:
    payload = payload_mutations(
        success=False,
        failed_stage="generation",
        failure_reason="Generated SQL is not a SELECT statement",
        raw_sql="DELETE FROM orders",
        final_sql=None,
        execution=None,
    )

    statuses = _statuses(payload)
    assert statuses["SQL Generation"] == "done"
    assert statuses["SQL Validation"] == "failed"
    assert statuses["SQL Execution"] == "skipped"


def test_execution_failure_marks_execution_failed() -> None:
    payload = payload_mutations(
        success=False,
        failed_stage="execution",
        failure_reason="column undefined",
        execution={**ANALYZE_PAYLOAD["execution"], "status": "DATABASE_ERROR",
                   "error_code": "42703", "error_message": 'column "cusomer" does not exist'},
    )

    statuses = _statuses(payload)
    assert statuses["SQL Validation"] == "done"
    assert statuses["SQL Execution"] == "failed"


def test_repair_attempt_only_listed_when_triggered() -> None:
    assert "SQL Repair" not in _statuses(ANALYZE_PAYLOAD)

    payload = payload_mutations(repair_attempts=2)
    statuses = _statuses(payload)
    assert statuses["SQL Repair"] == "done"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def test_format_duration() -> None:
    assert applogic.format_duration(18.4) == "18 ms"
    assert applogic.format_duration(1500.0) == "1.5 s"
    assert applogic.format_duration(0.2) == "0.200 ms"


def test_rows_caption_reports_truncation_context() -> None:
    execution = {"row_count": 100, "total_rows_fetched": 5000}

    assert applogic.rows_caption(execution) == "100 of 5000 rows shown (row limit applied)"


def test_rows_caption_simple_case() -> None:
    assert applogic.rows_caption({"row_count": 42, "total_rows_fetched": 42}) == "42 row(s) returned"
