"""Per-layer evaluation runners.

Each runner takes pre-built pipeline components plus one case (and question
variant) and returns a typed result. Components are injected so callers (the
`eval/run_eval.py` CLI and the pytest regression suite) control construction:
deterministic layers use real components with no network access, while
generation/e2e layers accept any LLM client (mock or provider-backed).
"""

import time
from typing import Any

from app.agents.schemas import SQLGenerationRequest
from app.agents.sql_generator import SQLGenerator
from app.core.logging import get_logger
from app.eval.cases import GoldenResultStore
from app.eval.compare import compare_result_sets
from app.eval.schemas import (
    E2ECase,
    E2ECaseResult,
    GenerationCase,
    GenerationCaseResult,
    GuardrailCase,
    GuardrailCaseResult,
    RetrievalCaseResult,
)
from app.repositories.analytics import AnalyticsRepository
from app.security.schemas import ValidationResult
from app.security.validator import SQLGuardrailValidator
from app.semantic.retrieval.retriever import SemanticRetriever

logger = get_logger(__name__)


def run_retrieval_case(
    retriever: SemanticRetriever,
    case_id: str,
    question: str,
    *,
    expected_metrics: list[str],
    expected_dimensions: list[str],
    expect_skip: bool,
) -> RetrievalCaseResult:
    """Run one question through the semantic retriever and score the selection.

    Pass rules:
    - ``expected_skip``: refusal must occur (no metric and no dimension selected).
    - otherwise: every expected metric/dimension must appear among the
      selected names. Extra selections reduce reported precision but do not
      fail the case.
    """
    result = retriever.retrieve(question)
    selected_metrics = [metric.name for metric in result.selected_metrics]
    selected_dimensions = [dimension.name for dimension in result.selected_dimensions]

    if expect_skip:
        passed = not selected_metrics and not selected_dimensions
        detail = (
            None
            if passed
            else (
                "Expected refusal, but selected metrics="
                f"{selected_metrics}, dimensions={selected_dimensions}."
            )
        )
        return RetrievalCaseResult(
            case_id=case_id,
            question=question,
            passed=passed,
            mode="expected_skip",
            selected_metrics=selected_metrics,
            selected_dimensions=selected_dimensions,
            detail=detail,
        )

    expected = set(expected_metrics) | set(expected_dimensions)
    selected = set(selected_metrics) | set(selected_dimensions)
    hits = expected & selected
    recall = len(hits) / len(expected) if expected else 1.0
    missing = sorted(expected - selected)
    passed = not missing
    precision = (len(hits) / len(selected)) if selected else None

    detail = None
    if not passed:
        extra = sorted(selected - expected)
        detail = f"Missing expected selections: {missing}." + (
            f" Extra selections: {extra}." if extra else ""
        )

    return RetrievalCaseResult(
        case_id=case_id,
        question=question,
        passed=passed,
        mode="expected_selection",
        recall=recall,
        precision=precision,
        selected_metrics=selected_metrics,
        selected_dimensions=selected_dimensions,
        expected_metrics=expected_metrics,
        expected_dimensions=expected_dimensions,
        detail=detail,
    )


def run_guardrail_case(
    validator: SQLGuardrailValidator,
    case: GuardrailCase,
) -> GuardrailCaseResult:
    """Validate one SQL statement and compare the verdict to the expectation.

    Runs security-only validation (no retrieval context), matching the pure
    safety policy applied to every query before execution. When
    ``expected_rules`` is set for a deny case, the observed violation rules
    must include all of them.
    """
    validation: ValidationResult = validator.validate(case.sql)
    observed_verdict = "allow" if validation.is_valid else "deny"
    observed_rules = sorted({str(violation.rule) for violation in validation.violations})

    passed = observed_verdict == case.verdict
    missing_rules: list[str] = []
    detail: str | None = None

    if case.expected_rules:
        missing_rules = [rule for rule in case.expected_rules if rule not in observed_rules]
        if missing_rules:
            passed = False

    if not passed:
        if observed_verdict != case.verdict:
            detail = (
                f"Expected verdict '{case.verdict}' but observed "
                f"'{observed_verdict}'. Rules: {observed_rules}."
            )
        else:
            detail = f"Expected rules missing among observed {observed_rules}: {missing_rules}."

    return GuardrailCaseResult(
        case_id=case.id,
        passed=passed,
        expected_verdict=case.verdict,
        observed_verdict=observed_verdict,
        observed_rules=observed_rules,
        missing_rules=missing_rules,
        detail=detail,
    )


