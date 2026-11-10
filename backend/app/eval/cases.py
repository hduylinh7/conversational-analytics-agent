"""YAML case loading and golden result storage for the evaluation framework.

Case files live under a benchmark directory (default ``eval/cases/``), one
file per layer named ``<layer>.yaml`` with a top-level ``cases`` list.
Golden result sets live in a sibling directory (default
``eval/golden_results/``), one JSON file per case id.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.core.logging import get_logger
from app.eval.schemas import (
    E2ECase,
    EvalLayer,
    GenerationCase,
    GoldenResult,
    GuardrailCase,
    RetrievalCase,
)

logger = get_logger(__name__)


class CaseLoadError(ValueError):
    """Raised when an evaluation case file cannot be listed or parsed."""


def _load_case_file(path: Path) -> list[dict]:
    """Read a YAML file and return its top-level ``cases`` list."""
    if not path.exists():
        raise CaseLoadError(f"Case file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, ValueError) as exc:
        raise CaseLoadError(f"Cannot read case file {path}: {exc}") from exc

    if not isinstance(data, dict) or "cases" not in data:
        raise CaseLoadError(f"Case file {path} must contain a top-level 'cases' list.")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise CaseLoadError(f"'cases' in {path} must be a list of objects.")
    return [c for c in cases if c is not None]


def load_cases(
    benchmark_dir: Path | str,
    layer: EvalLayer,
    *,
    suite: str | None = None,
) -> list[RetrievalCase | GuardrailCase | GenerationCase | E2ECase]:
    """Load (and optionally tag-filter) evaluation cases for one layer.

    Args:
        benchmark_dir: Directory holding ``<layer>.yaml`` case files.
        layer: Evaluation layer to load.
        suite: Optional tag filter; a case passes when the tag is listed in
            its ``tags``. ``None`` keeps every case.

    Returns:
        Parsed and validated cases in file order.

    Raises:
        CaseLoadError: On missing files, malformed YAML, or validation issues.
    """
    benchmark_path = Path(benchmark_dir)
    file_path = benchmark_path / f"{layer.value}.yaml"
    raw_cases = _load_case_file(file_path)

    case_type: type
    match layer:
        case EvalLayer.RETRIEVAL:
            case_type = RetrievalCase
        case EvalLayer.GUARDRAIL:
            case_type = GuardrailCase
        case EvalLayer.GENERATION:
            case_type = GenerationCase
        case EvalLayer.END_TO_END:
            case_type = E2ECase

    loaded: list[RetrievalCase | GuardrailCase | GenerationCase | E2ECase] = []
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise CaseLoadError(f"{file_path}[{index}] must be a mapping.")
        try:
            case = case_type.model_validate(raw)
        except ValidationError as exc:
            raise CaseLoadError(f"{file_path}[{index}] invalid case: {exc}") from exc
        if suite is None or suite in case.tags:
            loaded.append(case)

    logger.info(
        "Loaded %d %s cases from %s (suite=%s)",
        len(loaded),
        layer.value,
        file_path,
        suite or "all",
    )
    return loaded


class GoldenResultStore:
    """Reads materialized golden result sets from a ``golden_results`` folder."""

    def __init__(self, directory: Path | str | None = None) -> None:
        """Point the store at a golden results directory (loaded lazily)."""
        self._directory = Path(directory) if directory is not None else None
        self._cache: dict[str, GoldenResult | None] = {}

    def get(self, case_id: str) -> GoldenResult | None:
        """Return the golden result for a case id, or None when absent."""
        if self._directory is None:
            return None
        if case_id not in self._cache:
            path = self._directory / f"{case_id}.json"
            if not path.exists():
                self._cache[case_id] = None
            else:
                with path.open("r", encoding="utf-8") as handle:
                    self._cache[case_id] = GoldenResult.model_validate_json(handle.read())
        return self._cache[case_id]
