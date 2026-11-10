"""Result-set comparison for execution-accuracy (EX) scoring.

Two generated SQL queries are considered equivalent when they produce the
same multiset of rows, regardless of column names, row order, or formatting
differences. Numeric cells compare with a relative tolerance so that float
accumulation differences between different-but-equivalent SQL formulations
do not fail equivalent queries.

Strategy:
1. Fast exact phase: canonical multiset equality on typed cell keys.
2. Tolerance phase (only when the exact phase fails): sort both sides by a
   lenient quantized key and run a tolerant pairwise comparison.
"""

import math
from typing import Any

from app.eval.schemas import CompareOutcome, ComparisonMode

DEFAULT_REL_TOL = 1e-4
DEFAULT_ABS_TOL = 0.01


def canonical_cell(value: Any) -> str:
    """Produce a stable string fingerprint of one result cell."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return float(value).hex()
    return str(value)


def exact_row_key(row: list[Any]) -> tuple[str, ...]:
    """Hashable exact-equality key for one result row."""
    return tuple(canonical_cell(cell) for cell in row)


def cells_equal(expected: Any, actual: Any, rel_tol: float, abs_tol: float) -> bool:
    """Compare one expected/actual cell pair with numeric tolerance."""
    if expected is None or actual is None:
        return expected is None and actual is None
    expected_is_num = isinstance(expected, int | float) and not isinstance(expected, bool)
    actual_is_num = isinstance(actual, int | float) and not isinstance(actual, bool)
    if expected_is_num and actual_is_num:
        if isinstance(expected, float | int) and isinstance(actual, float | int):
            return math.isclose(float(expected), float(actual), rel_tol=rel_tol, abs_tol=abs_tol)
    return str(expected) == str(actual)


def rows_equal(
    expected: list[Any],
    actual: list[Any],
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
) -> bool:
    """Compare one expected/actual row pair with equal arity."""
    if len(expected) != len(actual):
        return False
    return all(
        cells_equal(expected_cell, actual_cell, rel_tol, abs_tol)
        for expected_cell, actual_cell in zip(expected, actual, strict=False)
    )


def _lenient_sort_key(row: list[Any]) -> tuple[tuple[int, str], ...]:
    """Total order over rows that keeps numeric noise adjacent after sorting."""
    key_parts: list[tuple[int, str]] = []
    for cell in row:
        if cell is None:
            key_parts.append((0, ""))
        elif isinstance(cell, bool):
            key_parts.append((0, "1" if cell else "0"))
        elif isinstance(cell, int | float):
            key_parts.append((0, f"{float(cell):.6f}"))
        else:
            key_parts.append((1, str(cell)))
    return tuple(key_parts)


def compare_result_sets(
    expected_columns: list[str],
    expected_rows: list[list[Any]],
    actual_columns: list[str],
    actual_rows: list[list[Any]],
    *,
    rel_tol: float = DEFAULT_REL_TOL,
    abs_tol: float = DEFAULT_ABS_TOL,
) -> CompareOutcome:
    """Compare an observed result set against the golden reference result.

    Column names are ignored; row multisets must match (order-insensitive).
    Numeric cells compare with ``rel_tol``/``abs_tol`` tolerance per row.
    """
    arity_expected = len(expected_columns)
    arity_actual = len(actual_columns)

    if len(expected_rows) != len(actual_rows):
        return CompareOutcome(
            passed=False,
            expected_rows=len(expected_rows),
            actual_rows=len(actual_rows),
            detail=(
                f"Row count mismatch: expected {len(expected_rows)}, got " f"{len(actual_rows)}."
            ),
        )
    if arity_expected != arity_actual:
        return CompareOutcome(
            passed=False,
            expected_rows=len(expected_rows),
            actual_rows=len(actual_rows),
            arity_mismatch=True,
            detail=(f"Column arity mismatch: expected {arity_expected}, got " f"{arity_actual}."),
        )
    if not expected_rows:
        return CompareOutcome(
            passed=True,
            mode=ComparisonMode.EXACT,
            expected_rows=0,
            actual_rows=0,
        )

    expected_keys = [exact_row_key(row) for row in expected_rows]
    actual_keys = [exact_row_key(row) for row in actual_rows]
    if sorted(expected_keys) == sorted(actual_keys):
        return CompareOutcome(
            passed=True,
            mode=ComparisonMode.EXACT,
            expected_rows=len(expected_rows),
            actual_rows=len(actual_rows),
        )

    # Tolerance phase: sort by a lenient key and compare pairs.
    expected_sorted = sorted(expected_rows, key=_lenient_sort_key)
    actual_sorted = sorted(actual_rows, key=_lenient_sort_key)
    mismatches: list[str] = []
    matched = True
    for expected_row, actual_row in zip(expected_sorted, actual_sorted, strict=False):
        if not rows_equal(expected_row, actual_row, rel_tol, abs_tol):
            matched = False
            if len(mismatches) < 3:
                mismatches.append(f"expected={expected_row!s} actual={actual_row!s}")

    if matched:
        return CompareOutcome(
            passed=True,
            mode=ComparisonMode.TOLERANT,
            expected_rows=len(expected_rows),
            actual_rows=len(actual_rows),
        )

    return CompareOutcome(
        passed=False,
        expected_rows=len(expected_rows),
        actual_rows=len(actual_rows),
        mismatched_examples=mismatches,
        detail=(
            f"{len(mismatches)}+ row pairs differ beyond tolerance "
            f"(rel_tol={rel_tol}, abs_tol={abs_tol})."
        ),
    )