def _retrieval_refused(retriever: SemanticRetriever, question: str) -> bool:
    """True when retrieval selects no metrics and no dimensions for the question."""
    result = retriever.retrieve(question)
    return not result.selected_metrics and not result.selected_dimensions


async def run_generation_case(
    retriever: SemanticRetriever,
    generator: SQLGenerator,
    repository: AnalyticsRepository,
    case: GenerationCase,
    question: str,
    *,
    golden_store: GoldenResultStore,
    model: str | None = None,
) -> GenerationCaseResult:
    """Score the Text-to-SQL generation layer for one question.

    Flow: retrieve -> (skip check) -> generate (repair loop included) ->
    execute valid SQL -> compare result rows to the materialized golden set.

    Expectation semantics (mutually exclusive):
    - ``skip``: retrieval must refuse the question (no LLM call happens).
    - ``valid_sql``: a guardrail-valid SQL candidate must be produced.
    - ``result_match``: executed rows must match the golden result multiset.
    """
    if case.result_match and case.valid_sql:
        raise ValueError(f"Case {case.id}: result_match and valid_sql are mutually exclusive.")
    if case.skip and (case.result_match or case.valid_sql):
        raise ValueError(f"Case {case.id}: skip cannot be combined with other expectations.")

    expectation = "skip" if case.skip else ("result_match" if case.result_match else "valid_sql")
    started = time.perf_counter()

    retrieval = retriever.retrieve(question)
    retrieval_refused = not retrieval.selected_metrics and not retrieval.selected_dimensions

    if expectation == "skip":
        passed = retrieval_refused
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=passed,
            expectation=expectation,
            skipped_by_retrieval=retrieval_refused,
            generation_ms=(time.perf_counter() - started) * 1000.0,
            detail=None if passed else "Expected refusal but retrieval selected metadata.",
        )

    if retrieval_refused:
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=False,
            expectation=expectation,
            skipped_by_retrieval=True,
            generation_ms=(time.perf_counter() - started) * 1000.0,
            detail=(
                "Retrieval refused the question (no metrics/dimensions); " "generation never ran."
            ),
        )

    request = SQLGenerationRequest(question=question, retrieval_result=retrieval, model=model)
    from app.agents.llm_client import LLMError  # late import to keep runners import-light

    try:
        response = generator.generate(request)
    except LLMError as exc:
        generation_ms = (time.perf_counter() - started) * 1000.0
        message = " ".join(str(exc).split())[:300]
        logger.error("Case %s: LLM provider error: %s", case.id, message)
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=False,
            expectation=expectation,
            is_valid_sql=False,
            generation_ms=generation_ms,
            golden_sql=case.golden_sql,
            detail=f"LLM provider error: {message}",
        )
    generation_ms = (time.perf_counter() - started) * 1000.0

    if not response.is_valid:
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=False,
            expectation=expectation,
            is_valid_sql=False,
            repair_attempts=response.repair_attempts,
            final_sql=None,
            golden_sql=case.golden_sql,
            generation_ms=generation_ms,
            detail=("No guardrail-valid SQL after " f"{response.repair_attempts} repair attempts."),
        )

    if expectation == "valid_sql":
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=True,
            expectation=expectation,
            is_valid_sql=True,
            repair_attempts=response.repair_attempts,
            final_sql=response.final_sql,
            golden_sql=case.golden_sql,
            generation_ms=generation_ms,
        )

    golden = golden_store.get(case.id)
    if golden is None:
        logger.error("Case %s expects result_match but no golden result is materialized.", case.id)
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=False,
            expectation=expectation,
            is_valid_sql=True,
            repair_attempts=response.repair_attempts,
            final_sql=response.final_sql,
            golden_missing=True,
            golden_sql=case.golden_sql,
            generation_ms=generation_ms,
            detail="Golden result missing; run scripts/materialize_golden_results.py first.",
        )

    execution = await repository.execute(response.final_sql or "")

    if not execution.is_success:
        return GenerationCaseResult(
            case_id=case.id,
            question=question,
            passed=False,
            expectation=expectation,
            is_valid_sql=True,
            repair_attempts=response.repair_attempts,
            final_sql=response.final_sql,
            golden_sql=case.golden_sql,
            generation_ms=generation_ms,
            execution_ms=execution.execution_ms,
            execution_status=execution.status.value,
            detail=f"Generated SQL failed to execute: {execution.error_message}",
        )

    outcome = compare_result_sets(
        golden.columns,
        golden.rows,
        execution.columns,
        execution.rows,
    )
    return GenerationCaseResult(
        case_id=case.id,
        question=question,
        passed=outcome.passed,
        expectation=expectation,
        is_valid_sql=True,
        repair_attempts=response.repair_attempts,
        comparison=outcome,
        final_sql=response.final_sql,
        golden_sql=case.golden_sql,
        generation_ms=generation_ms,
        execution_ms=execution.execution_ms,
        execution_status=execution.status.value,
        detail=None if outcome.passed else outcome.detail,
    )


