"""Visual CLI demo of the LangGraph analytics pipeline.

Runs the compiled StateGraph offline (deterministic fake retriever/executor,
real AST guardrails) and prints every node transition in the terminal, so the
control flow - including both repair loops - can be observed step by step.

Usage (from the repository root):
    python scripts/demo_langgraph.py
    python scripts/demo_langgraph.py --scenario execution-repair
    python scripts/demo_langgraph.py --list
    python scripts/demo_langgraph.py --mermaid
"""

import argparse
import asyncio
import io
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# Configure stdout for UTF-8 on Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.agents.graph import AnalyticsNodes, build_analytics_graph
from app.agents.llm_client import BaseLLMClient, LLMError, MockLLMClient
from app.repositories.schemas import ExecutionResult, ExecutionStatus
from app.semantic.retrieval.schemas import (
    RetrievalResult,
    RetrievalStatus,
    SelectedDimension,
    SelectedMetric,
)

# The trace itself is the demo output; silence internal repair/error logs.
logging.getLogger("app").setLevel(logging.CRITICAL)

WIDTH = 82

VALID_SQL = """SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
JOIN business.customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state
ORDER BY order_count DESC"""

BROKEN_COLUMN_SQL = """SELECT
    c.customer_state,
    COUNT(DISTINCT o.order_id) AS order_count
FROM business.orders o
JOIN business.customers c ON o.customer_id = c.customer_id
WHERE o.order_statuz = 'delivered'
GROUP BY c.customer_state
ORDER BY order_count DESC"""

HACK_SQL = "DROP TABLE business.orders"
SQLSTATE_UNDEFINED_COLUMN = "42703"
QUESTION = "How many delivered orders per customer state?"


def fenced(sql: str) -> str:
    """Wrap SQL in a ```sql fence exactly as LLM responses appear."""
    return f"```sql\n{sql}\n```"


