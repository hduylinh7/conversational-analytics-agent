"""Interactive CLI test runner for the SQL Generation agent (retriever -> LLM -> guardrails -> repair)."""

import argparse
import io
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Configure stdout for UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.agents.llm_client import create_llm_client
from app.agents.schemas import SQLGenerationRequest, SQLGenerationResponse
from app.agents.sql_generator import SQLGenerator
from app.core.config import get_settings
from app.semantic.loader import get_semantic_catalog
from app.semantic.retrieval.retriever import SemanticRetriever
from app.semantic.retrieval.schemas import RetrievalConfig


DEMO_QUESTIONS = [
    "Tong doanh thu theo danh muc san pham",
    "Phi van chuyen theo bang cua khach hang",
    "So dien thoai moi trong 2017",
    "Tong gia tri thanh toan theo phuong thuc",
]


def print_response(response: SQLGenerationResponse, show_llm_response: bool = False) -> None:
    print("=" * 80)
    print(f"Question          : {response.question}")
    print(f"Model used        : {response.model_used}")
    print(f"Valid             : {response.is_valid}")
    print(f"Repair attempts   : {response.repair_attempts}")
    if response.explanation:
        print(f"Explanation       : {response.explanation}")
    print("-" * 80)
    if show_llm_response and response.llm_response:
        print("FULL LLM RESPONSE (as returned by the provider):")
        print(response.llm_response.strip())
        print("-" * 80)
    if response.raw_sql:
        print("RAW SQL (LLM output):")
        print(response.raw_sql.strip())
        print("-" * 80)
    if response.validation_result:
        if response.validation_result.warnings:
            print("Validation warnings:")
            for warning in response.validation_result.warnings:
                print(f"  - {warning}")
        if response.validation_result.violations:
            print("Validation violations:")
            for violation in response.validation_result.violations:
                print(f"  - [{violation.severity}] {violation.rule}: {violation.message}")
    for step in response.repair_history:
        print(f"Repair step {step.attempt}: rules=[{', '.join(str(v.rule) for v in step.violations)}]")
    print("-" * 80)
    print("FINAL SQL (sanitized, ready for PostgreSQL):")
    print(response.final_sql or "(none - validation failed)")
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Demo the SQL generation pipeline end-to-end.")
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        help="Natural language question (Vietnamese or English). Omit to run all demo questions.",
    )
    parser.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print the user prompt sent to the LLM.",
    )
    parser.add_argument(
        "--show-llm-response",
        action="store_true",
        help="Print the full raw response text returned by the LLM.",
    )
    args = parser.parse_args()

    settings = get_settings()
    print(f"LLM provider: {settings.LLM_PROVIDER} | model: {settings.LLM_MODEL} | repair budget: {settings.SQL_MAX_REPAIR_ATTEMPTS}")

    catalog = get_semantic_catalog()
    retriever = SemanticRetriever(catalog=catalog, config=RetrievalConfig(enable_embeddings=False))
    generator = SQLGenerator(llm_client=create_llm_client())

    questions = [args.question] if args.question else DEMO_QUESTIONS
    for question in questions:
        retrieval = retriever.retrieve(question)
        print(f"\n>>> Retrieval: status={retrieval.status.value}, metrics={[m.name for m in retrieval.selected_metrics]}")
        if args.show_prompt:
            from app.agents.prompts import render_user_prompt

            print("-" * 80)
            print(render_user_prompt(question, retrieval))
            print("-" * 80)
        try:
            response = generator.generate(
                SQLGenerationRequest(question=question, retrieval_result=retrieval)
            )
        except Exception as exc:
            print(f">>> LLM call failed: {exc}")
            continue
        print_response(response, show_llm_response=args.show_llm_response)


if __name__ == "__main__":
    main()
