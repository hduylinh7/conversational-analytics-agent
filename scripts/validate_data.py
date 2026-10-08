#!/usr/bin/env python3
"""
Master Data Quality & Integrity Validation Pipeline.

Single command runner that:
1. Executes Data Profiling across all 5 Olist business tables
   -> Generates reports/data_profile.json
2. Executes Primary Key, Foreign Key, and Business Sanity Validations
   -> Generates reports/integrity_report.json
3. Displays a comprehensive summary dashboard in the terminal
4. Exits with code 0 on PASS, 1 on FAIL

Usage:
    python scripts/validate_data.py
    python scripts/validate_data.py --profile-only
    python scripts/validate_data.py --integrity-only
    python scripts/validate_data.py --output-dir custom_reports/
"""

import argparse
import asyncio
from pathlib import Path
import sys
import time
from typing import Any

# Add project root and backend directory to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure stdout/stderr handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

from scripts.profile_data import DataProfiler, print_profile_summary, DataProfileJSONEncoder
from scripts.validate_integrity import IntegrityValidator, print_integrity_summary
import json


async def run_pipeline(
    output_dir: Path,
    profile_only: bool = False,
    integrity_only: bool = False,
    quiet: bool = False,
) -> int:
    """Run data quality pipeline stages."""
    output_dir.mkdir(parents=True, exist_ok=True)
    profile_file = output_dir / "data_profile.json"
    integrity_file = output_dir / "integrity_report.json"

    start_time = time.time()
    exit_code = 0

    profile_report: dict[str, Any] | None = None
    integrity_report: dict[str, Any] | None = None

    # Step 1: Data Profiling
    if not integrity_only:
        if not quiet:
            print("\n[Step 1/2] Profiling business tables in PostgreSQL...")
        profiler = DataProfiler()
        profile_report = await profiler.profile_all()

        with open(profile_file, "w", encoding="utf-8") as f:
            json.dump(profile_report, f, indent=2, cls=DataProfileJSONEncoder)

        if not quiet and profile_only:
            print_profile_summary(profile_report)
        elif not quiet:
            tot_rows = profile_report["summary"]["total_rows"]
            tot_tbls = profile_report["summary"]["total_tables"]
            print(f" -> Profiling complete: {tot_tbls} tables, {tot_rows:,} total rows.")
            print(f" -> Saved to: {profile_file}")

    # Step 2: Data Integrity & Business Sanity Validation
    if not profile_only:
        if not quiet:
            print("\n[Step 2/2] Validating PKs, FKs, and business sanity rules...")
        validator = IntegrityValidator()
        integrity_report = await validator.validate_all()

        with open(integrity_file, "w", encoding="utf-8") as f:
            json.dump(integrity_report, f, indent=2)

        if not quiet:
            print_integrity_summary(integrity_report)

        if integrity_report["summary"]["overall_status"] != "PASS":
            exit_code = 1

    elapsed = round(time.time() - start_time, 2)

    # Master Summary
    if not quiet and not profile_only and not integrity_only:
        print("=" * 80)
        print("                    PIPELINE EXECUTION SUMMARY")
        print("=" * 80)
        print(f"Execution Duration: {elapsed} seconds")
        if profile_report:
            print(f"Tables Profiled:    {profile_report['summary']['total_tables']} tables ({profile_report['summary']['total_rows']:,} rows)")
            print(f"Profile Artifact:   {profile_file}")
        if integrity_report:
            summary = integrity_report["summary"]
            print(f"Integrity Checks:   {summary['passed_checks']}/{summary['total_checks']} passed ({summary['failed_checks']} failed)")
            print(f"Integrity Status:   [{summary['overall_status']}]")
            print(f"Integrity Artifact: {integrity_file}")
        print("=" * 80 + "\n")

    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified Data Profiling & Integrity Validation Pipeline for Olist Dataset."
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=REPO_ROOT / "reports",
        help="Directory to save generated JSON reports (default: reports/)",
    )
    parser.add_argument(
        "--profile-only",
        action="store_true",
        help="Run data profiling only",
    )
    parser.add_argument(
        "--integrity-only",
        action="store_true",
        help="Run integrity & sanity validation only",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="Suppress verbose logging",
    )

    args = parser.parse_args()

    exit_code = asyncio.run(
        run_pipeline(
            output_dir=args.output_dir,
            profile_only=args.profile_only,
            integrity_only=args.integrity_only,
            quiet=args.quiet,
        )
    )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
