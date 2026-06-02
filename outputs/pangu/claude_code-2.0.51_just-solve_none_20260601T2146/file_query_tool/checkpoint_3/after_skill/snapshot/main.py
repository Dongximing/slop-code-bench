#!/usr/bin/env python3
"""SQL Query CLI for multiple file formats."""

import argparse
import sys
from pathlib import Path

import pandas as pd
from database import CSVDatabase
from query_executor import SQLExecutor


def load_sql_query(sql_arg: str) -> str:
    sql_path = Path(sql_arg)
    if sql_path.exists():
        return sql_path.read_text()
    return sql_arg


def format_as_markdown(df) -> str:
    if df.empty:
        return ""

    headers = list(df.columns)
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]

    for _, row in df.iterrows():
        cells = ["NULL" if pd.isna(row[col]) else str(row[col]) for col in headers]
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def parse_sharded_arg(sharded_str: str) -> tuple:
    if '=' not in sharded_str:
        raise ValueError(f"Invalid --sharded format: '{sharded_str}'. Expected '<table_name>=<glob_pattern>'")
    table_name, pattern = sharded_str.split('=', 1)
    if not table_name.strip():
        raise ValueError(f"Empty table name in --sharded: '{sharded_str}'")
    if not pattern.strip():
        raise ValueError(f"Empty glob pattern in --sharded: '{sharded_str}'")
    return (table_name.strip(), pattern.strip())


def main():
    parser = argparse.ArgumentParser(description="Run SQL queries on CSV files")
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query or path to SQL file')
    parser.add_argument('--out', help='Output file path for CSV result')
    parser.add_argument('--sharded', action='append', default=[],
                       help='Sharded table definition: <table_name>=<glob_pattern>. Can be specified multiple times.')
    args = parser.parse_args()

    sharded_configs = []
    for sharded_arg in args.sharded:
        try:
            sharded_configs.append(parse_sharded_arg(sharded_arg))
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    if not Path(args.data).is_dir():
        print(f"Error: Data directory '{args.data}' does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        sql = load_sql_query(args.sql)
    except Exception as e:
        print(f"Error loading SQL: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        db = CSVDatabase(args.data)
        if sharded_configs:
            db.load_sharded_tables(sharded_configs)
        if not db.tables:
            print("Error: No valid CSV files found in data directory", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        result = SQLExecutor(db).execute(sql)
        print(format_as_markdown(result))
        if args.out:
            result.to_csv(args.out, index=False)
    except Exception as e:
        print(f"Error executing query: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()