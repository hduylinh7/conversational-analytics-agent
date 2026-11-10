"""Materialize golden result sets for generation/e2e evaluation cases.

Runs each case's golden SQL once against PostgreSQL (through the same
guardrail-sanitizing AnalyticsRepository the pipeline uses) and writes
``eval/golden_results/<case_id>.json``. Later evaluation runs compare the
agent's rows against these stored sets instead of re-running golden SQL.

    python scripts/materialize_golden_results.py            # materialize missing
    python scripts/materialize_golden_results.py --force    # overwrite all
    python scripts/materialize_golden_results.py --case gen_rev_by_state
    python scripts/materialize_golden_results.py --layers generation e2e
"""

import argparse
import asyncio
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.analytics import AnalyticsRepository
from app.repositories.schemas import ExecutionResult, ExecutionStatus

from app.eval.cases import load_cases
from app.eval.schemas import EvalLayer, GenerationCase

logger = get_logger(__name__)

REPO_ROOT = BACKEND_DIR.parent
GOLDEN_DIR = REPO_ROOT / "eval" / "golden_results"


async def materialize_case(
    repository: AnalyticsRepository,
    case: GenerationCase,
    force: bool,
) -> str:
    """Execute one case's golden SQL and persist its result set to disk.

    Returns a status string: 'ok', 'failed', 'missing_sql', or 'skipped'.
    """
    if not case.result_match and case.skip:
        return "skipped"
    if not case.golden_sql:
        return "missing_sql"

    golden_path = GOLDEN_DIR / f"{case.id}.json"
    if golden_path.exists() and not force:
        return "skipped"

    result: ExecutionResult = await repository.execute(case.golden_sql)
    if result.status != ExecutionStatus.SUCCESS:
        logger.error(
            "Golden SQL for %s failed to execute [%s]: %s",
            case.id,
            result.status.value,
            result.error_message,
        )
        return "failed"
    if result.truncated:
        logger.warning(
            "Golden result for %s was truncated at %d rows (row cap applied); "
            "result_match comparisons will only see the capped rows.",
            case.id,
            result.row_count,
        )

    payload = {
        "caseId": case.id,
        "goldenSql": result.sql,
        "columns": result.columns,
        "rows": result.rows,
        "rowCount": result.row_count,
        "truncated": result.truncated,
        "materializedAt": datetime.now(UTC).isoformat(),
    }
    golden_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  [OK] {case.id}: {result.row_count} rows -> {golden_path.name}")
    return "ok"


async def run(args: argparse.Namespace) -> int:
    """Materialize all eligible golden result sets for the requested layers."""
    settings = get_settings()
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    print(
        f"PostgreSQL: {settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/"
        f"{settings.POSTGRES_DB} | golden dir: {GOLDEN_DIR}"
    )

    repository = AnalyticsRepository()
    statuses: dict[str, str] = {}
    for layer in args.layers:
        cases = load_cases(args.cases_dir, EvalLayer(layer))
        matching = [case for case in cases if isinstance(case, GenerationCase)]
        print(f"\n[{layer.value}] {len(matching)} candidate cases")
        for case in matching:
            if args.case and case.id != args.case:
                statuses[case.id] = "skipped"
                continue
            statuses[case.id] = await materialize_case(repository, case, args.force)

    ok = sum(1 for s in statuses.values() if s == "ok")
    failed = sum(1 for s in statuses.values() if s == "failed")
    skipped = sum(1 for s in statuses.values() if s == "skipped")
    missing = sum(1 for s in statuses.values() if s == "missing_sql")
    print(f"\nDone: ok={ok}, failed={failed}, skipped={skipped}, missing_sql={missing}")

    if args.validate_ex and args.layers and failed == 0 and missing == 0:
        # Round-trip check: re-run golden SQL and confirm byte-stable comparison.
        from app.eval.compare import compare_result_sets

        mismatches = 0
        for case_id, status in statuses.items():
            if status != "ok":
                continue
            with (GOLDEN_DIR / f"{case_id}.json").open("r", encoding="utf-8") as handle:
                golden = json.load(handle)
            re_run = await repository.execute(golden["goldenSql"])
            if re_run.status != ExecutionStatus.SUCCESS:
                mismatches += 1
                continue
            outcome = compare_result_sets(
                golden["columns"], golden["rows"], re_run.columns, re_run.rows
            )
            if not outcome.passed:
                mismatches += 1
                print(f"  [UNSTABLE] {case_id}: golden SQL outputs are not deterministic!")
                print(f"    {outcome.detail}")
        if mismatches:
            print(f"\n{mismatches} golden SQL queries produced unstable results.")
            return 1
        print("\nAll golden result sets reproduce deterministically.")

    return 1 if failed or missing else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the golden result materializer."""
    parser = argparse.ArgumentParser(description=__doc__)
    layers = ",".join(layer.value for layer in EvalLayer)
    parser.add_argument(
        "--layers",
        nargs="+",
        choices=list(EvalLayer),
        type=EvalLayer,
        default=[EvalLayer.GENERATION],
        help=f"Layers to materialize ({layers}).",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=REPO_ROOT / "eval" / "cases",
        help="Directory holding <layer>.yaml case files.",
    )
    parser.add_argument("--case", default=None, help="Materialize a single case id.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing golden result files.",
    )
    parser.add_argument(
        "--validate-ex",
        action="store_true",
        help="Re-run every golden SQL after materialization to prove determinism.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    exit_code = asyncio.run(run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
