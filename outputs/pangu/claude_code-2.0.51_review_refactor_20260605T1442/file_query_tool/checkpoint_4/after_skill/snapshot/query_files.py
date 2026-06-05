#!/usr/bin/env python3
"""
CLI tool to run read-only SQL queries across a folder of multiple file formats.
Supports: CSV, Parquet, TSV, JSON, JSONL
Also supports compressed files (.gz, .bz2)
"""

import os
import sys
import glob
import argparse
import json
from pathlib import Path
from collections import defaultdict

SUPPORTED_FORMATS = {
    '.csv': 'csv',
    '.parquet': 'parquet',
    '.tsv': 'tsv',
    '.json': 'json',
    '.jsonl': 'jsonl',
}

COMPRESSED_EXTENSIONS = {'.gz', '.bz2'}

# Error codes and messages
ERROR_CODES = {
    3: {
        'GROUP': 'WINDOW_ERROR',
        'TYPES': {
            'INVALID_WINDOW_SPEC': 'Invalid window specification',
            'FRAME_ERROR': 'Frame clause error',
            'NESTED_WINDOW_ERROR': 'Nested window functions are not allowed'
        }
    }
}


def create_error(error_type: str, message: str = None) -> str:
    """Create a formatted error message with error code."""
    if error_type in ERROR_CODES[3]['TYPES']:
        default_msg = ERROR_CODES[3]['TYPES'][error_type]
        return f"[Code 3 - {ERROR_CODES[3]['GROUP']} - {error_type}] {message or default_msg}"
    return f"Unknown error: {error_type}"


def get_file_info(filepath: str) -> tuple:
    """
    Extract file information: base_name, format, compression.

    Returns:
        tuple: (base_name, format_type, compression) or (None, None, None) if unsupported
    """
    path = Path(filepath)

    # Get all extensions
    extensions = path.suffixes

    if not extensions:
        return None, None, None

    # Check for compression first (last extension)
    compression = None
    format_ext = None

    # Check last extension for compression
    if extensions and extensions[-1] in COMPRESSED_EXTENSIONS:
        compression = extensions[-1]
        extensions = extensions[:-1]

    # Now check format extension
    if extensions:
        format_ext = extensions[-1]

    if format_ext not in SUPPORTED_FORMATS:
        return None, None, None

    # Extract base name (without format and compression extensions)
    base_name = path.name
    for ext in [format_ext]:
        if compression:
            ext += compression
        if base_name.endswith(ext):
            base_name = base_name[:-len(ext)]
            break

    return base_name, SUPPORTED_FORMATS[format_ext], compression


def discover_files(data_dir: str) -> dict:
    """
    Discover all supported files in the data directory (including nested directories).
    Returns a dict mapping table names to file paths.

    Validates that files with the same base name but different suffixes/compressions
    have the exact same columns.
    """
    file_groups = defaultdict(list)

    # Find all files with supported extensions
    for ext in SUPPORTED_FORMATS.keys():
        pattern = os.path.join(data_dir, "**", f"*{ext}")
        # Also check for compressed versions
        pattern_gz = os.path.join(data_dir, "**", f"*{ext}.gz")
        pattern_bz2 = os.path.join(data_dir, "**", f"*{ext}.bz2")

        patterns = [pattern, pattern_gz, pattern_bz2]

        for pat in patterns:
            for filepath in glob.glob(pat, recursive=True):
                base_name, format_type, compression = get_file_info(filepath)
                if base_name is None:
                    continue

                # Store file info
                file_groups[base_name].append({
                    'path': filepath,
                    'format': format_type,
                    'compression': compression,
                })

    # Validate and build table map
    table_map = {}

    for base_name, files in file_groups.items():
        if not files:
            continue

        # Get columns from first file
        first_file = files[0]
        first_columns = get_columns(first_file)

        # Check all other files have the same columns
        for file_info in files[1:]:
            other_columns = get_columns(file_info)
            if first_columns != other_columns:
                raise ValueError(
                    f"Files with same name '{base_name}' have different columns:\n"
                    f"  {first_file['path']}: {list(first_columns)}\n"
                    f"  {file_info['path']}: {list(other_columns)}"
                )

        # Use the first file as the representative for the table
        # Determine table name from path
        rel_path = os.path.relpath(first_file['path'], data_dir)
        dir_part, filename = os.path.split(rel_path)
        name_without_ext = Path(filename).stem
        # Remove compression extension if present
        if name_without_ext.endswith('.gz') or name_without_ext.endswith('.bz2'):
            name_without_ext = Path(name_without_ext).stem

        if dir_part and dir_part != '.':
            # Nested directory: join directory parts with dots
            table_name = dir_part.replace(os.sep, '.') + '.' + name_without_ext
        else:
            table_name = name_without_ext

        table_map[table_name] = first_file['path']

    if not table_map:
        raise ValueError(f"No supported files found in directory: {data_dir}")

    return table_map


def get_columns(file_info: dict) -> tuple:
    """
    Get the column names from a file.
    Returns a tuple of column names for comparison.
    """
    df = load_data(file_info)
    return tuple(sorted(df.columns.tolist()))


