#!/usr/bin/env python3
"""
CLI tool to run read-only SQL queries across a folder of CSV files.
"""

import os
import sys
import csv
import glob
import argparse
from pathlib import Path


def discover_csv_files(data_dir: str) -> dict:
    """
    Discover all CSV files in the data directory (including nested directories).
    Returns a dict mapping table names to file paths.
    """
    csv_files = {}

    # Find all CSV files recursively
    pattern = os.path.join(data_dir, "**", "*.csv")
    for filepath in glob.glob(pattern, recursive=True):
        # Get relative path from data_dir
        rel_path = os.path.relpath(filepath, data_dir)

        # Remove .csv suffix
        name_without_csv = os.path.splitext(rel_path)[0]

        # Split into directory and filename parts
        dir_part, filename = os.path.split(name_without_csv)

        # For nested directories, join with dot; for root, just use the filename
        if dir_part:
            # Nested directory: join directory parts with dots
            table_name = dir_part.replace(os.sep, '.') + '.' + filename
        else:
            table_name = filename

        # Validate the file has a header
        if not has_valid_header(filepath):
            raise ValueError(f"Invalid file (no header): {filepath}")

        csv_files[table_name] = filepath

    if not csv_files:
        raise ValueError(f"No CSV files found in directory: {data_dir}")

    return csv_files


