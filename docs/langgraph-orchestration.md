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
python scripts/demo_langgraph.py --mermaid             # graph topology
```

Each line shows the node name, the relevant state update (retrieval status, SQL candidate, guardrail decision, SQLSTATE, repair feedback, final result) and the node latency.

---

## 7. Deferred to Later PRs

- **Streaming**: `graph.astream()` updates exposed through a new `/analyze/stream` (SSE) endpoint.
- **Checkpointing**: `MemorySaver`/Redis checkpointer keyed by conversation ID for multi-turn follow-ups.
- **Additional nodes**: question interpretation, clarification interrupts, visualization specs, result summarization, and persistence into `app.conversations` / `app.messages`.
