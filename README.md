# Conversational Analytics Agent

A production-oriented conversational AI analytics system that lets analysts query business data in natural language (Vietnamese & English) and get back validated SQL, executed results, and visualizations — powered by a semantic layer, hybrid semantic retrieval, AST-based SQL guardrails, and LangGraph orchestration with self-repair loops.

**Stack**: FastAPI (async Python 3.12) · LangGraph · PostgreSQL 16 · Redis 7 · sqlglot / Streamlit

---

## Features

- **Semantic Layer** — YAML-defined metrics (12), dimensions (10), and data sources (5 tables, 8 relationships) with bilingual (vi/en) synonyms, packed as Pydantic-validated application code.
- **Hybrid Semantic Retrieval** — keyword engine (Vietnamese diacritic stripping, synonyms, stemming, n-grams) combined with optional OpenAI-compatible embeddings; join-path resolution with fan-out / chasm-trap detection.
- **Text-to-SQL Generation** — provider-agnostic LLM client (OpenRouter / Groq / OpenAI / deterministic Mock for offline runs) with schema-qualified, SELECT-only prompting.
- **AST SQL Guardrails** — sqlglot-based validation: statement-type allowlist, mutation/dangerous-function denial, table allowlist from the semantic catalog, LIMIT clamping/injection, and business-intent conformance checks.
- **LangGraph Orchestration** — compiled state machine `retrieve → generate → validate → execute → finalize` with guardrail-repair (default 3 attempts) and execution-repair (default 2 attempts) loops driven by conditional edges and budgets.
- **Multi-turn Conversations** — checkpointer-backed threads (`memory` or `redis`), `conversation_id` in/out on every request.
- **SSE Streaming** — `POST /analyze/stream` emits live graph node transitions (`retrieve`, `generate`, `validate`, `execute`, `repair`) with keep-alive heartbeats.
- **Caching & Protection** — Redis prompt cache (validated LLM output), SQL result cache, embedding-based semantic cache (question→SQL replay, re-validated before execution), token-bucket rate limiting (fail-open), execution timeout and row caps.

---

## Architecture

```text
Streamlit demo UI / Next.js client
        │  HTTP + SSE
        ▼
FastAPI Backend (async Python 3.12)
   ├── Rate limiting (Redis token bucket, fail-open)
   ├── LangGraph state machine
   │     retrieve ─► generate ─► validate ─► execute ─► finalize
   │                     ▲          │(violations)│(repairable)
   │                     └── repair ◄────────────┘
   ├── Semantic layer (YAML: metrics / dimensions / data sources)
   ├── Hybrid retrieval (keyword + optional embeddings)
   ├── SQL guardrails (sqlglot AST + intent checks)
   ├── PostgreSQL 16  (schemas: business, app, semantic)
   └── Redis 7        (checkpointer, prompt/result/semantic caches, rate limit)
```

Deeper documentation:

| Topic | Doc |
|---|---|
| Database schema & ER diagram | [docs/database.md](docs/database.md) · [docs/erd.mmd](docs/erd.mmd) |
| Semantic layer | [docs/semantic-layer.md](docs/semantic-layer.md) |
| Semantic retrieval | [docs/semantic-retrieval.md](docs/semantic-retrieval.md) |
| SQL guardrails & AST validation | [docs/sql-guardrails.md](docs/sql-guardrails.md) |
| LangGraph orchestration | [docs/langgraph-orchestration.md](docs/langgraph-orchestration.md) |
| Data quality & profiling | [docs/data-quality.md](docs/data-quality.md) |
| Streamlit demo UI | [frontend/streamlit/README.md](frontend/streamlit/README.md) |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe |
| `GET` | `/health/ready` | Readiness probe (PostgreSQL/Redis dependencies; 503 when down) |
| `POST` | `/analyze` | Ask a business question; returns SQL, explanation, execution results, diagnostics |
| `POST` | `/analyze/stream` | Same contract, streamed as Server-Sent Events (`start` → `node` → `result` → `done`) |

`AnalyzeRequest`: `question` (3–2000 chars), `conversation_id` (optional — enables multi-turn), `model` (optional override), `temperature` (0–1).

