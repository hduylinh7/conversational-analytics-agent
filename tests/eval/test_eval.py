"""Evaluation framework regression tests.

Covers the deterministic layers end to end (no LLM API, no PostgreSQL):
- YAML case loading per layer
- result-set comparison semantics (EX scoring core)
- guardrail runner verdicts against the real AST validator
- retrieval runner selection/refusal scoring against the real retriever
- generation runner skip path (refusal before any LLM call)
"""

from pathlib import Path

import pytest

from app.eval.cases import GoldenResultStore, load_cases
from app.eval.compare import compare_result_sets
from app.eval.runners import run_generation_case, run_guardrail_case, run_retrieval_case
from app.eval.schemas import (
    E2ECase,
    EvalLayer,
    GenerationCase,
    GuardrailCase,
    RetrievalCase,
)
from app.agents.llm_client import MockLLMClient
from app.agents.sql_generator import SQLGenerator
from app.semantic.retrieval.retriever import SemanticRetriever
from app.semantic.retrieval.schemas import RetrievalConfig
from app.security.validator import SQLGuardrailValidator

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "eval" / "cases"


# ---------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------


def test_load_all_case_layers() -> None:
    """Every shipped case file must parse with its layer schema."""
    layers = {
        EvalLayer.RETRIEVAL: RetrievalCase,
        EvalLayer.GUARDRAIL: GuardrailCase,
        EvalLayer.GENERATION: GenerationCase,
        EvalLayer.END_TO_END: E2ECase,
    }
    for layer, case_type in layers.items():
        cases = load_cases(CASES_DIR, layer)
        assert cases, f"{layer.value} cases must not be empty"
        assert all(isinstance(case, case_type) for case in cases)


def test_load_cases_tag_filter() -> None:
    """The suite tag filter must narrow cases to matching tags."""
    cases = load_cases(CASES_DIR, EvalLayer.GENERATION, suite="smoke")
    assert cases
    assert all("smoke" in case.tags for case in cases)


def test_load_golden_results_for_materialized_cases() -> None:
    """Golden results shipped in the repository must parse and be non-empty."""
    store = GoldenResultStore(REPO_ROOT / "eval" / "golden_results")
    golden = store.get("gen_rev_by_state")
    assert golden is not None
    assert golden.rows, "golden result rows missing"
    assert golden.columns, "golden result columns missing"


# ---------------------------------------------------------------------
# Result-set comparison (EX core)
# ---------------------------------------------------------------------


def test_compare_exact_multiset_match() -> None:
    """Identical rows in different order and with different column names pass."""
    outcome = compare_result_sets(
        ["a", "b"], [["SP", 100.0], ["RJ", 50.0]], ["state", "value"], [["RJ", 50.0], ["SP", 100.0]]
    )
    assert outcome.passed
    assert outcome.mode == "EXACT"


def test_compare_numeric_tolerance() -> None:
    """Float noise below tolerance still passes via the tolerant route."""
    outcome = compare_result_sets(
        ["v"], [[1_000_000.0000000001]], ["x"], [[1_000_000.0000000099]]
    )
    assert outcome.passed


def test_compare_row_count_mismatch() -> None:
    """Different row counts fail with an explicit count diagnostic."""
    outcome = compare_result_sets(["v"], [[1], [2]], ["x"], [[1]])
    assert not outcome.passed
    assert "Row count mismatch" in (outcome.detail or "")


def test_compare_arity_mismatch() -> None:
    """Different column arities fail explicitly."""
    outcome = compare_result_sets(["a", "b"], [[1, 2]], ["x"], [[1]])
    assert not outcome.passed
    assert outcome.arity_mismatch


def test_compare_value_mismatch() -> None:
    """Different values fail and record at least one example pair."""
    outcome = compare_result_sets(["a", "b"], [["SP", 100.0]], ["x", "y"], [["SP", 200.0]])
    assert not outcome.passed
    assert outcome.mismatched_examples


def test_compare_empty_both_pass() -> None:
    """Two empty result sets are equal."""
    outcome = compare_result_sets(["v"], [], ["x"], [])
    assert outcome.passed


# ---------------------------------------------------------------------
# Guardrail layer (real validator, security-only mode)
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "verdict"),
    [
        ("UPDATE business.customers SET customer_city = 'x';", "deny"),
        ("DROP TABLE business.orders;", "deny"),
        ("SELECT order_id FROM business.orders; DROP TABLE business.orders;", "deny"),
        ("SELECT pg_sleep(50);", "deny"),
        ("SELECT email FROM app.users;", "deny"),
        ("SELECT COUNT(DISTINCT order_id) FROM business.orders;", "allow"),
    ],
)
def test_guardrail_runner_verdicts(sql: str, verdict: str) -> None:
    """Block/allow verdicts must match the shipped security policy."""
    case = GuardrailCase(id="inline_case", tags=[], sql=sql, verdict=verdict)
    result = run_guardrail_case(SQLGuardrailValidator(), case)
    assert result.passed, result.detail


def test_guardrail_runner_expected_rules() -> None:
    """ Explicit expected rules must be verified against observed ones."""
    case = GuardrailCase(
        id="inline_case",
        tags=[],
        sql="INSERT INTO business.orders VALUES ('x');",
        verdict="deny",
        expected_rules=["SELECT_ONLY"],
    )
    result = run_guardrail_case(SQLGuardrailValidator(), case)
    assert result.passed, result.detail
    assert result.observed_verdict == "deny"


# ---------------------------------------------------------------------
# Retrieval layer (real retriever, embeddings disabled)
# ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def retriever() -> SemanticRetriever:
    """Shared deterministic retriever instance (embeddings disabled)."""
    return SemanticRetriever(config=RetrievalConfig(enable_embeddings=False))


def test_retrieval_runner_hit(retriever: SemanticRetriever) -> None:
    """A clear revenue-by-state question must retrieve the expected contract."""
    result = run_retrieval_case(
        retriever,
        "regression_rev_by_state",
        "Tổng doanh thu theo bang khách hàng",
        expected_metrics=["total_revenue"],
        expected_dimensions=["customer_state"],
        expect_skip=False,
    )
    assert result.passed, result.detail


def test_retrieval_runner_skip(retriever: SemanticRetriever) -> None:
    """A chatty non-analytics question must be refused."""
    result = run_retrieval_case(
        retriever,
        "regression_skip",
        "Xin chào, bạn là ai vậy?",
        expected_metrics=[],
        expected_dimensions=[],
        expect_skip=True,
    )
    assert result.passed, result.detail
    assert not result.selected_metrics
    assert not result.selected_dimensions


# ---------------------------------------------------------------------
# Generation layer: deterministic refusal path (no LLM, no DB needed)
# ---------------------------------------------------------------------


async def test_generation_runner_refusal_path(retriever: SemanticRetriever) -> None:
    """A skip case must refuse retrieval-side and never call the LLM."""
    case = GenerationCase(
        id="regeneration_skip",
        tags=[],
        questions=["Xin chào, bạn là ai vậy?"],
        skip=True,
    )
    generator = SQLGenerator(llm_client=MockLLMClient())
    result = await run_generation_case(
        retriever,
        generator,
        repository=object(),  # Never touched on the skip path.
        case=case,
        question="Xin chào, bạn là ai vậy?",
        golden_store=GoldenResultStore(None),
    )
    assert result.passed, result.detail
    assert result.skipped_by_retrieval
