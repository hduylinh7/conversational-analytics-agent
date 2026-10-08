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

## Planned Scripts

- Database seeders (mock app sessions)
- Evaluation benchmark runners

