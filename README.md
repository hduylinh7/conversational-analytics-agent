# Conversational Analytics Agent

A production-oriented conversational AI analytics system that will eventually allow users to query business data using natural language while enforcing SQL security and execution policies.

---

## Current Status

**Initial Repository Foundation (Phase 0)**

This repository provides the core modular monolith foundation, containerized infrastructure, database connectivity, and health verification pipelines.

The following business and agent capabilities are intentionally **NOT** implemented yet:
- ❌ Text-to-SQL generation
- ❌ LangGraph agent orchestration
- ❌ Semantic layer & metric definitions
- ❌ SQL AST validation & security guardrails
- ❌ Dataset ingestion & schema creation
- ❌ Production analytics dashboard UI
- ❌ Benchmark evaluation suite

---

## Architecture

The system follows a clean modular monolith architecture with clear package boundaries:

```text
Frontend (Next.js / App Router)
   │
   ▼
FastAPI Backend (Async Python 3.12)
   │
   ├── PostgreSQL (Data storage & SQL execution target)
   │
   └── Redis (Connection cache & session store)
```

---

## Repository Structure

```text
conversational-analytics-agent/
├── backend/                  # FastAPI asynchronous backend application
│   ├── app/
│   │   ├── agents/           # Future: LangGraph orchestration workflows
│   │   ├── api/              # API routing and HTTP endpoints
│   │   │   └── routes/       # Endpoint route handlers (/health, /health/ready)
│   │   ├── core/             # Centralized settings (Pydantic) and logging
│   │   ├── db/               # PostgreSQL (SQLAlchemy 2.x async) & Redis clients
│   │   ├── models/           # Future: SQLAlchemy ORM models
│   │   ├── repositories/     # Future: Data access layer
│   │   ├── schemas/          # Pydantic request and response schemas
│   │   ├── security/         # Future: SQL AST validation and guardrails
│   │   ├── services/         # Future: Business and orchestration services
│   │   └── main.py           # Application entrypoint & lifespan lifecycle
│   ├── tests/                # Pytest test suite with async mock fixtures
│   ├── Dockerfile            # Production-ready non-root Dockerfile
│   ├── pyproject.toml        # Ruff, mypy, and pytest configuration
│   └── requirements.txt      # Python dependencies
├── frontend/                 # Minimal Next.js 15 client placeholder
│   ├── app/                  # Next.js App Router (layout, page, styles)
│   ├── Dockerfile            # Containerized frontend runner
│   ├── package.json          # Node dependencies & scripts
│   └── tsconfig.json         # TypeScript configuration
├── data/                     # Data directory (raw & processed data gitignored)
├── scripts/                  # Utility, migration, and automation scripts
├── eval/                     # Evaluation benchmarks and test cases
├── tests/                    # Top-level integration & contract tests
├── docker-compose.yml        # Multi-service infrastructure orchestration
├── .env.example              # Baseline environment configuration template
├── .gitignore                # Git ignore rules for Python, Node, data, and secrets
└── LICENSE                   # MIT License
```

---

## Local Development

### 1. Prerequisites
- Docker & Docker Compose v2+
- Python 3.11+ / 3.12+ (for local backend development)
- Node.js 20+ (for local frontend development)

### 2. Environment Configuration
Copy the template to create your `.env` file:
```bash
cp .env.example .env
```

### 3. Running with Docker Compose (Recommended)
To build and start all 4 services (`postgres`, `redis`, `backend`, `frontend`):
```bash
docker compose up --build
```

To run in detached mode:
```bash
docker compose up --build -d
```

### 4. Running Backend Locally (Outside Docker)
If running against local Docker services:
```bash
# Start only postgres and redis in background
docker compose up -d postgres redis

# Set up Python virtual environment
cd backend
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Start backend server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 5. Running Frontend Locally
```bash
cd frontend
npm install
npm run dev
```

---

## Verification URLs

| Service | URL | Description |
|---|---|---|
| Frontend | [http://localhost:3000](http://localhost:3000) | Analytics client UI placeholder & health monitor |
| Backend Liveness | [http://localhost:8000/health](http://localhost:8000/health) | API service liveness probe |
| Backend Readiness | [http://localhost:8000/health/ready](http://localhost:8000/health/ready) | PostgreSQL & Redis connectivity probe |
| Swagger OpenAPI | [http://localhost:8000/docs](http://localhost:8000/docs) | Interactive API documentation |
| ReDoc | [http://localhost:8000/redoc](http://localhost:8000/redoc) | Alternative OpenAPI documentation |

---

## Quality Checks & Testing

### Running Tests
```bash
cd backend
pytest
```

### Linting & Formatting
```bash
cd backend
ruff check .
ruff format --check .
```

### Type Checking
```bash
cd backend
mypy app
```
