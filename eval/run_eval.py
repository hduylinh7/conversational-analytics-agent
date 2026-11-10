"""Evaluation CLI for the conversational analytics agent.

Runs layered benchmarks against the repository's evaluation cases:

    python eval/run_eval.py --layers retrieval guardrail   # deterministic, CI-safe
    python eval/run_eval.py --layers generation            # needs a configured LLM provider + DB
    python eval/run_eval.py --layers e2e --model-override meta-llama/llama-3.3-70b-instruct
    python eval/run_eval.py --layers retrieval --suite revenue --output reports/eval/run1

Deterministic layers (retrieval, guardrail) require no LLM API access.
Generation/e2e layers call the configured LLM provider (or the mock provider
when ``LLM_PROVIDER=mock``) and need PostgreSQL with the Olist data ingested
plus materialized golden results for ``result_match`` cases.
"""

import argparse
import asyncio
import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.agents.graph.builder import build_analytics_graph
from app.agents.graph.nodes import AnalyticsNodes
from app.agents.llm_client import create_llm_client
from app.agents.sql_generator import SQLGenerator
from app.core.config import get_settings
from app.repositories.analytics import AnalyticsRepository
from app.semantic.retrieval.retriever import SemanticRetriever
from app.semantic.retrieval.schemas import RetrievalConfig
from app.security.validator import SQLGuardrailValidator

from app.eval.cases import GoldenResultStore, load_cases
from app.eval.report import render_layer_markdown, results_to_json_payload, summarize
from app.eval.runners import (
    run_e2e_case,
    run_generation_case,
    run_guardrail_case,
    run_retrieval_case,
)
from app.eval.schemas import E2ECase, EvalLayer, GenerationCase

REPO_ROOT = BACKEND_DIR.parent
DEFAULT_CASES_DIR = REPO_ROOT / "eval" / "cases"
DEFAULT_GOLDEN_DIR = REPO_ROOT / "eval" / "golden_results"
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "eval"


def _retriever() -> SemanticRetriever:
    """Build the semantic retriever with embeddings disabled (deterministic)."""
    return SemanticRetriever(config=RetrievalConfig(enable_embeddings=False))


async def run_layer(
    layer: EvalLayer,
    cases_dir: Path,
    golden_dir: Path,
    model: str | None,
    suite: str | None = None,
    delay: float = 0.0,
) -> tuple[dict[str, Any], list[Any]]:
    """Run one layer and return its summary with per-case results."""
    cases = load_cases(cases_dir, layer, suite=suite)
    results: list[Any] = []

    if layer == EvalLayer.RETRIEVAL:
        retriever = _retriever()
        for case in cases:
            for question in case.questions:
                results.append(
                    run_retrieval_case(
                        retriever,
                        case.id,
                        question,
                        expected_metrics=list(case.retrieval.metrics),
                        expected_dimensions=list(case.retrieval.dimensions),
                        expect_skip=case.retrieval.skip,
                    )
                )

    elif layer == EvalLayer.GUARDRAIL:
        validator = SQLGuardrailValidator()
        results = [run_guardrail_case(validator, case) for case in cases]

    elif layer == EvalLayer.GENERATION:
        retriever = _retriever()
        generator = SQLGenerator(llm_client=create_llm_client())
        repository = AnalyticsRepository()
        golden_store = GoldenResultStore(golden_dir)
        for case in cases:
            if not isinstance(case, GenerationCase):  # pragma: no cover - loader guarantee
                continue
            for question in case.questions:
                if delay > 0.0 and results:
                    await asyncio.sleep(delay)
                results.append(
                    await run_generation_case(
                        retriever,
                        generator,
                        repository,
                        case,
                        question,
                        golden_store=golden_store,
                        model=model,
                    )
                )

    elif layer == EvalLayer.END_TO_END:
        retriever = _retriever()
        llm_client = create_llm_client()
        if model is not None and hasattr(llm_client, "model"):
            llm_client.model = model
        repository = AnalyticsRepository()
        graph = build_analytics_graph(
            AnalyticsNodes(
                retriever=retriever,
                llm_client=llm_client,
                executor=repository.execute,
            )
        )
        golden_store = GoldenResultStore(golden_dir)
        for case in cases:
            if not isinstance(case, E2ECase):  # pragma: no cover - loader guarantee
                continue
            for question in case.questions:
                if delay > 0.0 and results:
                    await asyncio.sleep(delay)
                results.append(
                    await run_e2e_case(
                        graph,
                        case,
                        question,
                        golden_store=golden_store,
                        model=model,
                    )
                )

    return summarize(layer.value, results), results


def _print_summary(summary: dict[str, Any]) -> None:
    """Print headline metrics for one layer run."""
    print(f"\n=== {str(summary['layer']).upper()} SUMMARY ===")
    for key, value in summary.items():
        if key == "layer" or key.endswith("case_ids"):
            continue
        print(f"  {key}: {value}")


async def main_async(args: argparse.Namespace) -> int:
    """Run the requested evaluation layers and emit JSON/Markdown reports."""
    settings = get_settings()
    meta: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "llm_provider": settings.LLM_PROVIDER,
        "llm_model": settings.LLM_MODEL,
        "model_override": args.model_override,
        "cases_dir": str(args.cases_dir),
        "golden_dir": str(args.golden_dir),
        "suite": args.suite,
    }

    report_dir = Path(args.output) if args.output else DEFAULT_REPORT_DIR
    markdown_blocks: list[str] = []

    for layer in args.layers:
        summary, results = await run_layer(
            layer,
            args.cases_dir,
            args.golden_dir,
            args.model_override,
            suite=args.suite,
            delay=args.delay,
        )
        _print_summary(summary)

        payload = results_to_json_payload(summary, results, {"layer": layer.value, **meta})
        report_dir.mkdir(parents=True, exist_ok=True)
        json_path = report_dir / f"{layer.value}_eval.json"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON artifact written to {json_path}")

        markdown_blocks.append(render_layer_markdown(layer.value, summary, results))
        md_path = report_dir / f"{layer.value}_eval.md"
        md_path.write_text(markdown_blocks[-1], encoding="utf-8")

    if args.markdown:
        print("\n" + "".join(block + "\n\n" for block in markdown_blocks))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the evaluation runner."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--layers",
        nargs="+",
        choices=list(EvalLayer),
        type=EvalLayer,
        default=[EvalLayer.RETRIEVAL],
        help="Layers to evaluate (default: retrieval).",
    )
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=DEFAULT_CASES_DIR,
        help="Directory holding <layer>.yaml case files.",
    )
    parser.add_argument(
        "--golden-dir",
        type=Path,
        default=DEFAULT_GOLDEN_DIR,
        help="Directory holding materialized golden result sets.",
    )
    parser.add_argument(
        "--suite",
        default=None,
        help="Run only cases tagged with this suite tag.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Directory for JSON/Markdown artifacts (default reports/eval/).",
    )
    parser.add_argument(
        "--model-override",
        default=None,
        help="LLM model override for generation/e2e layers.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds between generation/e2e records (rate-limit friendly).",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print the full Markdown report at the end.",
    )
    return parser.parse_args(argv)


def main() -> None:
    """CLI entry point."""
    args = parse_args()
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
