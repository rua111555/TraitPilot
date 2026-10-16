#!/usr/bin/env python3
"""
WGS Expressive Pipeline — Database Query Utility
=================================================

This is the ONLY pre-written script for the expressive pipeline skill.
The LLM calls this script at runtime to fetch all pipeline parameters
from the SQLite database, instead of hardcoding paths in shell scripts.

Actions:
  query-all      — Single JSON dump: reference genome, sample fastq info, config
  query-ref      — Reference genome info (fasta_path, dict_path, fai_path, etc.)
  query-samples  — Sample fastq paths & metadata for given sample numbers
  query-config   — Pipeline configuration (known sites, output dir, etc.)

Output: JSON to stdout (machine-readable for the LLM).

Usage:
  conda run -n Agent python db_query.py --action query-all --samples 1,2,10
  conda run -n Agent python db_query.py --action query-config
  conda run -n Agent python db_query.py --action query-ref
  conda run -n Agent python db_query.py --action query-samples --samples 1,10
"""

import argparse
import json
import logging
import os
import sqlite3
import sys

logger = logging.getLogger("db_query")

DEFAULT_GENO_DIR = os.environ.get("GENO_DIR", os.path.join(os.path.expanduser("~"), "genomics_data"))
DEFAULT_DB = os.environ.get("PIPELINE_DB", os.path.join(DEFAULT_GENO_DIR, "pipeline.db"))


def get_conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def query_ref(conn):
    """Fetch reference genome info."""
    row = conn.execute("SELECT * FROM reference_genome ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"error": "No reference genome found in database"}
    return dict(row)


def query_samples(conn, sample_numbers):
    """Fetch sample fastq info for given sample numbers."""
    if not sample_numbers:
        return {"error": "No sample numbers provided"}
    placeholders = ",".join("?" for _ in sample_numbers)
    rows = conn.execute(
        f"SELECT * FROM fastq_samples WHERE sample_number IN ({placeholders}) ORDER BY sample_number",
        sample_numbers,
    ).fetchall()
    if not rows:
        return {"error": f"No samples found for numbers: {sample_numbers}"}
    return [dict(r) for r in rows]


def query_config(conn):
    """Fetch pipeline configuration key-value pairs."""
    rows = conn.execute("SELECT key, value, description FROM pipeline_config").fetchall()
    return {r["key"]: {"value": r["value"], "description": r["description"]} for r in rows}


def query_all(conn, sample_numbers):
    """Fetch everything the LLM needs in one JSON blob."""
    ref = query_ref(conn)
    samples = query_samples(conn, sample_numbers)
    config = query_config(conn)
    return {
        "reference_genome": ref,
        "samples": samples,
        "config": config,
    }


def main():
    parser = argparse.ArgumentParser(description="Query WGS Pipeline SQLite database")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path")
    parser.add_argument(
        "--action",
        required=True,
        choices=["query-all", "query-ref", "query-samples", "query-config"],
        help="Query action",
    )
    parser.add_argument("--samples", help="Comma-separated sample numbers (required for query-samples, query-all)")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(json.dumps({"error": f"Database not found: {args.db}"}))
        sys.exit(1)

    conn = get_conn(args.db)

    sample_numbers = None
    if args.samples:
        sample_numbers = [int(s.strip()) for s in args.samples.split(",") if s.strip()]

    if args.action == "query-ref":
        result = query_ref(conn)
    elif args.action == "query-samples":
        if not sample_numbers:
            print(json.dumps({"error": "--samples is required for query-samples action"}))
            sys.exit(1)
        result = query_samples(conn, sample_numbers)
    elif args.action == "query-config":
        result = query_config(conn)
    elif args.action == "query-all":
        if not sample_numbers:
            print(json.dumps({"error": "--samples is required for query-all action"}))
            sys.exit(1)
        result = query_all(conn, sample_numbers)
    else:
        result = {"error": f"Unknown action: {args.action}"}

    conn.close()
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
