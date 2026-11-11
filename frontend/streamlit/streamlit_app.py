"""Streamlit demo UI for the Conversational Analytics Agent.

Run from the repository root (backend must be reachable at the configured URL):

    streamlit run frontend/streamlit/streamlit_app.py

Configuration (environment variables):
    STREAMLIT_BACKEND_URL     Base URL of the FastAPI backend (default http://localhost:8000).
    STREAMLIT_TIMEOUT_SECONDS HTTP timeout per request in seconds (default 120).
    STREAMLIT_LOG_LEVEL       Demo-UI log level (default INFO).

The UI talks exclusively to the existing FastAPI ``POST /analyze`` endpoint; no
agent logic (retrieval, SQL generation, guardrails, execution, repair) lives
here. Because the API does not expose intermediate node events, the workflow
stage list shown per answer is derived from response evidence only.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from frontend.streamlit import applogic  # noqa: E402 (repo root added above)
from frontend.streamlit.analytics_client import (  # noqa: E402
    AnalyticsAPIError,
    AnalyticsClient,
    AnalyticsClientError,
    PipelineError,
    backend_url,
)

logging.basicConfig(level=os.getenv("STREAMLIT_LOG_LEVEL", "INFO"))
logger = logging.getLogger("frontend.streamlit")

st.set_page_config(
    page_title="Conversational Analytics Demo",
    page_icon="💬",
    layout="wide",
)

STAGES_DONE = "✅"
STAGES_FAILED = "❌"
STAGES_SKIPPED = "⬜"

SQL_LANGUAGE = "sql"

# Bumped whenever the demo UI changes shape; rendered in the sidebar so a
# stale Streamlit process (import caches survive only until restart) is
# obvious at a glance.
UI_VERSION = "v2.1-cache-indicator"

# Map LangGraph node names (from SSE ``node`` events) to demo-friendly labels.
NODE_LABELS = {
    "retrieve": "Semantic Retrieval",
    "generate": "SQL Generation",
    "validate": "SQL Validation",
    "execute": "SQL Execution",
    "repair": "SQL Repair",
    "finalize": "Finalization",
}


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def init_session_state() -> None:
    """Initialize volatile UI state, seeded from environment variables."""
    defaults: dict[str, object] = {
        "messages": [],
        "conversation_id": None,
        "backend_url": backend_url(),
        "temperature": 0.0,
        "model_override": "",
        "use_stream": True,
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def reset_conversation() -> None:
    """Forget the checkpoint thread so the next question starts a new chat."""
    st.session_state["conversation_id"] = None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


def check_backend(client: AnalyticsClient) -> bool:
    """Best-effort liveness probe, shown as a sidebar badge (never fatal)."""
    try:
        client.health()
        return True
    except AnalyticsClientError:
        return False


def render_sidebar() -> AnalyticsClient:
    """Render configuration controls and return a client built from them."""
    with st.sidebar:
        st.header("⚙️ Demo settings")
        st.caption(f"Demo UI {UI_VERSION}")

        url = st.text_input(
            "Backend URL",
            value=st.session_state["backend_url"],
            help="FastAPI service exposing POST /analyze (STREAMLIT_BACKEND_URL).",
        )
        # Only rebuild the client when the URL actually changed; Streamlit
        # re-runs this script on every interaction.
        st.session_state["backend_url"] = url
        client = AnalyticsClient(base_url=url)

        if check_backend(client):
            st.success("Backend reachable", icon="🟢")
        else:
            st.warning(
                f"Backend not reachable at {url} — start it with "
                "`uvicorn app.main:app --app-dir backend`.",
                icon="🟠",
            )

        st.divider()
        st.session_state["temperature"] = st.slider(
            "Generation temperature",
            min_value=0.0,
            max_value=1.0,
            value=float(st.session_state["temperature"]),
            step=0.1,
            help="Sampling temperature forwarded to POST /analyze.",
        )
        st.session_state["model_override"] = st.text_input(
            "Model override (optional)",
            value=st.session_state["model_override"],
            placeholder="e.g. google/gemini-2.5-flash",
            help="Leave empty to use the backend default model.",
        )
        st.toggle(
            "Realtime workflow progress (SSE)",
            value=True,
            key="use_stream",
            help=(
                "Watch each workflow node complete live via POST /analyze/stream. "
                "Turn off to fall back to the blocking POST /analyze endpoint."
            ),
        )

        st.divider()
        st.subheader("🧵 Conversation")
        conversation_id = st.session_state["conversation_id"]
        if conversation_id:
            st.caption("Checkpoint id (enables multi-turn follow-ups)")
            st.code(conversation_id)
            if st.button("🗑️ Start new conversation", use_container_width=True):
                reset_conversation()
                st.rerun()
        else:
            st.caption("No active conversation — the next question starts one.")

        st.divider()
        st.caption(
            "Demo UI only. All analytics logic runs behind the FastAPI "
            "backend; conversation memory comes from the checkpointer on that "
            "side."
        )
    return client


# ---------------------------------------------------------------------------
# Assistant message rendering
# ---------------------------------------------------------------------------


def render_success_banner(payload: dict) -> None:
    """Short confirmation above the detailed sections."""
    text = "✅ Query executed successfully"
    if payload.get("model_used"):
        text += f" — model: `{payload['model_used']}`"
    if applogic.cache_is_hit(payload):
        text += " — ⚡ served from Redis cache"
    st.success(text)
    st.caption(f"Cache: {applogic.cache_label(payload)}")
    if payload.get("conversation_id"):
        st.caption(
            f"Conversation id `{payload['conversation_id']}` — follow-up questions can reference this result."
        )


def render_pipeline_status(payload: dict) -> None:
    """Collapsible workflow summary derived from the final response."""
    success = bool(payload.get("success"))
    failed_stage = payload.get("failed_stage")
    title = (
        "Workflow completed"
        if success
        else f"Workflow stopped at stage: {failed_stage}"
    )
    with st.status(title, state="complete" if success else "error", expanded=not success):
        for stage in applogic.pipeline_stages(payload):
            icon = {"done": STAGES_DONE, "failed": STAGES_FAILED, "skipped": STAGES_SKIPPED}[
                stage.status
            ]
            st.markdown(f"{icon} **{stage.name}** — {stage.detail or stage.status}".rstrip(" —"))

    warnings = payload.get("warnings") or []
    if warnings:
        st.info("**Warnings**\n\n" + "\n".join(f"- {w}" for w in warnings))


def render_sql(payload: dict) -> None:
    """The SQL that was actually executed, plus the raw candidate when it differs."""
    final_sql = payload.get("final_sql") or ""
    raw_sql = payload.get("raw_sql") or ""
    shown = final_sql or raw_sql
    if not shown:
        return

    with st.expander("🧾 Generated SQL", icon="🧾", expanded=False):
        st.code(shown, language=SQL_LANGUAGE)
        if final_sql and raw_sql and raw_sql != final_sql:
            st.caption("Sanitized SQL (executed)")
        if raw_sql and raw_sql != final_sql:
            with st.expander("Raw LLM output (before sanitization)"):
                st.code(raw_sql, language=SQL_LANGUAGE)


def render_chart_section(df: pd.DataFrame, key_prefix: str) -> None:
    """Interactive chart controls for chartable results.

    The section is only offered when the DataFrame has at least one dimension
    (text/date/category) and one metric column — unsuitable datasets are left
    as tables on purpose.
    """
    if not applogic.is_chartable(df):
        return

    dimensions = applogic.dimension_columns(df)
    metrics = applogic.metric_columns(df)
    temporal = applogic.temporal_names(df)

    with st.expander("📊 Chart the results", icon="📊", expanded=True):
        columns = st.columns(3)
        with columns[0]:
            chart_type = st.selectbox(
                "Chart type", ["bar", "line", "pie"], key=f"{key_prefix}-type"
            )
        with columns[1]:
            dimension = st.selectbox(
                "Dimension (X)",
                dimensions,
                index=dimensions.index(temporal[0]) if temporal else 0,
                key=f"{key_prefix}-x",
            )
        with columns[2]:
            metric = st.selectbox("Metric (Y)", metrics, key=f"{key_prefix}-y")

        chart_frame = applogic.prepare_chart_frame(df, dimension, metric)
        if chart_frame.empty:
            st.info("No plottable values for the selected columns.")
            return

        if len(chart_frame) < len(df):
            st.caption(
                f"Values summed per unique `{dimension}` ({len(chart_frame)} unique values)."
            )
        if chart_type == "pie" and len(chart_frame) > applogic.MAX_PIE_SLICES:
            top = chart_frame.head(applogic.MAX_PIE_SLICES - 1)
            other = chart_frame.iloc[applogic.MAX_PIE_SLICES - 1 :]["metric"].sum()
            chart_frame = pd.concat(
                [top, pd.DataFrame([{"dimension": "Other", "metric": other}])],
                ignore_index=True,
            )
            st.caption(f"Showing the top {applogic.MAX_PIE_SLICES - 1} values plus 'Other'.")

        if chart_type == "bar":
            chart = alt.Chart(chart_frame).mark_bar().encode(
                x=alt.X("dimension:N", title=dimension, sort="-y"),
                y=alt.Y("metric:Q", title=metric),
                tooltip=["dimension", "metric"],
            )
        elif chart_type == "pie":
            chart = alt.Chart(chart_frame).mark_arc().encode(
                color=alt.Color("dimension:N", title=dimension),
                theta=alt.Theta("metric:Q"),
                tooltip=["dimension", "metric"],
            )
        else:
            # Line charts read best in the natural (chronological/alphabetic)
            # order of the dimension, rather than by metric value.
            if temporal and dimension == temporal[0]:
                chart_frame = chart_frame.sort_values("dimension")
                x_type = "temporal"
            else:
                chart_frame = chart_frame.sort_values("dimension")
                x_type = "nominal"
            chart = alt.Chart(chart_frame).mark_line(point=True).encode(
                x=alt.X("dimension", title=dimension, type=x_type),
                y=alt.Y("metric:Q", title=metric),
                tooltip=["dimension", "metric"],
            )
        st.altair_chart(chart, use_container_width=True)


def render_results(payload: dict, key_prefix: str) -> None:
    """Result table, row counts, and optional chart section."""
    execution = payload.get("execution")
    if execution is None:
        st.info("The pipeline finished without reaching SQL execution, so there are no results.")
        return

    df: pd.DataFrame | None = None
    try:
        df = applogic.build_dataframe(execution)
    except Exception:
        logger.exception("Failed to build result DataFrame for the UI")
        st.warning("Could not render the results table for this query.")
        return

    if df.empty:
        st.info("The query executed successfully but returned **0 rows**.")
        st.caption("This may mean the filters matched nothing in the dataset.")
    else:
        st.dataframe(df, use_container_width=True, hide_index=True)

    caption = applogic.rows_caption(execution)
    if execution.get("truncated"):
        caption += " — ⚠️ result truncated (hard row cap)"
    st.caption(caption)

    if not df.empty:
        render_chart_section(df, key_prefix)


def render_execution_details(payload: dict) -> None:
    """Metadata expander sourced from the AnalyzeResponse contract."""
    with st.expander("🔬 Execution details", icon="🔬"):
        execution = payload.get("execution") or None

        metric_row_1 = st.columns(5)
        metric_row_1[0].metric(
            "Execution status",
            execution.get("status") if execution else "not reached",
        )
        metric_row_1[1].metric(
            "Rows returned",
            execution.get("row_count") if execution else "—",
        )
        metric_row_1[2].metric(
            "Duration",
            applogic.format_duration(execution.get("execution_ms") or 0.0) if execution else "—",
        )
        metric_row_1[3].metric("Repair attempts", payload.get("repair_attempts", 0))
        metric_row_1[4].metric("Cache", applogic.cache_label(payload))

        metric_row_2 = st.columns(4)
        metric_row_2[0].metric("Model", payload.get("model_used") or "—")
        metric_row_2[1].metric("Retrieval status", payload.get("retrieval_status", "—"))
        metric_row_2[2].metric("Retrieval confidence", _confidence_text(payload))
        metric_row_2[3].metric(
            "Row limit enforced",
            (execution or {}).get("limit_applied") or "—",
        )

        if execution and (execution.get("error_code") or execution.get("error_message")):
            st.error(
                f"Execution error `{execution.get('error_code')}`: {execution.get('error_message')}"
            )
        if execution and execution.get("truncated"):
            st.caption(
                "Result truncated to the execution row cap "
                f"({execution.get('row_count')} of {execution.get('total_rows_fetched')} rows fetched)."
            )


def _confidence_text(payload: dict) -> str:
    try:
        return f"{float(payload.get('confidence_score') or 0):.2f}"
    except (TypeError, ValueError):
        return "—"


def render_assistant_result(payload: dict, key_prefix: str) -> None:
    """Render one full assistant answer from an /analyze response payload."""
    success = bool(payload.get("success"))
    if not success:
        failed_stage = payload.get("failed_stage") or "unknown"
        failure_reason = payload.get("failure_reason") or "No reason provided."
        st.error(f"❌ The request did not complete (stage: **{failed_stage}**): {failure_reason}")
    else:
        render_success_banner(payload)

    render_pipeline_status(payload)
    render_sql(payload)
    render_results(payload, key_prefix)
    render_execution_details(payload)


# ---------------------------------------------------------------------------
# Chat driver
# ---------------------------------------------------------------------------


def ask_backend(question: str, client: AnalyticsClient) -> dict:
    """Send one question to POST /analyze and return the payload dict."""
    started = time.perf_counter()
    payload = client.analyze(
        question,
        conversation_id=st.session_state["conversation_id"],
        model=st.session_state["model_override"] or None,
        temperature=float(st.session_state["temperature"]),
    )
    elapsed = time.perf_counter() - started
    logger.info(
        "Question answered in %.2fs (success=%s, stage=%s)",
        elapsed,
        payload.get("success"),
        payload.get("failed_stage"),
    )

    conversation_id = payload.get("conversation_id")
    if conversation_id:
        st.session_state["conversation_id"] = conversation_id
    return payload


def ask_backend_stream(question: str, client: AnalyticsClient) -> dict:
    """Send one question to POST /analyze/stream while showing live progress.

    Renders an ``st.status`` panel that ticks off each LangGraph node as the
    backend reports it over SSE — the stages shown are the actual events the
    pipeline emitted, not a simulation. Returns the final AnalyzeResponse
    payload. Raises the same connection/protocol exceptions as
    :meth:`AnalyticsClient.analyze`, plus :class:`PipelineError` when the
    pipeline itself reports failure through the stream's ``error`` event.
    """
    started = time.perf_counter()
    stage_started = started
    payload: dict[str, Any] | None = None
    failure_reason: str | None = None

    with st.status("Workflow running…", state="running", expanded=True) as status_box:
        body = status_box.container()
        body.markdown("⏳ Workflow started")

        try:
            for event, data in client.stream_analyze(
                question,
                conversation_id=st.session_state["conversation_id"],
                model=st.session_state["model_override"] or None,
                temperature=float(st.session_state["temperature"]),
            ):
                if event == "start":
                    conversation_id = data.get("conversation_id")
                    if conversation_id:
                        # Adopt the thread id up front so even a turn that
                        # fails mid-pipeline continues the same conversation.
                        st.session_state["conversation_id"] = conversation_id
                elif event == "node":
                    node_name = str(data.get("node") or "unknown")
                    elapsed = time.perf_counter() - stage_started
                    stage_started = time.perf_counter()
                    label = NODE_LABELS.get(node_name, node_name)
                    body.markdown(f"{STAGES_DONE} **{label}** completed ({elapsed:.1f}s)")
                elif event == "result":
                    payload = data.get("response")
                    if not isinstance(payload, dict):
                        raise AnalyticsAPIError(
                            "The streaming result event was malformed."
                        )
                elif event == "error":
                    failure_reason = str(data.get("reason") or "Unknown pipeline failure.")
                elif event == "done":
                    break
                else:
                    logger.debug("Ignoring unknown SSE event: %s", event)
        finally:
            elapsed_total = time.perf_counter() - started
            # Collapse the live panel: the full detail follows below anyway.
            status_box.update(
                label=f"Workflow finished in {elapsed_total:.1f}s",
                state="error" if failure_reason else "complete",
                expanded=False,
            )

        if failure_reason:
            raise PipelineError(failure_reason)
        if payload is None:
            raise AnalyticsAPIError(
                "The analytics stream ended without a result."
            )

    logger.info(
        "Question answered via stream in %.2fs (success=%s, stage=%s)",
        time.perf_counter() - started,
        payload.get("success"),
        payload.get("failed_stage"),
    )
    return payload


def render_history() -> None:
    """Re-render stored chat messages (assistant results stay interactive)."""
    for index, message in enumerate(st.session_state["messages"]):
        role = message["role"]
        with st.chat_message(role):
            if message.get("kind") == "result":
                render_assistant_result(message["payload"], key_prefix=f"history-{index}")
            elif message.get("kind") == "error":
                st.error(message["content"])
            else:
                st.markdown(message["content"])


def handle_new_question(question: str, client: AnalyticsClient) -> None:
    """Run one turn: append the user bubble, call the API, append the answer."""
    st.session_state["messages"].append(
        {"role": "user", "kind": "text", "content": question}
    )
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        if bool(st.session_state.get("use_stream", True)):
            spinner_text = "Streaming workflow progress…"
        else:
            spinner_text = "Running retrieval → SQL generation → validation → execution…"
        with st.spinner(spinner_text):
            try:
                if bool(st.session_state.get("use_stream", True)):
                    payload = ask_backend_stream(question, client)
                else:
                    payload = ask_backend(question, client)
            except PipelineError as exc:
                logger.exception("Analytics pipeline reported failure: %s", exc)
                st.session_state["messages"].append(
                    {
                        "role": "assistant",
                        "kind": "error",
                        "content": (
                            "🛑 **The analytics pipeline reported a failure.**\n\n"
                            + str(exc)
                        ),
                    }
                )
                st.error("🛑 The analytics pipeline reported a failure. See the message below.")
                return
            except AnalyticsAPIError as exc:
                logger.exception("Analyze API error for question: %s", question)
                st.session_state["messages"].append(
                    {
                        "role": "assistant",
                        "kind": "error",
                        "content": (
                            "⚠️ **The analytics API rejected the request.**\n\n"
                            f"{exc}"
                        ),
                    }
                )
                st.error("⚠️ The analytics API rejected the request. See the message below.")
                return
            except AnalyticsClientError as exc:
                logger.exception("Backend unavailable for question: %s", question)
                st.session_state["messages"].append(
                    {
                        "role": "assistant",
                        "kind": "error",
                        "content": (
                            "🚫 **Could not reach the analytics backend.**\n\n"
                            + str(exc)
                            + "\n\nCheck that the FastAPI service is running "
                            "(`uvicorn app.main:app --app-dir backend`) and that the "
                            "URL in the sidebar is correct."
                        ),
                    }
                )
                st.error("🚫 Could not reach the analytics backend. See the message below.")
                return
            except Exception:
                logger.exception("Unexpected error for question: %s", question)
                st.session_state["messages"].append(
                    {
                        "role": "assistant",
                        "kind": "error",
                        "content": (
                            "⚠️ **Something went wrong while running this question.** "
                            "Details were logged; please try again."
                        ),
                    }
                )
                st.error("⚠️ Something went wrong. Details were logged; see the message below.")
                return

        key_prefix = f"history-{len(st.session_state['messages'])}"
        st.session_state["messages"].append(
            {
                "role": "assistant",
                "kind": "result",
                "payload": payload,
            }
        )
        render_assistant_result(payload, key_prefix)


def main() -> None:
    """Streamlit entry point."""
    init_session_state()
    client = render_sidebar()

    st.title("💬 Conversational Analytics Agent")
    st.caption(
        "Ask analytical questions about the data in natural language. The agent retrieves "
        "schema context, generates SQL with self-repair, validates it with guardrails, and "
        "executes it on PostgreSQL."
    )

    render_history()

    question = st.chat_input("Ask a question, e.g. 'Which city placed the most orders?'")
    if question:
        handle_new_question(question.strip(), client)


main()
