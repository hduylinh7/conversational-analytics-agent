# Data Directory

This directory is reserved for data storage and ingestion pipelines.

## Structure

- `raw/`: Unprocessed source files (e.g., CSV, JSON, raw database dumps). Files in this directory are excluded from version control by `.gitignore`.
- `processed/`: Transformed, cleaned, or benchmark data ready for loading or analysis.

> Note: Do not commit large datasets or sensitive production data to git.
