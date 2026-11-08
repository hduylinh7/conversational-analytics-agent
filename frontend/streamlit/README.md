# Streamlit Demo UI

A lightweight demonstration interface for the Conversational Analytics Agent.
The UI talks exclusively to the existing FastAPI backend (`POST /analyze`,
`GET /health`) — no agent logic (retrieval, Text-to-SQL generation, guardrails,
execution, repair) is duplicated here.

## Features

- **Chat interface** (`st.chat_message` / `st.chat_input`) with a persistent
  conversation stored in `st.session_state` and multi-turn follow-up questions
  via the backend checkpointer (`conversation_id` round-trip).
- **Realtime workflow** (enabled by default, toggle in the sidebar):
  each question is sent to `POST /analyze/stream` and the UI renders an
  `st.status` panel that ticks off the actual LangGraph nodes as the backend
  emits them over SSE — Semantic Retrieval, SQL Generation, SQL Validation,
  SQL Execution, SQL Repair (only when triggered), Finalization — with
  per-stage elapsed time measured from the real events. Turn the toggle off
  to fall back to the blocking `POST /analyze` endpoint, in which case the
  workflow summary is *derived from the final response* only.
- **Generated SQL** in a collapsible section with syntax highlighting, showing
  the sanitized SQL that was executed and, when it differs, the raw LLM output.
- **Query results** in `st.dataframe` with row counts, truncation notices, and
  graceful handling of empty result sets.
- **Charts** (bar / line / pie via Altair, bundled with Streamlit) offered only
  when the result has a suitable dimension + metric column.
- **Execution details**: execution status, duration, rows returned/total,
  repair attempts, model, retrieval status/confidence, row limit, error codes.
- **Error handling** for backend outages, API errors, SQL execution failures,
  invalid payloads, and unexpected exceptions — user-friendly messages with
  details in the logs.

## Setup

```bash
# 1. Install demo dependencies (backend dependencies stay separate)
pip install -r frontend/streamlit/requirements.txt

# 2. Start the backend (repo root), plus PostgreSQL/Redis as usual
#    (`docker compose up -d postgres redis`, `alembic upgrade head`, ...)
uvicorn app.main:app --app-dir backend

# 3. Point the UI at the backend (optional; defaults are used otherwise)
#    STREAMLIT_BACKEND_URL=http://localhost:8000
#    STREAMLIT_TIMEOUT_SECONDS=120
#    STREAMLIT_LOG_LEVEL=INFO

# 4. Run the demo UI (from the repository root)
streamlit run frontend/streamlit/streamlit_app.py
```

Streamlit then opens on http://localhost:8501. The backend URL, timeout, and
log level can also be changed at runtime in the sidebar; **Settings → Backend
URL** points the demo at any deployment.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `STREAMLIT_BACKEND_URL` | `http://localhost:8000` | Base URL of the FastAPI backend |
| `STREAMLIT_TIMEOUT_SECONDS` | `120` | HTTP timeout per request (LLM calls can be slow) |
| `STREAMLIT_LOG_LEVEL` | `INFO` | Demo-UI logging verbosity |

## Files

- `streamlit_app.py` — the demo UI (chat, status, SQL, results, charts, details).
- `analytics_client.py` — HTTP bridge to the FastAPI backend (injectable
  transport for testing).
- `applogic.py` — pure helpers: response → DataFrame, numeric/categorical
  detection, chart framing, derived pipeline-stage observations.

Unit tests live in `tests/streamlit_demo` at the repository root
(`pytest tests/streamlit_demo -v`).
