"""Offline evaluation framework for the conversational analytics agent.

Layered benchmarking aligned with the LangGraph pipeline stages:

- ``retrieval``: semantic retrieval precision/recall and refusal behavior (no LLM).
- ``guardrail``: AST security verdict correctness (no LLM).
- ``generation``: Text-to-SQL validity and execution accuracy against golden SQL.
- ``e2e``: full graph outcome fidelity (success / refusal / failure stage).

Entry point: ``eval/run_eval.py`` at the repository root.
"""

from app.eval.cases import GoldenResultStore, load_cases
from app.eval.compare import compare_result_sets
from app.eval.report import render_layer_markdown, summarize
from app.eval.runners import (
    run_e2e_case,
    run_generation_case,
    run_guardrail_case,
    run_retrieval_case,
)
from app.eval.schemas import EvalLayer

__all__ = [
    "EvalLayer",
    "GoldenResultStore",
    "compare_result_sets",
    "load_cases",
    "render_layer_markdown",
    "run_e2e_case",
    "run_generation_case",
    "run_guardrail_case",
    "run_retrieval_case",
    "summarize",
]