def one_line(text: str, limit: int = 90) -> str:
    """Collapse multi-line text into a single truncated terminal line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def make_retrieval(with_metrics: bool = True) -> RetrievalResult:
    """Build a deterministic semantic retrieval fixture."""
    metrics = (
        [
            SelectedMetric(
                name="order_count",
                description="Number of orders",
                expression="COUNT(DISTINCT o.order_id)",
                aggregation="COUNT",
                source_tables=["business.orders"],
                score=0.91,
            )
        ]
        if with_metrics
        else []
    )
    dimensions = (
        [
            SelectedDimension(
                name="customer_state",
                description="Customer state",
                source_table="business.customers",
                source_column="customer_state",
                data_type="VARCHAR(2)",
                score=0.84,
            )
        ]
        if with_metrics
        else []
    )
    return RetrievalResult(
        question=QUESTION,
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.88,
        selected_metrics=metrics,
        selected_dimensions=dimensions,
        is_safe_for_direct_execution=True,
    )


class StaticRetriever:
    """Retriever stub returning a canned RetrievalResult."""

    def __init__(self, result: RetrievalResult) -> None:
        self.result = result

    def retrieve(self, question: str) -> RetrievalResult:
        return self.result


class DemoExecutor:
    """Fake PostgreSQL executor: succeeds unless the SQL contains a marker."""

    def __init__(self, fail_marker: str | None = None) -> None:
        self.fail_marker = fail_marker
        self.calls = 0

    async def __call__(self, sql: str) -> ExecutionResult:
        self.calls += 1
        await asyncio.sleep(0)
        if self.fail_marker and self.fail_marker in sql:
            return ExecutionResult(
                status=ExecutionStatus.DATABASE_ERROR,
                sql=sql,
                error_code=SQLSTATE_UNDEFINED_COLUMN,
                error_message='column "o.order_statuz" does not exist',
                execution_ms=3.4,
            )
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            sql=sql,
            columns=["customer_state", "order_count"],
            rows=[["SP", 41746], ["RJ", 12856], ["MG", 11471]],
            row_count=3,
            total_rows_fetched=3,
            execution_ms=5.2,
            limit_applied=100,
        )


class FailingLLM(BaseLLMClient):
    """LLM stub that always raises a provider error."""

    def generate(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("provider down")

    async def generate_async(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        raise LLMError("OpenRouter API error (404): model `string` not found")


@dataclass
class Scenario:
    """One demo run: retrieval fixture, LLM script, executor, and budgets."""

    key: str
    title: str
    description: str
    retrieval: RetrievalResult
    llm: BaseLLMClient
    executor: DemoExecutor
    max_guardrail_repairs: int = 3
    max_execution_repairs: int = 2


def build_scenarios() -> dict[str, Scenario]:
    """Return the deterministic scenarios shown by the CLI."""
    return {
        "happy": Scenario(
            key="happy",
            title="Happy path (no repair)",
            description="First SQL passes guardrails and executes successfully.",
            retrieval=make_retrieval(),
            llm=MockLLMClient(responses=[fenced(VALID_SQL)]),
            executor=DemoExecutor(),
        ),
        "guardrail-repair": Scenario(
            key="guardrail-repair",
            title="Guardrail repair",
            description="First attempt is a mutation, rejected by AST guardrails; the LLM is asked to fix it.",
            retrieval=make_retrieval(),
            llm=MockLLMClient(responses=[fenced(HACK_SQL), fenced(VALID_SQL)]),
            executor=DemoExecutor(),
            max_guardrail_repairs=3,
        ),
        "execution-repair": Scenario(
            key="execution-repair",
            title="Execution repair (SQLSTATE feedback)",
            description="First SQL passes guardrails but fails in PostgreSQL with 42703; SQLSTATE is fed back to the LLM.",
            retrieval=make_retrieval(),
            llm=MockLLMClient(responses=[fenced(BROKEN_COLUMN_SQL), fenced(VALID_SQL)]),
            executor=DemoExecutor(fail_marker="order_statuz"),
            max_guardrail_repairs=3,
            max_execution_repairs=2,
        ),
        "guardrail-budget": Scenario(
            key="guardrail-budget",
            title="Guardrail budget exhausted",
            description="The LLM keeps emitting mutations until the guardrail repair budget runs out.",
            retrieval=make_retrieval(),
            llm=MockLLMClient(responses=[fenced(HACK_SQL)] * 10),
            executor=DemoExecutor(),
            max_guardrail_repairs=2,
        ),
        "execution-budget": Scenario(
            key="execution-budget",
            title="Execution budget exhausted",
            description="The query keeps failing at runtime until the execution repair budget runs out.",
            retrieval=make_retrieval(),
            llm=MockLLMClient(responses=[fenced(BROKEN_COLUMN_SQL)] * 10),
            executor=DemoExecutor(fail_marker="order_statuz"),
            max_guardrail_repairs=5,
            max_execution_repairs=2,
        ),
        "retrieval-skip": Scenario(
            key="retrieval-skip",
            title="Retrieval gate (non-analytics question)",
            description="No metric/dimension matches, so the graph finalizes before calling the LLM.",
            retrieval=make_retrieval(with_metrics=False),
            llm=MockLLMClient(responses=[fenced(VALID_SQL)]),
            executor=DemoExecutor(),
        ),
        "llm-error": Scenario(
            key="llm-error",
            title="LLM provider error",
            description="The provider raises during generation; the graph finalizes gracefully.",
            retrieval=make_retrieval(),
            llm=FailingLLM(),
            executor=DemoExecutor(),
        ),
    }


def summarize_update(node: str, update: dict[str, Any]) -> str:
    """Turn one node's partial state update into a single readable line."""
    if node == "retrieve":
        retrieval: RetrievalResult = update["retrieval"]
        failure = update.get("retrieval_failure")
        if failure:
            return f"status={retrieval.status} -> REFUSED: {one_line(failure, 55)}"
        return (
            f"status={retrieval.status} metrics={len(retrieval.selected_metrics)} "
            f"dims={len(retrieval.selected_dimensions)} join_safe={retrieval.is_safe_for_direct_execution}"
        )

    if node == "generate":
        if update.get("generation_error"):
            return f"LLM ERROR: {one_line(update['generation_error'], 60)}"
        sql = (update.get("sql_candidate") or "").strip().replace("\n", " ")
        return f"LLM SQL: {one_line(sql, 72)}"

    if node in ("validate", "validate_sql"):
        result = update.get("validation_result")
        if result is None:
            return "no validation result"
        if result.is_valid:
            limit = f" LIMIT={result.applied_limit}" if result.applied_limit else ""
            return f"PASS -> sanitized SQL{limit}"
        rules = ", ".join(str(v.rule) for v in result.violations) or "unknown"
        return f"REJECT -> {rules}"

    if node == "execute":
        execution: ExecutionResult = update["execution"]
        if execution.is_success:
            return (
                f"SUCCESS rows={execution.row_count} "
                f"cols={','.join(execution.columns)} ({execution.execution_ms:.1f} ms)"
            )
        return (
            f"FAILED status={execution.status} code={execution.error_code} "
            f"repairable={execution.is_repairable}"
        )

    if node == "repair":
        history = update.get("repair_history", [])
        step = history[-1] if history else None
        guardrail = update.get("guardrail_repairs", 0)
        execution = update.get("execution_repairs", 0)
        if step is None:
            return "feedback appended"
        if step.stage == "execution":
            return (
                f"stage=execution attempt {execution} -> SQLSTATE {step.error_code} "
                "fed back to LLM"
            )
        return f"stage=guardrail attempt {guardrail} -> violations fed back to LLM"

    if node == "finalize":
        response = update["response"]
        if response.success:
            return f"success=True repair_attempts={response.repair_attempts}"
        return (
            f"success=False stage={response.failed_stage} "
            f"repair_attempts={response.repair_attempts}"
        )

    return "updated"


