"""CLI demo of SSE streaming and checkpointer-backed multi-turn conversations.

Runs AnalyzeService offline (mock LLM, fake retriever/executor, MemorySaver
checkpointer) and prints:
  1. the SSE event stream (start -> node -> ... -> result) for each turn, and
  2. how the checkpointer keeps the conversation across follow-up turns.

Usage (from the repository root):
    python scripts/demo_stream_checkpoint.py
"""

import asyncio
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

from app.agents.graph.checkpointer import create_checkpointer  # noqa: E402
from app.agents.llm_client import MockLLMClient  # noqa: E402
from app.repositories.schemas import ExecutionResult, ExecutionStatus  # noqa: E402
from app.security.schemas import ValidationResult  # noqa: E402
from app.semantic.retrieval.schemas import (  # noqa: E402
    RetrievalResult,
    RetrievalStatus,
    SelectedMetric,
)
from app.services.analyze_service import AnalyzeService  # noqa: E402

TOTAL_REVENUE_SQL = "SELECT SUM(p.payment_value) AS revenue FROM business.payments p"
STATE_REVENUE_SQL = """SELECT c.customer_state, SUM(p.payment_value) AS revenue
FROM business.payments p
JOIN business.orders o ON p.order_id = o.order_id
JOIN business.customers c ON o.customer_id = c.customer_id
GROUP BY c.customer_state"""

TURNS = [
    ("what is total revenue?", f"```sql\n{TOTAL_REVENUE_SQL}\n```"),
    ("and group that revenue by customer state?", f"```sql\n{STATE_REVENUE_SQL}\n```"),
]


def make_retrieval() -> RetrievalResult:
    return RetrievalResult(
        question="demo",
        status=RetrievalStatus.SUCCESS,
        confidence_score=0.9,
        selected_metrics=[
            SelectedMetric(
                name="revenue",
                description="Total revenue",
                expression="SUM(p.payment_value)",
                aggregation="SUM",
                source_tables=["business.payments"],
                score=0.9,
            )
        ],
        selected_dimensions=[],
        is_safe_for_direct_execution=True,
    )


class FakeRetriever:
    def retrieve(self, question: str) -> RetrievalResult:
        return make_retrieval()


class FakeRepository:
    async def execute(self, sql: str, **kwargs: object) -> ExecutionResult:
        return ExecutionResult(
            status=ExecutionStatus.SUCCESS,
            sql=sql,
            columns=["revenue"],
            rows=[[103_555.12]],
            row_count=1,
            total_rows_fetched=1,
        )


class AlwaysValidValidator:
    def validate(self, sql: str) -> ValidationResult:
        return ValidationResult(is_valid=True, original_sql=sql, sanitized_sql=sql)


async def main() -> None:
    checkpointer = await create_checkpointer(backend="memory")
    llm = MockLLMClient(responses=[sql for _, sql in TURNS])
    service = AnalyzeService(
        retriever=FakeRetriever(),
        llm_client=llm,
        validator=AlwaysValidValidator(),
        repository=FakeRepository(),
        checkpointer=checkpointer,
    )

    conversation_id: str | None = None
    for index, (question, _) in enumerate(TURNS, start=1):
        print(f"\n{'=' * 74}\nTurn {index}: {question!r}")
        print(f"{'=' * 74}")
        async for event, payload in service.stream_analyze(
            question, conversation_id=conversation_id
        ):
            if event == "node":
                print(f"  event: node     data: {{\"node\": \"{payload['node']}\"}}")
            elif event in ("start", "result"):
                label = "conversation_id" if event == "start" else "response"
                value = payload.get("conversation_id") or "<new thread>"
                print(f"  event: {event:<7} data: {label}={value}")
                if event == "result":
                    conversation_id = payload["conversation_id"]
                    response = payload["response"]
                    print(f"    success={response['success']}  sql={response['final_sql']}")

    if llm.call_history:
        assert len(llm.call_history[-1]) > len(llm.call_history[0])
        print(
            f"\nTurn 2 replayed {len(llm.call_history[0])} message(s) from turn 1 "
            f"to the LLM (conversation_id={conversation_id})."
        )


if __name__ == "__main__":
    asyncio.run(main())
