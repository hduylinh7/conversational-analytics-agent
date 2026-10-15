"""Interactive test runner for the Semantic Retrieval module."""

import argparse
import json
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Configure stdout for UTF-8 on Windows
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.semantic.retrieval import retrieve


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test Semantic Retrieval for analytics questions."
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="Phí vận chuyển theo bang của khách hàng.",
        help="Natural language analytics question to test.",
    )
    args = parser.parse_args()

    print("=" * 80)
    print(f"QUESTION: {args.question}")
    print("=" * 80)

    result = retrieve(args.question)

    print(json.dumps(result.model_dump(), indent=2, ensure_ascii=False))
    print("=" * 80)
    print(f"STATUS: {result.status.value}")
    print(f"SAFE FOR DIRECT EXECUTION: {result.is_safe_for_direct_execution}")
    print(f"CONFIDENCE SCORE: {result.confidence_score}")
    print(f"SELECTED METRICS: {[m.name for m in result.selected_metrics]}")
    print(f"SELECTED DIMENSIONS: {[d.name for d in result.selected_dimensions]}")
    print(f"REQUIRED TABLES: {result.required_tables}")
    if result.resolved_join_path and result.resolved_join_path.steps:
        print("JOIN PATH STEPS:")
        for step in result.resolved_join_path.steps:
            print(f"  -> JOIN {step.to_table} ON {step.join_condition} ({step.cardinality})")
    if result.warnings:
        print("WARNINGS:")
        for w in result.warnings:
            print(f"  * {w}")
    print("=" * 80)


if __name__ == "__main__":
    main()
