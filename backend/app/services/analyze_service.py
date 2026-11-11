"""AnalyzeService: LangGraph-based analytics pipeline orchestration.

The workflow is compiled once from ``AnalyticsNodes`` and executed with a
per-request state. Control flow (retrieval gate, guardrail repair loop,
execution-error repair loop, and termination) lives in the graph edges; this
service only wires dependencies, runs the graph, and maps its output to the
public API response.

Two execution modes are exposed:

- :meth:`analyze` runs the graph to completion and returns the response.
- :meth:`stream_analyze` yields ``(event, payload)`` tuples while the graph
  executes (``graph.astream``), consumed by the ``/analyze/stream`` SSE
  endpoint.

When a checkpointer is attached, runs sharing a ``thread_id`` resume the prior
conversation state, enabling multi-turn follow-up questions.
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Literal, Protocol, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.errors import GraphRecursionError

from app.agents.graph.builder import build_analytics_graph
from app.agents.graph.checkpointer import get_checkpointer
from app.agents.graph.nodes import AnalyticsNodes, RetrieverLike, ValidatorLike
from app.agents.graph.routing import NODE_FINALIZE
from app.agents.graph.state import AnalyticsState
from app.agents.llm_client import BaseLLMClient, create_llm_client
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.redis import get_redis_client
from app.repositories.analytics import AnalyticsRepository
from app.repositories.schemas import ExecutionResult
from app.schemas.analyze import AnalyzeResponse
from app.semantic.retrieval.retriever import SemanticRetriever
from app.services.cache_service import CacheService
from app.services.semantic_cache import SemanticCacheService, create_semantic_cache

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
        checkpointer: BaseCheckpointSaver | None = None,
        cache: CacheService | None = None,
        semantic_cache: SemanticCacheService | None = None,
    ) -> None:
        """Build the graph with injectable components for testing."""
        settings = get_settings()
        self.retriever = retriever or SemanticRetriever()
        self.llm_client = llm_client or create_llm_client()
        self.validator = validator
        self.repository = repository or AnalyticsRepository()
        self.recursion_limit = settings.GRAPH_RECURSION_LIMIT
        # Default to the process-wide checkpointer created during app startup;
        # ``None`` keeps runs stateless (unit tests, offline scripts).
        self.checkpointer = checkpointer if checkpointer is not None else get_checkpointer()
        # Default Redis cache wired to the shared connection pool; explicit
        # ``None`` arguments keep the graph cache-free (tests, offline scripts).
        if cache is None:
            cache = CacheService(get_redis_client())
        self.cache = cache
        # Question-similarity cache (requires OPENAI_API_KEY; ``None`` disables it).
        self.semantic_cache = semantic_cache or create_semantic_cache()

        self.nodes = AnalyticsNodes(
            retriever=self.retriever,
            llm_client=self.llm_client,
            executor=self.repository.execute,
            validator=self.validator,
            max_guardrail_repairs=max_guardrail_repairs,
            max_execution_repairs=max_execution_repairs,
            default_model=settings.LLM_MODEL,
            cache=self.cache,
            semantic_cache=self.semantic_cache,
        )
        self.graph = build_analytics_graph(self.nodes, checkpointer=self.checkpointer)

    # ------------------------------------------------------------------
    # Per-turn graph run setup
    # ------------------------------------------------------------------

    @staticmethod
    def _initial_state(question: str, model: str | None, temperature: float) -> AnalyticsState:
        """Build the per-turn input state.

        Every per-turn artifact is reset explicitly so checkpoint-carried state
        from earlier turns in the same thread cannot leak into this turn's
        response (e.g. a previous turn's ``sql_candidate`` surfacing as
        ``raw_sql`` of a turn that never generated SQL). ``messages`` is
        intentionally omitted: the ``retrieve`` node replays the checkpointed
        conversation history and appends the new turn's prompt.
        """
        return {
            "question": question,
            "model": model,
            "temperature": temperature,
            "warnings": [],
            "retrieval": None,
            "retrieval_failure": None,
            "llm_response": "",
            "sql_candidate": "",
            "generation_error": None,
            "validation_result": None,
            "execution": None,
            "repair_history": [],
            "guardrail_repairs": 0,
            "execution_repairs": 0,
            "last_failure": None,
            "failure_stage": None,
            "failure_reason": None,
            "prompt_cache_key": "",
            "cache_hit": False,
            "semantic_cache_vector": [],
        }

    def _request_config(self, conversation_id: str | None) -> tuple[str | None, RunnableConfig]:
        """Resolve the LangGraph run config and the thread id it checkpoints to.

        Returns ``(thread_id, config)`` where ``thread_id`` is only synthesized
        when a checkpointer is present; without one the graph is stateless and
        thread ids are irrelevant.
        """
        config: RunnableConfig = {"recursion_limit": self.recursion_limit}

        if conversation_id:
            thread_id: str | None = conversation_id
        elif self.checkpointer is not None:
            thread_id = str(uuid.uuid4())
        else:
            thread_id = None

        if thread_id is not None and self.checkpointer is not None:
            config["configurable"] = {"thread_id": thread_id}

        return thread_id, config

    @staticmethod
    def _attach_conversation(response: AnalyzeResponse, thread_id: str | None) -> AnalyzeResponse:
        """Echo the conversation thread id back so clients can follow up."""
        if thread_id is not None and response.conversation_id is None:
            response = response.model_copy(update={"conversation_id": thread_id})
        return response

    # ------------------------------------------------------------------
    # Blocking execution
    # ------------------------------------------------------------------

    async def analyze(
        self,
        question: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        conversation_id: str | None = None,
    ) -> AnalyzeResponse:
        """Run the analytics graph and return the pipeline response."""
        thread_id, config = self._request_config(conversation_id)
        initial_state = self._initial_state(question, model, temperature)

        try:
            final_state = cast(
                AnalyticsState,
                await self.graph.ainvoke(initial_state, config=config),
            )
        except GraphRecursionError:
            logger.error(
                "LangGraph recursion limit (%d) exceeded for question '%s' " "(conversation '%s')",
                self.recursion_limit,
                question,
                thread_id,
            )
            response = AnalyzeResponse(
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
            return self._attach_conversation(response, thread_id)

        raw_response: AnalyzeResponse | None = final_state.get("response")
        if raw_response is None:  # defensive: finalize always sets a response
            raw_response = self.nodes.build_response(final_state)
        return self._attach_conversation(raw_response, thread_id)

    # ------------------------------------------------------------------
    # Streaming execution
    # ------------------------------------------------------------------

    async def stream_analyze(
        self,
        question: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
        conversation_id: str | None = None,
        stream_mode: Literal["updates"] = "updates",
    ) -> AsyncGenerator[tuple[str, dict], None]:
        """Yield ``(event, payload)`` tuples while the graph executes.

        Events:
            - ``start``  -> ``{"conversation_id": ...}`` emitted before any work.
            - ``node``   -> ``{"node": name}`` after each node updates state.
            - ``result`` -> ``{"conversation_id": ..., "response": <dict>}`` once
              the ``finalize`` node produced the public response.
            - ``error``  -> ``{"reason": ...}`` when the run hits the recursion
              limit; the stream ends right after.

        Unexpected exceptions propagate to the caller (the SSE route maps them
        to an ``error`` event followed by ``done``).
        """
        thread_id, config = self._request_config(conversation_id)
        logger.info(
            "Streaming analytics pipeline (conversation '%s', question='%s').",
            thread_id,
            question,
        )
        yield "start", {"conversation_id": thread_id}

        try:
            async for chunk in self.graph.astream(
                self._initial_state(question, model, temperature),
                config=config,
                stream_mode=stream_mode,
            ):
                if not isinstance(chunk, dict):  # defensive against new stream modes
                    continue
                for node_name, node_update in chunk.items():
                    if not isinstance(node_name, str) or node_name.startswith("__"):
                        continue  # skip internal stream entries (e.g. interrupts)

                    if node_name == NODE_FINALIZE:
                        response = (
                            node_update.get("response") if isinstance(node_update, dict) else None
                        )
                        if response is None:  # defensive: finalize always sets it
                            raise RuntimeError("Finalize node finished without a response payload.")
                        yield (
                            "result",
                            {
                                "conversation_id": thread_id,
                                "response": response.model_dump(mode="json"),
                            },
                        )
                        continue

                    logger.debug("Analyze stream node completed: %s", node_name)
                    yield "node", {"node": node_name}
        except GraphRecursionError:
            logger.error(
                "LangGraph recursion limit (%d) exceeded while streaming " "(conversation '%s').",
                self.recursion_limit,
                thread_id,
            )
            yield (
                "error",
                {
                    "reason": (
                        "The analytics workflow exceeded its maximum number of steps "
                        "before producing a result."
                    )
                },
            )


_global_analyze_service: AnalyzeService | None = None


def get_analyze_service() -> AnalyzeService:
    """Return a singleton AnalyzeService instance."""
    global _global_analyze_service
    if _global_analyze_service is None:
        _global_analyze_service = AnalyzeService()
    return _global_analyze_service
