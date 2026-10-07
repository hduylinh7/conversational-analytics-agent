# Conversational Analytics Agent

A production-oriented conversational AI analytics system designed to enable analysts to query business data using natural language, with robust schema retrieval, SQL generation, AST guardrails, and visualization.

---

## Current Status

**Database Foundation (Phase 1)**

The PostgreSQL database architecture is fully implemented, migrated, and covered with automated tests.

* **PostgreSQL Schemas (3)**: `business`, `app`, `semantic`
* **Total Tables (13)**:
  * `business` (5): `customers`, `orders`, `order_items`, `products`, `payments`
  * `app` (5): `users`, `conversations`, `messages`, `query_executions`, `saved_queries`
  * `semantic` (3): `metric_definitions`, `dimension_definitions`, `data_sources`
* **Migrations**: Alembic async migration suite supporting upgrade, downgrade, and replay.
* **Constraints & Indexes**: Composite PKs, foreign keys, unique constraints, check constraints, and performance indexes.

> ⚠️ **Important Notice on Data Ingestion**:
>
> The Olist Brazilian E-Commerce dataset has **NOT** been downloaded or ingested yet.
> * No CSV files are present under `data/raw/`.
> * The business tables currently contain **no production records**.
> * In the next phase, the user will manually download the Olist dataset and place only the required CSV files in `data/raw/`.

---

## Architecture

The system follows a clean modular monolith architecture with clear package boundaries:

```text
Frontend (Next.js / App Router)
   │
   ▼
FastAPI Backend (Async Python 3.12)
   │
   ├── PostgreSQL (Schemas: business, app, semantic)
   │
   └── Redis (Connection cache & session store)
```

For detailed schema diagrams and column descriptions:
* **Entity-Relationship Diagram**: [docs/erd.mmd](docs/erd.mmd)
* **Database Documentation**: [docs/database.md](docs/database.md)

---

## Database Setup & Workflows

### 1. Start PostgreSQL

Start PostgreSQL (and Redis) using Docker Compose:

```bash
docker compose up -d postgres redis
```

The database container will start with:
* Database: `analytics`
* User: `analytics`
* Password: `analytics`
* Host Port: `5433` (or `5432` if available; configured in `.env`)

### 2. Run Alembic Migrations

Apply database migrations to create the schemas and all 13 tables:

```bash
# From repository root:
alembic upgrade head

# Or from backend directory:
cd backend
alembic upgrade head
```

To roll back migrations:
```bash
alembic downgrade base
```

### 3. Run Database & Application Tests

Run the full automated test suite (including schema, constraint, relationship, and health tests):

```bash
# From repository root:
pytest backend/tests -v

# Or from backend directory:
cd backend
pytest -v
```

---

## Repository Structure

```text
conversational-analytics-agent/
├── backend/                  # FastAPI asynchronous backend application
│   ├── alembic/              # Alembic migration scripts and environment
│   │   ├── versions/         # Migration versions (0001_initial_schema.py)
│   │   ├── env.py            # Async migration runner across schemas
│   │   └── script.py.mako    # Migration template
│   ├── app/
│   │   ├── agents/           # Future: LangGraph orchestration workflows
│   │   ├── api/              # API routing and HTTP endpoints (/health)
│   │   ├── core/             # Centralized settings (Pydantic) and logging
│   │   ├── db/               # PostgreSQL (SQLAlchemy 2.x async) & Redis clients
│   │   ├── models/           # SQLAlchemy 2.x declarative models
│   │   │   ├── base.py       # DeclarativeBase base class
│   │   │   ├── business.py   # business.* schema models (5 tables)
│   │   │   ├── application.py# app.* schema models (5 tables)
│   │   │   └── semantic.py   # semantic.* schema models (3 tables)
│   │   ├── repositories/     # Future: Data access layer
│   │   ├── schemas/          # Pydantic request and response schemas
│   │   ├── security/         # Future: SQL AST validation and guardrails
│   │   ├── services/         # Future: Business and orchestration services
│   │   └── main.py           # Application entrypoint & lifespan lifecycle
│   ├── tests/                # Automated pytest suite (database & health)
│   ├── alembic.ini           # Backend Alembic configuration
│   ├── Dockerfile            # Production-ready non-root Dockerfile
│   ├── pyproject.toml        # Ruff, mypy, and pytest configuration
│   └── requirements.txt      # Python dependencies
├── docs/
│   ├── database.md           # Full database schema and table documentation
│   └── erd.mmd               # Mermaid ER diagram
├── frontend/                 # Minimal Next.js 15 client placeholder
├── data/
│   ├── raw/                  # Target for future Olist CSV files (.gitkeep)
│   └── processed/            # Target for future processed data (.gitkeep)
├── scripts/                  # Utility and operations scripts
├── eval/                     # Evaluation benchmarks and test cases
├── alembic.ini               # Root Alembic configuration
├── docker-compose.yml        # Multi-service infrastructure orchestration
├── .env.example              # Baseline environment configuration template
├── .gitignore                # Git ignore rules
└── LICENSE                   # MIT License
```

---

## Next Steps (Planned Phases)

1. **Manual Olist Dataset Placement**:
   Place the 5 required CSV files into `data/raw/`:
   * `olist_customers_dataset.csv`
   * `olist_orders_dataset.csv`
   * `olist_order_items_dataset.csv`
   * `olist_products_dataset.csv`
   * `olist_order_payments_dataset.csv`

2. **Ingestion Pipeline**:
   Implement data validation, foreign key ordering, and ingestion into the `business` schema.

3. **Semantic Layer & Agent**:
   Populate semantic definitions, configure retrieval, and implement Text-to-SQL generation with AST validation.

---

## Code Quality & Verification

```bash
cd backend

# Run tests
pytest -v

# Code formatting & linting
ruff check .

# Static type checking
mypy app tests
```
