# LangGraph Orchestration Architecture

The **LangGraph orchestration layer** ([`backend/app/agents/graph/`](file:///d:/conversational-analytics-agent/backend/app/agents/graph/)) is the control plane of the analytics pipeline. It replaces the hand-written retry loops with an explicit state machine that coordinates semantic retrieval, Text-to-SQL generation, AST guardrail validation, PostgreSQL execution, and both repair loops (guardrail + SQLSTATE).

LangGraph is used for control flow only. All business logic — retrieval ranking, prompt rendering, AST validation, SQL execution — is reused from the modules built in earlier phases.

---

## 1. Graph Topology

```text
START
  │
  ▼
┌──────────┐
│ retrieve │  SemanticRetriever.retrieve(question)
└────┬─────┘
     │
     ├── retrieval failure / unsafe join ──────────────► finalize (refusal, no LLM call)
     │
     ▼
┌──────────┐   LLMError    ┌──────────┐
│ generate │──────────────►│ finalize │
└────┬─────┘               └──────────┘
     │
     ▼
┌──────────┐  invalid + budget  ┌────────┐
│ validate │───────────────────►│ repair │──┐
└────┬─────┘                    └────────┘  │
     │ valid                                 │
     ▼                                       │
┌──────────┐  repairable + budget  ┌────────┐│
│ execute  │──────────────────────►│ repair ││
└────┬─────┘                       └────────┘│
     │                                       │
     ├── success / non-repairable / exhausted│
     ▼                                       │
┌──────────┐                                 │
│ finalize │◄────────────────────────────────┘
└────┬─────┘
     ▼
    END
```

The `repair` node always routes back to `generate`. SQL that failed is never re-executed directly: the LLM must produce a corrected statement, and that statement must pass `validate` again before `execute` can run.

---

## 2. Nodes

| Node | Responsibility | Reuses |
|---|---|---|
| `retrieve` | Select semantic metrics/dimensions; gate unsafe or non-analytics questions; build the initial chat prompt | `SemanticRetriever`, `retrieval_failure_reason`, `build_initial_messages` |
| `generate` | Call the LLM, extract the SQL candidate, append the assistant turn to the shared conversation | `BaseLLMClient.generate_async`, `extract_sql` |
| `validate` | Run AST guardrails on the candidate | `SQLGuardrailValidator` / `validate_sql` |
| `execute` | Execute sanitized SQL with timeout and row caps | `AnalyticsRepository.execute` |
| `repair` | Append guardrail-violation or SQLSTATE feedback as the next user turn; consume the matching budget | `render_repair_feedback_prompt`, `render_execution_repair_feedback_prompt` |
| `finalize` | Normalize state into the public `AnalyzeResponse` | Response mapping helpers |

Routing functions in `routing.py` are pure state predicates:

| Router | Decision |
|---|---|
| `route_after_retrieve` | `finalize` when retrieval found no metrics/dimensions or the join plan is unsafe, otherwise `generate` |
| `route_after_generate` | `finalize` on an LLM provider error, otherwise `validate` |
| `route_after_validate` | `execute` when valid; `repair` while guardrail budget remains; otherwise `finalize` |
| `route_after_execute` | `finalize` on success/non-repairable/exhausted; `repair` for repairable failures with budget left |

---

## 3. State

`AnalyticsState` (`state.py`) is a `TypedDict` carrying every artifact produced along the way:

| Group | Fields |
|---|---|
| Input | `question`, `model`, `temperature` |
| Retrieval | `retrieval`, `retrieval_failure` |
| Generation | `messages`, `llm_response`, `sql_candidate`, `model_used`, `generation_error` |
| Validation / execution | `validation_result`, `execution` |
| Repair accounting | `repair_history`, `guardrail_repairs`, `execution_repairs`, `last_failure` |
| Finalization | `failure_stage`, `failure_reason`, `warnings`, `response` |

Both repair loops share the same `messages` conversation, so the LLM always sees its earlier attempts and the feedback it received.

---

## 4. Repair Semantics

| Scenario | Behaviour |
|---|---|
| Guardrail violation | `repair` appends the violation list; budget `SQL_MAX_REPAIR_ATTEMPTS` |
| Repairable execution error (`DATABASE_ERROR`, `TIMEOUT`, `VALIDATION_FAILED`) | `repair` appends the SQLSTATE + guidance; budget `SQL_MAX_EXECUTION_REPAIR_ATTEMPTS` |
| Non-repairable execution error (`CANCELLED`) | Immediate `finalize` with `failed_stage="execution"` |
| No semantic match / unsafe join | Immediate `finalize` with `failed_stage="retrieval"`, no LLM call |
| LLM provider failure | `finalize` with `failed_stage="generation"` |
| Budgets exhausted | `finalize` with the corresponding failed stage and the accumulated warnings |

`GRAPH_RECURSION_LIMIT` (default `50`) caps total supersteps as a final safety net against runaway loops. `AnalyzeService` converts a recursion-limit breach into a graceful `failed_stage="generation"` response.

---

## 5. Service Integration

`AnalyzeService` compiles the graph once with injectable collaborators:

```python
service = AnalyzeService(
    retriever=SemanticRetriever(),
    llm_client=create_llm_client(),
    validator=SQLGuardrailValidator(),
    repository=AnalyticsRepository(),
    max_guardrail_repairs=settings.SQL_MAX_REPAIR_ATTEMPTS,
    max_execution_repairs=settings.SQL_MAX_EXECUTION_REPAIR_ATTEMPTS,
)
response = await service.analyze(question, model=model, temperature=temperature)
```

The HTTP contract (`POST /analyze` → `AnalyzeResponse`) is unchanged. Tests inject fake retrievers, LLM clients, validators, and repositories directly into the graph.

---

## 6. Visual Demo (CMD)

`scripts/demo_langgraph.py` runs the compiled graph offline with deterministic fixtures and prints each node transition, so the flow can be inspected without PostgreSQL or an API key:

```bash
python scripts/demo_langgraph.py                      # all scenarios
python scripts/demo_langgraph.py --scenario happy
python scripts/demo_langgraph.py --scenario guardrail-repair
python scripts/demo_langgraph.py --scenario execution-repair
python scripts/demo_langgraph.py --scenario combined-repair   # guardrail + SQLSTATE in one run
python scripts/demo_langgraph.py --scenario non-repairable    # CANCELLED stops immediately
python scripts/demo_langgraph.py --scenario execution-repair --verbose
python scripts/demo_langgraph.py --strict              # exit 1 on unexpected outcome
python scripts/demo_langgraph.py --mermaid             # graph topology
```

Each line shows the node name, the relevant state update (retrieval status, SQL candidate, guardrail decision, SQLSTATE, repair feedback, final result) and the node latency. `--verbose` expands the SQL candidate, individual guardrail violations, database error messages, and the first lines of each repair prompt. `--strict` compares every scenario with its declared expected outcome and returns a non-zero exit code on a mismatch, which makes the demo usable as a smoke test.

When more than one scenario runs, a summary table reports the observed result, failed stage, repair count, node count, and duration for each scenario.

---

## 7. Checkpointing (Multi-Turn Conversations)

The graph can be compiled with a LangGraph `BaseCheckpointSaver`, which snapshots state after every superstep keyed by `config.configurable.thread_id`:

```python
graph = build_analytics_graph(nodes, checkpointer=checkpointer)
result = await graph.ainvoke(
    initial_state,
    config={"configurable": {"thread_id": thread_id}, "recursion_limit": limit},
)
```

- **Backends** (`CHECKPOINTER_BACKEND` env): `memory` (default, per-process `MemorySaver`) or `redis` (`AsyncRedisSaver`, persistent and shared across replicas; requires `langgraph-checkpoint-redis`). The backend is built once at app startup by `agents.graph.checkpointer.init_checkpointer()` and closed on shutdown.
- **Thread ids**: the API accepts `conversation_id` in `AnalyzeRequest` and echoes it on `AnalyzeResponse.conversation_id`. When omitted and a checkpointer exists, the service synthesizes a fresh UUID thread for each request (caller may pass it back to continue).
- **Multi-turn context**: because state is restored per thread, the `retrieve` node replays up to `MAX_CARRIED_MESSAGES` older `messages` from previous turns and appends the new turn's prompt, so follow-ups like "and group by month?" keep their context. Per-turn artifacts (repair counters, `sql_candidate`, `execution`, ...) are explicitly reset in the request's initial state so stale values from earlier turns can never leak into a new response.
- With no checkpointer attached (unit tests, offline scripts), runs are stateless exactly as before.

## 8. SSE Streaming (`POST /analyze/stream`)

`AnalyzeService.stream_analyze()` drives the graph with `graph.astream(..., stream_mode="updates")` and yields `(event, payload)` tuples; `api/routes/analyze.py` serializes them as Server-Sent Events with keep-alive comments (`services/sse.py:heartbeat_stream`) whenever the pipeline is silent (e.g. during long LLM calls), defeating reverse-proxy timeouts.

| Event | When | `data` |
|---|---|---|
| `start` | before any graph work | `{"conversation_id": ...}` |
| `node` | after each node updates state | `{"node": retrieve\|generate\|validate\|execute\|repair}` |
| `result` | `finalize` produced the response | `{"conversation_id": ..., "response": AnalyzeResponse}` |
| `error` | pipeline failure (incl. recursion limit) | `{"reason": ...}` |
| `done` | always last, before closing the stream | `{}` |

Frames are `text/event-stream` with `Cache-Control: no-cache` and `X-Accel-Buffering: no`; unexpected exceptions inside the stream degrade to an `error` event followed by `done` instead of breaking the connection silently.

---

## 9. Remaining Deferred Work

- **Conversation persistence**: records into `app.conversations` / `app.messages` and audit rows into `app.query_executions` (read-only `GET /conversations` history).
- **Additional nodes**: question interpretation, clarification interrupts, visualization specs, and result summarization.