async def run_e2e_case(
    graph: Any,
    case: E2ECase,
    question: str,
    *,
    golden_store: GoldenResultStore,
    config: dict[str, Any] | None = None,
    model: str | None = None,
) -> E2ECaseResult:
    """Invoke the compiled analytics graph for one question and score the outcome.

    Pass rules:
    - ``skip``: the graph must terminate at the retrieval stage with a refusal.
    - otherwise: the pipeline must succeed. When a golden result is
      materialized, executed rows must additionally match it (EX scoring).
    """
    case_id = case.id
    expectation = "skip" if case.skip else "success"
    started = time.perf_counter()

    state: dict[str, Any] = {"question": question}
    if model:
        state["model"] = model
    invoke_config = config or {"recursion_limit": 50}
    final_state = await graph.ainvoke(state, config=invoke_config)
    wall_ms = (time.perf_counter() - started) * 1000.0

    response = final_state.get("response")
    failed_stage = final_state.get("failure_stage")
    repair_attempts = len(final_state.get("repair_history", []))
    success = bool(response and response.success)

    if expectation == "skip":
        passed = not success and failed_stage == "retrieval"
        return E2ECaseResult(
            case_id=case_id,
            question=question,
            passed=passed,
            expectation=expectation,
            success=success,
            failed_stage=failed_stage,
            repair_attempts=repair_attempts,
            wall_ms=wall_ms,
            detail=(
                None
                if passed
                else (
                    f"Expected retrieval refusal; got failed_stage={failed_stage}, "
                    f"success={success}."
                )
            ),
        )

    execution = response.execution if response is not None else None
    if not success or execution is None:
        return E2ECaseResult(
            case_id=case_id,
            question=question,
            passed=False,
            expectation=expectation,
            success=False,
            failed_stage=failed_stage,
            repair_attempts=repair_attempts,
            wall_ms=wall_ms,
            detail=(
                f"Pipeline failed at stage '{failed_stage}': "
                f"{final_state.get('failure_reason') or 'unknown reason'}"
            ),
        )

    golden = golden_store.get(case_id)
    if golden is None:
        return E2ECaseResult(
            case_id=case_id,
            question=question,
            passed=True,
            expectation=expectation,
            success=True,
            failed_stage=None,
            repair_attempts=repair_attempts,
            row_count=execution.row_count,
            wall_ms=wall_ms,
        )

    outcome = compare_result_sets(golden.columns, golden.rows, execution.columns, execution.rows)
    return E2ECaseResult(
        case_id=case_id,
        question=question,
        passed=outcome.passed,
        expectation=expectation,
        success=True,
        failed_stage=None,
        repair_attempts=repair_attempts,
        comparison=outcome,
        row_count=execution.row_count,
        wall_ms=wall_ms,
        detail=None if outcome.passed else outcome.detail,
    )
