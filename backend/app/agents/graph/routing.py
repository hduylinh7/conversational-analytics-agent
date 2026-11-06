"""Conditional edge routing for the analytics LangGraph workflow.

Routing functions are pure: they inspect the current state and return the name
of the next node. All retry budgets and loop-exit decisions live here so the
node implementations stay focused on a single responsibility.
"""

from app.agents.graph.state import AnalyticsState

NODE_RETRIEVE = "retrieve"
NODE_GENERATE = "generate"
NODE_VALIDATE = "validate"
NODE_EXECUTE = "execute"
NODE_REPAIR = "repair"
NODE_FINALIZE = "finalize"


def route_after_retrieve(state: AnalyticsState) -> str:
    """Skip generation when retrieval found nothing actionable or the join plan is unsafe."""
    if state.get("retrieval_failure"):
        return NODE_FINALIZE
    return NODE_GENERATE


def route_after_generate(state: AnalyticsState) -> str:
    """Stop immediately on an LLM provider error; otherwise validate the candidate."""
    if state.get("generation_error"):
        return NODE_FINALIZE
    return NODE_VALIDATE


def route_after_validate(
    state: AnalyticsState,
    *,
    max_guardrail_repairs: int,
) -> str:
    """Execute valid SQL, repair guardrail violations, or give up when out of budget."""
    validation = state.get("validation_result")
    if validation is None:
        return NODE_FINALIZE
    if validation.is_valid:
        return NODE_EXECUTE
    if state.get("guardrail_repairs", 0) < max_guardrail_repairs:
        return NODE_REPAIR
    return NODE_FINALIZE


def route_after_execute(
    state: AnalyticsState,
    *,
    max_execution_repairs: int,
) -> str:
    """Repair only repairable execution failures that still have budget left."""
    execution = state.get("execution")
    if execution is None:
        return NODE_FINALIZE
    if execution.is_success or not execution.is_repairable:
        return NODE_FINALIZE
    if state.get("execution_repairs", 0) < max_execution_repairs:
        return NODE_REPAIR
    return NODE_FINALIZE