def print_scenario_header(index: int, total: int, scenario: Scenario) -> None:
    """Print the scenario banner."""
    print()
    print("=" * WIDTH)
    print(f" SCENARIO {index}/{total}: {scenario.title}")
    print("=" * WIDTH)
    print(f" {scenario.description}")
    print(f" Question : {QUESTION}")
    print("-" * WIDTH)


def print_result(state: dict[str, Any]) -> None:
    """Print the final AnalyzeResponse summary."""
    response = state.get("response")
    if response is None:
        print(" RESULT   : <no response produced>")
        return

    status = "SUCCESS" if response.success else "FAILED"
    print("-" * WIDTH)
    print(f" RESULT   : {status}")
    if response.failed_stage:
        print(f" Stage    : {response.failed_stage}")
        print(f" Reason   : {one_line(response.failure_reason or '', WIDTH - 16)}")
    print(f" Repairs  : {response.repair_attempts}")
    print(f" Model    : {response.model_used}")
    if response.final_sql:
        print(" Final SQL:")
        for line in response.final_sql.strip().splitlines():
            print(f"   {line}")
    if response.execution and response.execution.is_success:
        execution = response.execution
        print(f" Rows     : {execution.row_count} (limit={execution.limit_applied})")
        print(f" Columns  : {', '.join(execution.columns)}")
    if response.warnings:
        print(" Warnings :")
        for warning in response.warnings:
            print(f"   - {one_line(warning, WIDTH - 6)}")


async def run_scenario(index: int, total: int, scenario: Scenario) -> None:
    """Stream one scenario through the compiled graph and print each node."""
    nodes = AnalyticsNodes(
        retriever=StaticRetriever(scenario.retrieval),
        llm_client=scenario.llm,
        executor=scenario.executor,
        max_guardrail_repairs=scenario.max_guardrail_repairs,
        max_execution_repairs=scenario.max_execution_repairs,
        default_model="demo-model",
    )
    graph = build_analytics_graph(nodes)

    initial_state: dict[str, Any] = {
        "question": QUESTION,
        "model": None,
        "temperature": 0.0,
        "warnings": [],
    }
    state: dict[str, Any] = dict(initial_state)

    print_scenario_header(index, total, scenario)
    step = 0
    previous = time.perf_counter()
    started = previous

    async for chunk in graph.astream(
        initial_state,
        stream_mode="updates",
        config={"recursion_limit": 50},
    ):
        now = time.perf_counter()
        elapsed_ms = (now - previous) * 1000
        previous = now
        for node_name, update in chunk.items():
            step += 1
            state.update(update)
            summary = summarize_update(node_name, update)
            print(f"  [{step:02d}] {node_name:<11} | {summary}  ({elapsed_ms:5.1f} ms)")

    total_ms = (time.perf_counter() - started) * 1000
    print_result(state)
    print(f" Total graph time: {total_ms:.1f} ms")


def print_mermaid() -> None:
    """Print the compiled graph topology as a Mermaid diagram."""
    nodes = AnalyticsNodes(
        retriever=StaticRetriever(make_retrieval()),
        llm_client=MockLLMClient(responses=[fenced(VALID_SQL)]),
        executor=DemoExecutor(),
    )
    graph = build_analytics_graph(nodes)
    print(graph.get_graph().draw_mermaid())


def main() -> None:
    """Parse CLI arguments and run the selected demo scenarios."""
    scenarios = build_scenarios()

    parser = argparse.ArgumentParser(
        description="Visual LangGraph pipeline demo (offline, no database required)."
    )
    parser.add_argument(
        "--scenario",
        choices=[*scenarios.keys(), "all"],
        default="all",
        help="Scenario to run (default: all).",
    )
    parser.add_argument("--list", action="store_true", help="List scenarios and exit.")
    parser.add_argument(
        "--mermaid", action="store_true", help="Print the graph topology as Mermaid and exit."
    )
    args = parser.parse_args()

    if args.list:
        for key, scenario in scenarios.items():
            print(f"{key:<20} {scenario.title}")
        return

    if args.mermaid:
        print_mermaid()
        return

    selected = list(scenarios.values()) if args.scenario == "all" else [scenarios[args.scenario]]
    print()
    print("#" * WIDTH)
    print("# LangGraph analytics pipeline - node-by-node execution trace")
    print("# Nodes: retrieve -> generate -> validate -> execute -> finalize")
    print("#        repair (guardrail or SQLSTATE) always loops back to generate")
    print("#" * WIDTH)

    for index, scenario in enumerate(selected, start=1):
        asyncio.run(run_scenario(index, len(selected), scenario))

    print()
    print("=" * WIDTH)
    print(" Done. Tip: --scenario <name> runs one scenario, --mermaid prints the topology.")
    print("=" * WIDTH)


if __name__ == "__main__":
    main()