def load_data(file_info: dict):
    """
    Load data from a file based on its format and compression.
    """
    import pandas as pd

    filepath = file_info['path']
    format_type = file_info['format']
    compression = file_info['compression']

    if format_type == 'csv':
        if compression:
            df = pd.read_csv(filepath, compression=compression, keep_default_na=False)
        else:
            df = pd.read_csv(filepath, keep_default_na=False)
        df = df.replace('', pd.NA)

    elif format_type == 'parquet':
        df = pd.read_parquet(filepath)

    elif format_type == 'tsv':
        if compression:
            df = pd.read_csv(filepath, compression=compression, sep='\t', keep_default_na=False)
        else:
            df = pd.read_csv(filepath, sep='\t', keep_default_na=False)
        df = df.replace('', pd.NA)

    elif format_type == 'json':
        if compression:
            import gzip
            import bz2
            if compression == '.gz':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    data = json.load(f)
        else:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
        # Handle both list of records and dict with arrays
        if isinstance(data, list):
            df = pd.DataFrame(data)
        elif isinstance(data, dict):
            # Check if values are lists (array format)
            if data and all(isinstance(v, list) for v in data.values()):
                df = pd.DataFrame(data)
            else:
                df = pd.json_normalize(data)
        else:
            df = pd.DataFrame([data])

    elif format_type == 'jsonl':
        if compression:
            df = pd.read_json(filepath, compression=compression, lines=True)
        else:
            df = pd.read_json(filepath, lines=True)

    else:
        raise ValueError(f"Unsupported format: {format_type}")

    # Normalize column names (strip whitespace)
    df.columns = df.columns.str.strip()

    # Try to convert columns to appropriate numeric types
    for col in df.columns:
        numeric_series = pd.to_numeric(df[col], errors='coerce')
        if numeric_series.notna().sum() > numeric_series.isna().sum():
            df[col] = numeric_series

    return df


def load_sharded_table(glob_pattern: str) -> 'pd.DataFrame':
    """
    Load and concatenate all files matching a glob pattern into a single DataFrame.
    Files are sorted lexicographically by path before concatenation.
    Returns an empty DataFrame (with no columns) if no files match the pattern.
    """
    import pandas as pd

    # Expand glob pattern and sort lexicographically
    filepaths = sorted(glob.glob(glob_pattern))

    if not filepaths:
        # Return empty DataFrame with no schema (as specified)
        return pd.DataFrame()

    dataframes = []
    for filepath in filepaths:
        # Get file info for loading
        _, format_type, compression = get_file_info(filepath)
        if format_type is None:
            # Skip unsupported files
            continue
        file_info = {
            'path': filepath,
            'format': format_type,
            'compression': compression,
        }
        df = load_data(file_info)
        dataframes.append(df)

    if not dataframes:
        return pd.DataFrame()

    # Concatenate all DataFrames
    return pd.concat(dataframes, ignore_index=True)


def parse_sql_query(sql_input: str) -> str:
    """
    Parse SQL input: if it's a file path, read from it; otherwise treat as raw SQL.
    Returns the SQL query string.
    """
    p = Path(sql_input)
    if p.exists():
        with open(p, 'r', encoding='utf-8') as f:
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


def execute_query(data_dir: str, sql_query: str, sharded_tables: dict = None) -> 'pd.DataFrame':
    """
    Execute a SQL query on files in the data directory.
    Returns a pandas DataFrame with the result.
    """
    import pandas as pd
    import duckdb

    # Discover files
    table_map = discover_files(data_dir)
    table_names = list(table_map.keys())

    # Add sharded tables if specified
    if sharded_tables:
        for table_name, glob_pattern in sharded_tables.items():
            df = load_sharded_table(glob_pattern)
            if not df.empty:
                table_map[table_name] = df
                table_names.append(table_name)

    # Load all files into DataFrames and register with DuckDB
    con = duckdb.connect()

    for table_name, table_data in table_map.items():
        # Handle both file paths and DataFrames
        if isinstance(table_data, str):
            # Load from file
            filepath = table_data
            _, format_type, compression = get_file_info(filepath)
            file_info = {
                'path': filepath,
                'format': format_type,
                'compression': compression,
            }
            df = load_data(file_info)
        else:
            # Already a DataFrame (sharded table)
            df = table_data

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
    except duckdb.ValidationException as e:
        error_msg = str(e).lower()

        # Detect and handle window function errors
        if 'window' in error_msg or 'over' in error_msg:
            # Parse the specific error type
            if 'nested' in error_msg or 'aggregate in window' in error_msg:
                error_type = 'NESTED_WINDOW_ERROR'
                raise RuntimeError(create_error(error_type, str(e)))
            elif 'frame' in error_msg:
                error_type = 'FRAME_ERROR'
                raise RuntimeError(create_error(error_type, str(e)))
            else:
                error_type = 'INVALID_WINDOW_SPEC'
                raise RuntimeError(create_error(error_type, str(e)))
        elif 'partition' in error_msg and 'order' in error_msg:
            error_type = 'INVALID_WINDOW_SPEC'
            raise RuntimeError(create_error(error_type, str(e)))
        else:
            raise RuntimeError(f"SQL execution error: {e}")
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
            return "|\n|---|---|\n"

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
        description="Execute read-only SQL queries on data files (CSV, Parquet, TSV, JSON, JSONL).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python query_files.py --data ./data --sql "SELECT * FROM users LIMIT 10"
  python query_files.py --data ./data --sql queries.sql --out result.csv
  python query_files.py --data ./data --sql "SELECT country, COUNT(*) FROM users GROUP BY country"

Supported formats:
  - CSV (.csv)
  - Parquet (.parquet)
  - TSV (.tsv)
  - JSON (.json)
  - JSONL (.jsonl)

Compressed files (.gz, .bz2) are also supported for all formats.
"""
    )
    parser.add_argument(
        "--data",
        required=True,
        help="Directory containing data files (can be nested)"
    )
    parser.add_argument(
        "--sql",
        required=True,
        help="SQL query string or path to SQL file"
    )
    parser.add_argument(
        "--sharded",
        action="append",
        dest="sharded_tables",
        metavar="<table_name>=<glob path>",
        help="Define a sharded table by merging files matching a glob pattern "
             "(can be specified multiple times)."
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
