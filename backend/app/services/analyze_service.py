"""AnalyzeService: LangGraph-based analytics pipeline orchestration.

The workflow is compiled once from ``AnalyticsNodes`` and executed with a
per-request state. Control flow (retrieval gate, guardrail repair loop,
execution-error repair loop, and termination) lives in the graph edges; this
service only wires dependencies, invokes the graph, and returns its response.
"""

from typing import Protocol, cast

from langgraph.errors import GraphRecursionError

from app.agents.graph.builder import build_analytics_graph
from app.agents.graph.nodes import AnalyticsNodes, RetrieverLike, ValidatorLike
from app.agents.graph.state import AnalyticsState
from app.agents.llm_client import BaseLLMClient, create_llm_client
from app.core.config import get_settings
from app.core.logging import get_logger
from app.repositories.analytics import AnalyticsRepository
from app.repositories.schemas import ExecutionResult
from app.schemas.analyze import AnalyzeResponse
from app.semantic.retrieval.retriever import SemanticRetriever

logger = get_logger(__name__)


class ExecutorLike(Protocol):
    """Structural type for the SQL execution repository used by the graph."""

    async def execute(self, sql: str, **kwargs: object) -> ExecutionResult:
        """Execute validated SQL and return the execution report."""


class AnalyzeService:
    """Coordinates the compiled LangGraph pipeline for natural language questions."""

    def __init__(
        self,
        retriever: RetrieverLike | None = None,
        llm_client: BaseLLMClient | None = None,
        validator: ValidatorLike | None = None,
        repository: ExecutorLike | None = None,
        max_guardrail_repairs: int | None = None,
        max_execution_repairs: int | None = None,
    ) -> None:
        """Build the graph with injectable components for testing."""
        settings = get_settings()
        self.retriever = retriever or SemanticRetriever()
        self.llm_client = llm_client or create_llm_client()
        self.validator = validator
        self.repository = repository or AnalyticsRepository()
        self.recursion_limit = settings.GRAPH_RECURSION_LIMIT

        self.nodes = AnalyticsNodes(
            retriever=self.retriever,
            llm_client=self.llm_client,
            executor=self.repository.execute,
            validator=self.validator,
            max_guardrail_repairs=max_guardrail_repairs,
            max_execution_repairs=max_execution_repairs,
            default_model=settings.LLM_MODEL,
        )
        self.graph = build_analytics_graph(self.nodes)

    async def analyze(
        self, question: str, *, model: str | None = None, temperature: float = 0.0
    ) -> AnalyzeResponse:
        """Run the analytics graph and return the pipeline response."""
        initial_state: AnalyticsState = {
            "question": question,
            "model": model,
            "temperature": temperature,
            "warnings": [],
        }

        try:
            final_state = cast(
                AnalyticsState,
                await self.graph.ainvoke(
                    initial_state,
                    config={"recursion_limit": self.recursion_limit},
                ),
            )
        except GraphRecursionError:
            logger.error(
                "LangGraph recursion limit (%d) exceeded for question '%s'",
                self.recursion_limit,
                question,
            )
            return AnalyzeResponse(
                question=question,
                success=False,
                failed_stage="generation",
                failure_reason=(
                    "The analytics workflow exceeded its maximum number of steps "
                    "before producing a result."
                ),
                retrieval_status="INSUFFICIENT_METADATA",
                confidence_score=0.0,
            )

        response = final_state.get("response")
        if response is None:  # defensive: finalize always sets a response
            response = self.nodes.build_response(final_state)
        return response


_global_analyze_service: AnalyzeService | None = None


def get_analyze_service() -> AnalyzeService:
    """Return a singleton AnalyzeService instance."""
    global _global_analyze_service
    if _global_analyze_service is None:
        _global_analyze_service = AnalyzeService()
    return _global_analyze_service
