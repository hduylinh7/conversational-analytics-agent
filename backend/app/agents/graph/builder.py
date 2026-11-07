"""StateGraph assembly for the conversational analytics workflow."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.graph.nodes import AnalyticsNodes
from app.agents.graph.routing import (
    NODE_EXECUTE,
    NODE_FINALIZE,
    NODE_GENERATE,
    NODE_REPAIR,
    NODE_RETRIEVE,
    NODE_VALIDATE,
    route_after_execute,
    route_after_generate,
    route_after_retrieve,
    route_after_validate,
)
from app.agents.graph.state import AnalyticsState


def build_analytics_graph(
    nodes: AnalyticsNodes,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """Compile the retrieval -> generation -> validation -> execution workflow.

    Topology:
        START -> retrieve -> (generate -> validate -> execute)* -> finalize -> END
                              ^            |           |
                              +-- repair <-+-----------+   (guardrail or execution feedback)

    Guardrail and execution repairs share the same conversation and converge on
    the single ``repair`` node, which routes back through generation and
    validation before any SQL can be executed again.

    When ``checkpointer`` is provided, graph state snapshots are persisted after
    every superstep, keyed by ``config.configurable.thread_id``. Invocations
    sharing a thread id resume the prior conversation state, enabling multi-turn
    follow-up questions.
    """
    graph = StateGraph(AnalyticsState)

    graph.add_node(NODE_RETRIEVE, nodes.retrieve)
    graph.add_node(NODE_GENERATE, nodes.generate)
    graph.add_node(NODE_VALIDATE, nodes.validate_sql)
    graph.add_node(NODE_EXECUTE, nodes.execute)
    graph.add_node(NODE_REPAIR, nodes.repair)
    graph.add_node(NODE_FINALIZE, nodes.finalize)

    graph.add_edge(START, NODE_RETRIEVE)

    graph.add_conditional_edges(
        NODE_RETRIEVE,
        route_after_retrieve,
        {
            NODE_GENERATE: NODE_GENERATE,
            NODE_FINALIZE: NODE_FINALIZE,
        },
    )
    graph.add_conditional_edges(
        NODE_GENERATE,
        route_after_generate,
        {
            NODE_VALIDATE: NODE_VALIDATE,
            NODE_FINALIZE: NODE_FINALIZE,
        },
    )

    def after_validate(state: AnalyticsState) -> str:
        return route_after_validate(
            state, max_guardrail_repairs=nodes.max_guardrail_repairs
        )

    def after_execute(state: AnalyticsState) -> str:
        return route_after_execute(
            state, max_execution_repairs=nodes.max_execution_repairs
        )

    graph.add_conditional_edges(
        NODE_VALIDATE,
        after_validate,
        {
            NODE_EXECUTE: NODE_EXECUTE,
            NODE_REPAIR: NODE_REPAIR,
            NODE_FINALIZE: NODE_FINALIZE,
        },
    )
    graph.add_conditional_edges(
        NODE_EXECUTE,
        after_execute,
        {
            NODE_REPAIR: NODE_REPAIR,
            NODE_FINALIZE: NODE_FINALIZE,
        },
    )

    graph.add_edge(NODE_REPAIR, NODE_GENERATE)
    graph.add_edge(NODE_FINALIZE, END)

    return graph.compile(checkpointer=checkpointer)