`AnalyzeResponse` highlights: `success`, `failed_stage` (`retrieval` | `generation` | `execution`), `final_sql`, `raw_sql`, `explanation`, `repair_attempts`, `confidence_score`, `model_used`, `warnings`, `cache_hit`, `execution` (columns/rows/row_count/duration).

---

## Quickstart

### 1. Prerequisites

- Python 3.12+
- Docker & Docker Compose
- An LLM API key (OpenRouter, Groq, or OpenAI) — or use `LLM_PROVIDER=mock` for offline runs

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env: set LLM_PROVIDER, the matching API key, POSTGRES_*, REDIS_*, etc.
```

> ⚠️ The Olist CSV dataset is **not committed to git** (gitignored). Download the
> [Olist Brazilian E-Commerce dataset](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
> and place these 5 files in `data/raw/`:
> `olist_customers_dataset.csv`, `olist_orders_dataset.csv`, `olist_order_items_dataset.csv`,
> `olist_products_dataset.csv`, `olist_order_payments_dataset.csv`.

### 3. Start infrastructure

```bash
docker compose up -d postgres redis
```

Defaults: database `analytics`, user `analytics`, password `analytics`, host port `5433` (configurable via `.env`).

### 4. Apply migrations

```bash
alembic upgrade head        # from repo root (or: cd backend && alembic upgrade head)
alembic downgrade base      # to roll back
```

Creates 3 schemas (`business`, `app`, `semantic`) and 13 tables.

### 5. Ingest the Olist dataset

```bash
python scripts/ingest_data.py                 # full ingestion into business.*
python scripts/ingest_data.py --dry-run       # validate without writing
python scripts/ingest_data.py --truncate      # re-ingest from scratch
```

Optional data-quality gates (writes `reports/data_profile.json`, `reports/integrity_report.json`):

```bash
python scripts/validate_data.py
```

### 6. Run the backend

```bash
cd backend
uvicorn app.main:app --reload          # http://localhost:8000 (docs at /docs)
```

Or run the full stack (PostgreSQL + Redis + backend + Next.js frontend):

```bash
docker compose up -d
```

### 7. Try the demo UI

```bash
pip install -r frontend/streamlit/requirements.txt
streamlit run frontend/streamlit/streamlit_app.py
```

The Streamlit app is the full conversational demo: chat with multi-turn `conversation_id`, live SSE node-timeline with per-stage timings, generated-SQL viewer, result tables, Altair charts, and cache-hit indicators.
The Next.js app under `frontend/` is a lightweight backend health/status dashboard.

---

## Evaluation

Four benchmark layers (cases in `eval/cases/*.yaml`, bilingual vi/en; golden results in `eval/golden_results/`):

| Layer | Scope | Needs LLM/DB |
|---|---|---|
| `retrieval` | Semantic retriever fidelity | No (CI-safe) |
| `guardrail` | Guardrail verdicts (unsafe-allows vs false-denies) | No (CI-safe) |
| `generation` | Text-to-SQL: valid rate + execution accuracy vs golden | Yes |
| `e2e` | Full graph outcome accuracy + execution accuracy | Yes |

```bash
python eval/run_eval.py --layers retrieval guardrail                        # CI-safe layers
python eval/run_eval.py --layers generation --suite smoke                   # single layer
python eval/run_eval.py --layers e2e --model-override google/gemini-2.5-flash
python eval/run_eval.py --layers retrieval guardrail --markdown             # + markdown report
```

Reports are written to `reports/eval/`. See [eval/README.md](eval/README.md) for the full flag reference.

### Offline demos

```bash
python scripts/demo_langgraph.py --scenario revenue_by_category   # offline graph walkthrough (--mermaid for diagram)
python scripts/demo_retrieval.py
python scripts/demo_guardrails.py
python scripts/demo_sql_generation.py
```

---

## Repository Structure

```text
conversational-analytics-agent/
├── backend/
│   ├── alembic/                  # Async migration suite (0001_initial_schema.py)
│   ├── app/
│   │   ├── agents/               # LLM clients, prompts, SQL generator
│   │   │   └── graph/            # LangGraph: state, nodes, routing, builder, checkpointer
│   │   ├── api/routes/           # /health, /health/ready, /analyze, /analyze/stream
│   │   ├── core/                 # Pydantic settings, logging, rate limiting
│   │   ├── db/                   # SQLAlchemy 2.x async (PostgreSQL) & Redis clients
│   │   ├── models/               # business.* / app.* / semantic.* declarative models
│   │   ├── repositories/         # Defensive SQL execution (timeout, row caps)
│   │   ├── schemas/              # Pydantic request/response schemas
│   │   ├── security/             # sqlglot guardrail validator + intent checks
│   │   ├── semantic/             # YAML catalog, loader, validator, hybrid retrieval
│   │   ├── services/             # Analyze service, caches, SSE formatting
│   │   └── main.py               # Entrypoint & lifespan
│   ├── tests/                    # pytest suite (13 modules: DB, graph, guardrails, caches…)
│   ├── Dockerfile                # Non-root production image
│   └── pyproject.toml            # Ruff + mypy + pytest configuration
├── frontend/
│   ├── app/                      # Next.js 15 health/status dashboard
│   └── streamlit/                # Full conversational demo UI (chat + SSE + charts)
├── data/
│   ├── raw/                      # Olist CSVs (gitignored — download manually)
│   └── processed/
├── docs/                         # Architecture & subsystem documentation
├── eval/                         # Benchmark cases, golden results, run_eval.py
├── reports/                      # Eval & data-quality report outputs
├── scripts/                      # Ingestion, validation, profiling, demos
├── tests/                        # Root-level tests (eval, data, streamlit demo, SSE client)
├── alembic.ini
├── docker-compose.yml            # postgres + redis + backend + frontend
├── .env.example
└── LICENSE                       # MIT
```

---

## Configuration

All settings are environment-driven (see `.env.example`). Key groups:

| Group | Variables |
|---|---|
| LLM | `LLM_PROVIDER` (`openrouter`/`groq`/`openai`/`mock`), `LLM_MODEL`, `LLM_TEMPERATURE`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENAI_API_KEY` |
| Embeddings | `RETRIEVAL_ENABLE_EMBEDDINGS` (default `false`), `EMBEDDING_BASE_URL/API_KEY/MODEL`, `RETRIEVAL_KEYWORD_WEIGHT` / `RETRIEVAL_EMBEDDING_WEIGHT` (0.6/0.4) |
| SQL policy | `SQL_MAX_LIMIT=1000`, `SQL_DEFAULT_LIMIT=100`, `SQL_EXECUTION_TIMEOUT=10`, `SQL_MAX_ROWS=5000`, `SQL_MAX_REPAIR_ATTEMPTS=3`, `SQL_MAX_EXECUTION_REPAIR_ATTEMPTS=2`, `SQL_ENFORCE_INTENT_CHECKS` |
| Graph | `CHECKPOINTER_BACKEND` (`memory`/`redis`), `GRAPH_RECURSION_LIMIT`, `SSE_HEARTBEAT_INTERVAL_SECONDS` |
| Caching | `CACHE_ENABLED`, `PROMPT_CACHE_TTL_SECONDS`, `RESULT_CACHE_TTL_SECONDS`, `SEMANTIC_CACHE_*` |
| Rate limiting | `RATE_LIMIT_ENABLED`, `RATE_LIMIT_RPM=30`, `RATE_LIMIT_BURST=10` |
| Infrastructure | `POSTGRES_*`, `REDIS_*`, `FRONTEND_URL`, `NEXT_PUBLIC_BACKEND_URL`, `STREAMLIT_BACKEND_URL` |

---

## Testing & Code Quality

```bash
# Full test suite (backend + root-level tests)
pytest -v                       # from repo root (pytest.ini covers backend/tests and tests/)

# Backend only
cd backend && pytest -v

# Lint & format
ruff check .

# Static type checking
mypy app tests
```

CI-safe eval regression (no LLM/DB required):

```bash
pytest tests/eval -q
python eval/run_eval.py --layers retrieval guardrail
```

---

## License

[MIT](LICENSE)
