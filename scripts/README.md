# Utility Scripts

This directory houses development, database migration, and operations utility scripts.

## Available Tools

### 1. Data Ingestion Pipeline (`ingest_data.py`)

Ingests the Olist Brazilian E-Commerce dataset from `data/raw/` into the PostgreSQL `business` schema.

```bash
# Full ingestion with truncation of existing business records:
python scripts/ingest_data.py --truncate

# Dry-run validation only (no DB writes):
python scripts/ingest_data.py --dry-run

# Custom data directory or DB URL:
python scripts/ingest_data.py --data-dir path/to/raw --database-url postgresql://...
```

**Pipeline Stages:**
1. **Discovery & Loading**: Scans and loads CSVs from `data/raw/`.
2. **Schema Validation**: Checks exact column presence and schema specifications.
3. **Type Conversion & Cleaning**: Formats dates, numerical precision, zip codes, and strips text.
4. **Referential Integrity**: Enforces PK uniqueness, non-negative checks, and parent-child foreign key consistency.
5. **PostgreSQL Bulk Ingestion**: Uses transactional `asyncpg.copy_records_to_table` for high-throughput insertion.
6. **Post-Ingestion Verification**: Cross-checks row counts against the database.

### 2. Data Profiling (`profile_data.py`)

Profiles PostgreSQL tables in the `business` schema, generating row counts, column types, null percentages, distinct counts, numeric stats (min, max, avg), timestamp spans, and top categorical frequencies.

```bash
# Profile all business tables:
python scripts/profile_data.py

# Profile specific tables and output to custom file:
python scripts/profile_data.py --table orders products --output reports/custom_profile.json
```

### 3. Data Integrity & Sanity Validation (`validate_integrity.py`)

Executes comprehensive validation across Primary Keys, Foreign Key referential integrity (handling nullable keys), and business sanity rules.

```bash
# Run integrity validation suite:
python scripts/validate_integrity.py
```

### 4. Master Data Validation Pipeline (`validate_data.py`)

Single-command orchestrator that executes both data profiling and integrity validation, generating machine-readable reports in `reports/` and returning exit code `0` on PASS or `1` on FAIL.

```bash
# Run complete validation pipeline:
python scripts/validate_data.py

# Run with sub-stage options:
python scripts/validate_data.py --profile-only
python scripts/validate_data.py --integrity-only
```

### 5. Golden Result Materializer (`materialize_golden_results.py`)

Executes each generation/e2e case's golden SQL once against PostgreSQL (through the guardrail-sanitizing `AnalyticsRepository`) and stores result sets in `eval/golden_results/<case_id>.json` for later execution-accuracy comparison.

```bash
# Materialize missing golden result sets:
python scripts/materialize_golden_results.py --layers generation e2e

# Overwrite all and prove golden SQL determinism (re-runs + compares):
python scripts/materialize_golden_results.py --force --validate-ex

# Single case:
python scripts/materialize_golden_results.py --case gen_rev_by_state --force
```

Returns exit code 1 on failed executions or unstable (non-deterministic) golden SQL.

### 6. LangGraph Visual Demo (`demo_langgraph.py`)

Runs the compiled analytics graph offline (fake retriever/executor, real AST guardrails) and prints every node transition in the terminal, including both repair loops.

```bash
# Run all scenarios (happy path, guardrail repair, execution repair, combined repair,
# budgets, non-repairable failure, retrieval skip, LLM error):
python scripts/demo_langgraph.py

# Run one scenario:
python scripts/demo_langgraph.py --scenario execution-repair

# Show full SQL candidates, guardrail violations, and repair prompts:
python scripts/demo_langgraph.py --scenario combined-repair --verbose

# Fail with exit code 1 when a scenario does not match its expected outcome (CI-friendly):
python scripts/demo_langgraph.py --strict

# List scenarios / print the graph as a Mermaid diagram:
python scripts/demo_langgraph.py --list
python scripts/demo_langgraph.py --mermaid
```

When multiple scenarios run, the CLI prints a summary table comparing the observed
result (success/failure, failed stage, repair count, node count, duration) with the
expected outcome encoded in each scenario.

## Planned Scripts

- Database seeders (mock app sessions)

