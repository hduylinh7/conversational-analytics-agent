"""Aggregation and reporting for evaluation runs.

``summarize`` collapses a list of per-case results into headline metrics per
layer; ``render_layer_markdown`` renders a summary table plus failing-case
details for terminal output and archived Markdown reports.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from statistics import mean, quantiles
from typing import Any

from app.eval.schemas import (
    E2ECaseResult,
    GenerationCaseResult,
    GuardrailCaseResult,
    RetrievalCaseResult,
)


def _percentiles(values: list[float]) -> dict[str, float | None]:
    """Return p50/p95 for a sample, or None when empty."""
    if not values:
        return {"p50": None, "p95": None}
    if len(values) == 1:
        value = values[0]
        return {"p50": value, "p95": value}
    cut = quantiles(values, n=20)  # each cut = 5% centile
    return {"p50": cut[9], "p95": cut[18]}


def _f(value: float | None, digits: int = 4) -> str:
    """Format an optional float for tables."""
    return "n/a" if value is None else f"{value:.{digits}f}"


def summarize(
    layer: str,
    results: Sequence[
        RetrievalCaseResult | GuardrailCaseResult | GenerationCaseResult | E2ECaseResult
    ],
) -> dict[str, Any]:
    """Compute headline metrics for one completed evaluation layer run."""
    total = len(results)
    passed = sum(1 for item in results if item.passed)
    accuracy = passed / total if total else 0.0
    summary: dict[str, Any] = {
        "layer": layer,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": accuracy,
    }

    if all(isinstance(item, RetrievalCaseResult) for item in results):
        retrieval_items = [item for item in results if isinstance(item, RetrievalCaseResult)]
        selection_items = [item for item in retrieval_items if item.mode == "expected_selection"]
        skip_items = [item for item in retrieval_items if item.mode == "expected_skip"]
        recalls = [item.recall for item in selection_items]
        precisions = [item.precision for item in selection_items if item.precision is not None]
        summary.update(
            {
                "mean_recall": mean(recalls) if recalls else None,
                "mean_precision": mean(precisions) if precisions else None,
                "selection_cases": len(selection_items),
                "skip_cases": len(skip_items),
            }
        )

    if total and all(isinstance(item, GuardrailCaseResult) for item in results):
        guardrail_items = [item for item in results if isinstance(item, GuardrailCaseResult)]
        fake_allows = [
            item
            for item in guardrail_items
            if item.expected_verdict == "deny" and item.observed_verdict == "allow"
        ]
        false_denies = [
            item
            for item in guardrail_items
            if item.expected_verdict == "allow" and item.observed_verdict == "deny"
        ]
        summary.update(
            {
                "unsafe_allows": len(fake_allows),
                "unsafe_allow_case_ids": [item.case_id for item in fake_allows],
                "false_denies": len(false_denies),
            }
        )

    if total and all(isinstance(item, GenerationCaseResult) for item in results):
        generation_items: list[GenerationCaseResult] = [res for res in results]  # type: ignore[misc]
        ex_items = [gen for gen in generation_items if gen.expectation == "result_match"]
        valid_items = [gen for gen in generation_items if gen.expectation == "valid_sql"]
        gen_skips = [gen for gen in generation_items if gen.expectation == "skip"]
        scored = [gen for gen in ex_items if not gen.golden_missing]
        repairs = [gen.repair_attempts for gen in generation_items]
        repaired_saved = [gen for gen in generation_items if gen.passed and gen.repair_attempts > 0]
        generation_latencies = [gen.generation_ms for gen in generation_items]
        execution_latencies = [
            gen.execution_ms for gen in scored if gen.execution_status == "SUCCESS"
        ]
        percentiles = _percentiles(generation_latencies)
        exec_percentiles = _percentiles(execution_latencies)
        summary.update(
            {
                "execution_accuracy": (
                    sum(1 for gen in scored if gen.passed) / len(scored) if scored else None
                ),
                "ex_scored_cases": len(scored),
                "golden_missing_cases": sum(1 for gen in ex_items if gen.golden_missing),
                "valid_sql_cases": len(valid_items),
                "valid_rate": (
                    sum(1 for gen in valid_items if gen.passed) / len(valid_items)
                    if valid_items
                    else None
                ),
                "skip_cases": len(gen_skips),
                "avg_repairs": mean(repairs) if repairs else 0.0,
                "repaired_saved": len(repaired_saved),
                "generation_latency_ms": percentiles,
                "execution_latency_ms": exec_percentiles,
            }
        )

    if total and all(isinstance(case_result, E2ECaseResult) for case_result in results):
        e2e_items = [e2e for e2e in results if isinstance(e2e, E2ECaseResult)]
        skippable = [e2e for e2e in e2e_items if e2e.expectation == "skip"]
        wall_times = [e2e.wall_ms for e2e in e2e_items]
        percentiles = _percentiles(wall_times)
        summary.update(
            {
                "skip_cases": len(skippable),
                "wall_latency_ms": percentiles,
                "avg_repairs": mean([e2e.repair_attempts for e2e in e2e_items]),
            }
        )

    return summary


def _fail_rows(
    results: Sequence[Any],
) -> list[tuple[str, str, str]]:
    """One diagnostic line per failing case (case id, question header, reason)."""
    rows: list[tuple[str, str, str]] = []
    for item in results:
        if item.passed:
            continue
        question = getattr(item, "question", "")
        detail = getattr(item, "detail", None) or "no detail recorded"
        rows.append((str(item.case_id), question, detail))
    return rows


def render_layer_markdown(
    layer: str,
    summary: dict[str, Any],
    results: Sequence[Any],
) -> str:
    """Render a Markdown report block for one layer: metrics table + failures."""
    lines: list[str] = [f"## {layer} evaluation", "", "| Metric | Value |", "|---|---|"]
    known_keys = {
        "total",
        "passed",
        "failed",
        "accuracy",
        "mean_recall",
        "mean_precision",
        "selection_cases",
        "skip_cases",
        "execution_accuracy",
        "ex_scored_cases",
        "golden_missing_cases",
        "valid_sql_cases",
        "valid_rate",
        "avg_repairs",
        "repaired_saved",
    }
    for key in summary:
        if key == "layer" or not isinstance(summary[key], int | float | type(None)):
            continue
        if key in known_keys or key.endswith("_latency_ms") or key == "unsafe_allows":
            value = summary[key]
            if isinstance(value, dict):
                value = f"p50={_f(value.get('p50'), 1)} p95={_f(value.get('p95'), 1)}"
            elif isinstance(value, float):
                value = f"{value:.4f}"
            lines.append(f"| `{key}` | {value} |")

    lines.append("")
    lines.append("### Reported counts")

    for key in ("unsafe_allows", "false_denies"):
        if key in summary:
            lines.append(f"- `{key}`: {summary.get(key)}")

    failures = _fail_rows(results)
    lines.append("")
    lines.append(f"### Failures ({len(failures)})")
    if not failures:
        lines.append("All cases passed.")
    else:
        lines.append("| Case | Question | Detail |")
        lines.append("|---|---|---|")
        for case_id, question, detail in failures:
            question_short = question.replace("\n", " ")[:80]
            detail_short = detail.replace("\n", " ").replace("|", "\\|")[:180]
            lines.append(f"| `{case_id}` | {question_short} | {detail_short} |")

    lines.append("")
    return "\n".join(lines)


def results_to_json_payload(
    summary: dict[str, Any],
    results: Sequence[Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """Compose the machine-readable run artifact (summary + per-case outputs)."""
    return {
        "meta": meta,
        "summary": summary,
        "results": json.loads(
            json.dumps(list(results), default=lambda obj: obj.model_dump(mode="json"))
        ),
    }
