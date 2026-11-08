"""Pure, testable helpers backing the Streamlit demo UI.

These functions map the public ``/analyze`` response (a plain dict matching the
``AnalyzeResponse`` schema) to UI-ready artifacts: result DataFrames, chart
suitability, and derived pipeline-stage observations. The streaming API does
not expose intermediate node events over ``POST /analyze``, so the stage list
is *derived* from response evidence only — stages that provably did not run are
reported as ``skipped`` instead of being fabricated as executed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

# Cells holding PostgreSQL numeric or timestamp values can arrive as strings
# (JSON does not carry database types). FORBIDDEN_CHARS filters out decimal
# separators, signs, digits, and whitespace so "1, 2-3" style text is preserved.
_FORBIDDEN_RE = re.compile(r"[^0-9eE+\-.,%\s]")

MAX_PIE_SLICES = 8

STAGE_RETRIEVAL = "Semantic Retrieval"
STAGE_GENERATION = "SQL Generation"
STAGE_VALIDATION = "SQL Validation"
STAGE_EXECUTION = "SQL Execution"
STAGE_REPAIR = "SQL Repair"
STAGE_FINALIZATION = "Finalization"


@dataclass(frozen=True)
class StageObservation:
    """One node of the analytics workflow as evidenced by the final response."""

    name: str
    status: str  # "done" | "failed" | "skipped"
    detail: str = ""


# ---------------------------------------------------------------------------
# Result table
# ---------------------------------------------------------------------------


def build_dataframe(execution: Mapping[str, Any] | None) -> pd.DataFrame:
    """Convert ``ExecutionResult`` rows/columns into a display DataFrame.

    Values are cast per-column based on their majority type so PostgreSQL
    numbers/timestamps render as proper types rather than strings.
    """
    if execution is None:
        return pd.DataFrame()

    columns = list(execution.get("columns") or [])
    rows = list(execution.get("rows") or [])
    if not columns:
        return pd.DataFrame()

    frame = pd.DataFrame(rows, columns=columns)
    for column in frame.columns:
        frame[column] = _cast_column(column, frame[column])
    return frame


def _cast_column(name: str, series: pd.Series) -> pd.Series:
    import decimal  # local import keeps module import cheap when unused

    if series.empty:
        return series

    parsed_timestamps = pd.to_datetime(series, errors="coerce", format="ISO8601")
    if parsed_timestamps.notna().all():
        return parsed_timestamps

    try:
        numeric = pd.to_numeric(series, errors="raise")
        return numeric
    except (ValueError, TypeError):
        pass

    try:
        return series.map(lambda value: decimal.Decimal(str(value)) if value is not None else value)
    except (decimal.InvalidOperation, ValueError, TypeError):
        return series


# ---------------------------------------------------------------------------
# Chart suitability
# ---------------------------------------------------------------------------


def dimension_columns(df: pd.DataFrame) -> list[str]:
    """Columns that can serve as a chart axis (text, booleans, dates/categories)."""
    return [
        name
        for name, dtype in df.dtypes.items()
        if dtype.kind in ("O", "b", "M")  # object, boolean, datetime64
    ]


def metric_columns(df: pd.DataFrame) -> list[str]:
    """Columns that can serve as a chart value (numbers as well as periodic/duration strings)."""
    numeric_names = [name for name, dtype in df.dtypes.items() if dtype.kind in "iuf"]
    untyped_names = [name for name in df.columns if _string_is_numeric(df[name])]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in [*numeric_names, *untyped_names]:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def temporal_names(df: pd.DataFrame) -> list[str]:
    """Names of datetime columns."""
    return [
        name
        for name, dtype in df.dtypes.items()
        if dtype.kind in "M"  # datetime64
    ]


def _string_is_numeric(series: pd.Series) -> bool:
    """True when an object column holds mostly numeric-ish strings ("12.5", "70%")."""
    if series.empty or series.dtype.kind not in ("O",):
        return False
    values = [str(v) for v in series.head(50) if v is not None]
    if not values:
        return False
    numeric = sum(1 for v in values if _is_numeric_string(v))
    return numeric / len(values) >= 0.8


def _is_numeric_string(text: str) -> bool:
    stripped = text.strip().replace(" ", "")
    if not stripped or _FORBIDDEN_RE.search(stripped):
        return False
    return bool(re.fullmatch(r"[+-]?(\d+(?:[.,]\d+)*)%?", stripped))


def coerce_metric(series: pd.Series) -> pd.Series:
    """Convert a plot value column to numeric, understanding "70%" and "1,234".

    Non-convertible values become NaN (dropped later by the chart framing).
    """
    if series.dtype.kind in "iuf":
        return series
    cleaned = (
        series.astype("string")
        .str.replace("%", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def is_chartable(df: pd.DataFrame) -> bool:
    """True when the results could legitimately be visualized (needs ≥1 dimension + metric)."""
    return bool(
        not df.empty
        and len(df.columns) >= 2
        and dimension_columns(df)
        and metric_columns(df)
    )


def prepare_chart_frame(df: pd.DataFrame, dimension: str, metric: str) -> pd.DataFrame:
    """Aggregate ``metric`` per unique ``dimension`` for charting.

    Returns a DataFrame with exactly ``[dimension, metric]`` where the metric is
    numeric (periodic strings such as ``"70%"`` are converted to floats).
    """
    values = coerce_metric(df[metric])
    frame = pd.DataFrame({"dimension": df[dimension], "metric": values})
    frame = frame.dropna()
    frame["dimension"] = frame["dimension"].astype(object)
    frame = frame.groupby("dimension", dropna=False, as_index=False)["metric"].sum()
    return frame.sort_values("metric", ascending=False)


# ---------------------------------------------------------------------------
# Derived pipeline stages
# ---------------------------------------------------------------------------


def pipeline_stages(response: Mapping[str, Any]) -> list[StageObservation]:
    """Derive the workflow stage observations from the final ``/analyze`` response.

    Because ``POST /analyze`` does not stream node events, the observations rely
    solely on response evidence:

    - ``failed_stage`` names where the pipeline stopped.
    - ``raw_sql``/``final_sql``/``model_used`` prove a SQL candidate was produced.
    - ``execution`` being present proves guardrails passed and SQL was executed.
    - ``repair_attempts`` proves the repair loop triggered.
    The finalize node always produces one response payload per run.
    """
    failed_stage = response.get("failed_stage")
    execution = response.get("execution")
    raw_sql = response.get("raw_sql") or ""
    final_sql = response.get("final_sql") or ""
    repair_attempts = int(response.get("repair_attempts") or 0)
    model_used = response.get("model_used")
    retrieval_status = response.get("retrieval_status")
    confidence = _as_float(response.get("confidence_score"))
    has_candidate = bool(raw_sql or final_sql) or repair_attempts > 0

    stages: list[StageObservation] = []

    if failed_stage == "retrieval":
        stages.append(StageObservation(STAGE_RETRIEVAL, "failed", f"Status: {retrieval_status}"))
    else:
        stages.append(
            StageObservation(
                STAGE_RETRIEVAL,
                "done",
                f"Status: {retrieval_status}, confidence {confidence:.2f}",
            )
        )

    if failed_stage == "retrieval":
        stages.append(StageObservation(STAGE_GENERATION, "skipped", "Not reached (retrieval failed)"))
        stages.append(StageObservation(STAGE_VALIDATION, "skipped", "Not reached (retrieval failed)"))
        stages.append(StageObservation(STAGE_EXECUTION, "skipped", "Not reached (retrieval failed)"))
    elif failed_stage == "generation" and not has_candidate:
        # LLM provider error: the graph routed to finalize instead of validating.
        stages.append(StageObservation(STAGE_GENERATION, "failed", "No SQL candidate produced"))
        stages.append(StageObservation(STAGE_VALIDATION, "skipped", "Not reached (generation failed)"))
        stages.append(StageObservation(STAGE_EXECUTION, "skipped", "Not reached (generation failed)"))
    else:
        generation_detail = f"Model: {model_used}" if model_used else ""
        stages.append(StageObservation(STAGE_GENERATION, "done", generation_detail))
        if execution is not None:
            stages.append(StageObservation(STAGE_VALIDATION, "done", "Guardrails passed"))
        else:
            # A candidate was produced but no execution followed: guardrail budget exhausted.
            stages.append(
                StageObservation(
                    STAGE_VALIDATION,
                    "failed",
                    "Guardrails kept rejecting the candidate within the allowed budget",
                )
            )
            stages.append(StageObservation(STAGE_EXECUTION, "skipped", "Guardrails blocked execution"))

    if execution is not None:
        exec_status = str(execution.get("status") or "")
        if exec_status == "SUCCESS":
            stages.append(StageObservation(STAGE_EXECUTION, "done", "SQL executed successfully"))
        else:
            stages.append(StageObservation(STAGE_EXECUTION, "failed", f"Status: {exec_status}"))

    if repair_attempts > 0:
        stages.append(StageObservation(STAGE_REPAIR, "done", f"{repair_attempts} attempt(s)"))

    stages.append(StageObservation(STAGE_FINALIZATION, "done", "Response assembled by the finalize node"))
    return stages


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Misc formatting helpers
# ---------------------------------------------------------------------------


def format_duration(milliseconds: float) -> str:
    """Human-readable wall-clock duration from milliseconds."""
    if milliseconds >= 1:
        text = f"{milliseconds:.0f} ms" if milliseconds < 1000 else f"{milliseconds / 1000:.1f} s"
    else:
        text = f"{milliseconds:.3f} ms"
    return text


def rows_caption(execution: Mapping[str, Any]) -> str:
    """Row-count caption for the results section."""
    row_count = int(execution.get("row_count") or 0)
    total = int(execution.get("total_rows_fetched") or row_count)
    if total and row_count < total:
        return f"{row_count} of {total} rows shown (row limit applied)"
    return f"{row_count} row(s) returned"