def has_valid_header(filepath: str) -> bool:
    """Check if a CSV file has a valid (non-empty) header."""
    try:
        with open(filepath, 'r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            return header is not None and len(header) > 0
    except Exception:
        return False


def load_csv_data(filepath: str) -> 'pd.DataFrame':
    """
    Load a CSV file into a pandas DataFrame.
    Handle quoted fields, empty values, and various data types.
    """
    import pandas as pd

    # Read CSV, treating empty strings as NaN
    df = pd.read_csv(filepath, keep_default_na=False)

    # Convert empty strings to actual NaN for proper NULL handling
    df = df.replace('', pd.NA)

    # Normalize column names (strip whitespace)
    df.columns = df.columns.str.strip()

    # Try to convert columns to appropriate numeric types
    for col in df.columns:
        # Try to convert to numeric
        numeric_series = pd.to_numeric(df[col], errors='coerce')
        # If most values are numeric, convert
        if numeric_series.notna().sum() > numeric_series.isna().sum():
            df[col] = numeric_series

    return df


def parse_sql_query(sql_input: str) -> str:
    """
    Parse SQL input: if it's a file path, read from it; otherwise treat as raw SQL.
    Returns the SQL query string.
    """
    sql_path = Path(sql_input)
    if sql_path.exists():
        with open(sql_path, 'r', encoding='utf-8') as f:
            return f.read().strip()
    return sql_input.strip()


def transform_sql_query(sql_query: str, discovered_table_names: list) -> str:
    """
    Transform SQL query to replace dotted table names with underscored versions.
    """
    import re

    # Build mapping of dotted names to underscored versions
    dotted_name_to_underscored: dict[str, str] = {}
    for table_name in discovered_table_names:
        if '.' in table_name:
            dotted_name_to_underscored[table_name] = table_name.replace('.', '_')

    # Replace each dotted name with its underscored version in the SQL
    transformed_sql = sql_query
    for dotted_name, underscored_name in dotted_name_to_underscored.items():
        pattern = r'\b' + re.escape(dotted_name) + r'\b'
        transformed_sql = re.sub(pattern, underscored_name, transformed_sql)

    return transformed_sql


def execute_query(data_dir: str, sql_query: str) -> 'pd.DataFrame':
    """
    Execute a SQL query on CSV files in the data directory.
    Returns a pandas DataFrame with the result.
    """
    import pandas as pd
    import duckdb

    # Discover CSV files
    table_map = discover_csv_files(data_dir)
    table_names = list(table_map.keys())

    # Load all CSV files into DataFrames and register with DuckDB
    con = duckdb.connect()

    for table_name, filepath in table_map.items():
        df = load_csv_data(filepath)

        # If table name contains dots, also register with underscored version
        # This is needed because DuckDB doesn't support dots in table names
        if '.' in table_name:
            underscored_name = table_name.replace('.', '_')
            con.register(underscored_name, df)
        else:
            con.register(table_name, df)

    # Transform SQL query to use underscored table names
    transformed_sql = transform_sql_query(sql_query, table_names)

    try:
        # Execute the query
        result = con.execute(transformed_sql).fetchdf()
    except Exception as e:
        raise RuntimeError(f"SQL execution error: {e}")
    finally:
        con.close()

    return result


def dataframe_to_markdown(df: 'pd.DataFrame') -> str:
    """
    Convert a pandas DataFrame to a markdown table string.
    """
    import pandas as pd

    columns = list(df.columns)

    if df.empty:
        if not columns:
            return "| |\n|---|---|\n|\n"

        header = "| " + " | ".join(str(col) for col in columns) + " |"
        separator = "| " + " | ".join(["---"] * len(columns)) + " |"
        return f"{header}\n{separator}\n"

    # Format header row
    header = "| " + " | ".join(str(col) for col in columns) + " |"

    # Format separator row
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"

    # Format data rows
    rows = []
    for _, row in df.iterrows():
        row_values = []
        for col in columns:
            val = row[col]
            if pd.isna(val):
                row_values.append("NULL")
            else:
                row_values.append(str(val))
        rows.append("| " + " | ".join(row_values) + " |")

    return "\n".join([header, separator] + rows)


def dataframe_to_csv(df: 'pd.DataFrame') -> str:
    """
    Convert a pandas DataFrame to CSV format with proper NULL handling.
    """
    import pandas as pd

    # According to spec, we need valid CSV with header
    output = []

    # Add header
    header = list(df.columns)
    output.append(",".join(str(h) for h in header))

    # Add data rows
    for _, row in df.iterrows():
        row_values = []
        for col in header:
            val = row[col]
            if pd.isna(val):
                row_values.append("")  # Empty for CSV NULL
            else:
                # Quote the value if it contains commas or quotes
                val_str = str(val)
                if "," in val_str or '"' in val_str:
                    val_str = '"' + val_str.replace('"', '""') + '"'
                row_values.append(val_str)
        output.append(",".join(row_values))

    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(
        description="Execute read-only SQL queries on CSV files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python query_files.py --data ./data --sql "SELECT * FROM users LIMIT 10"
  python query_files.py --data ./data --sql queries.sql --out result.csv
  python query_files.py --data ./data --sql "SELECT country, COUNT(*) FROM users GROUP BY country"
"""
    )
    parser.add_argument(
        "--data",
        required=True,
        help="Directory containing CSV files (can be nested)"
    )
    parser.add_argument(
        "--sql",
        required=True,
        help="SQL query string or path to SQL file"
    )
    parser.add_argument(
        "--out",
        required=False,
        help="Optional path to save result as CSV file"
    )

    args = parser.parse_args()

    # Validate data directory
    if not os.path.isdir(args.data):
        print(f"Error: Data directory not found: {args.data}", file=sys.stderr)
        sys.exit(1)

    # Parse SQL input (could be raw SQL or file path)
    try:
        sql_query = parse_sql_query(args.sql)
    except Exception as e:
        print(f"Error reading SQL: {e}", file=sys.stderr)
        sys.exit(1)

    if not sql_query:
        print("Error: SQL query is empty", file=sys.stderr)
        sys.exit(1)

    # Execute the query
    try:
        result_df = execute_query(args.data, sql_query)
    except Exception as e:
        print(f"Error executing query: {e}", file=sys.stderr)
        sys.exit(1)

    # Output to STDOUT as markdown table
    markdown_output = dataframe_to_markdown(result_df)
    print(markdown_output)

    # Optionally save to CSV file
    if args.out:
        try:
            csv_output = dataframe_to_csv(result_df)
            with open(args.out, 'w', newline='', encoding='utf-8') as f:
                f.write(csv_output)
            print(f"\n\nResults also saved to: {args.out}", file=sys.stderr)
        except Exception as e:
            print(f"Error writing output file: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
