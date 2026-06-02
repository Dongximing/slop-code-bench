#!/usr/bin/env python3
"""SQL Query CLI for multiple file formats."""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd
from database import CSVDatabase
from query_executor import SQLExecutor


def load_sql_query(sql_arg: str) -> str:
    """Load SQL query from file or return as-is."""
    sql_path = Path(sql_arg)
    if sql_path.exists():
        return sql_path.read_text()
    return sql_arg


def format_as_markdown(df) -> str:
    """Format DataFrame as a markdown table."""
    if df.empty:
        return ""

    headers = list(df.columns)
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]

    for _, row in df.iterrows():
        cells = []
        for col in headers:
            val = row[col]
            cells.append("NULL" if pd.isna(val) else str(val))
        lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines)


def save_as_csv(df, output_path: str):
    """Save DataFrame to CSV file."""
    df.to_csv(output_path, index=False)


def main():
    parser = argparse.ArgumentParser(description="Run SQL queries on CSV files")
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query or path to SQL file')
    parser.add_argument('--out', required=False, help='Output file path for CSV result')
    args = parser.parse_args()

    if not os.path.isdir(args.data):
        print(f"Error: Data directory '{args.data}' does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        sql = load_sql_query(args.sql)
    except Exception as e:
        print(f"Error loading SQL: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        db = CSVDatabase(args.data)
        if not db.tables:
            print("Error: No valid CSV files found in data directory", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        executor = SQLExecutor(db)
        result = executor.execute(sql)
    except Exception as e:
        print(f"Error executing query: {e}", file=sys.stderr)
        sys.exit(1)

    print(format_as_markdown(result))

    if args.out:
        try:
            save_as_csv(result, args.out)
        except Exception as e:
            print(f"Error saving output: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
