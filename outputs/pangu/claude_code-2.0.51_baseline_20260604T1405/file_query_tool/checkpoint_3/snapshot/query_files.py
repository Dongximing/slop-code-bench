#!/usr/bin/env python3
"""Lightweight CLI for running read-only SQL queries across CSV files."""

import argparse
import glob
import os
import re
import sys
from pathlib import Path
from typing import Optional, List, Dict, Any
import pandas as pd
import numpy as np


class DataLoader:
    """Load and manage data files from a directory supporting multiple formats."""

    def __init__(self, data_dir: str = None):
        self.data_dir = Path(data_dir) if data_dir else None
        self.tables = {}
        if self.data_dir:
            self._load_all_files()

    def _load_file(self, filepath: Path) -> pd.DataFrame:
        """Load a single file based on its extension."""
        ext = filepath.suffix.lower()

        try:
            if ext == '.csv':
                df = pd.read_csv(filepath, dtype=str)
            elif ext == '.jsonl':
                df = pd.read_json(filepath, lines=True, dtype=str)
            elif ext == '.parquet':
                df = pd.read_parquet(filepath, dtype=str)
            else:
                raise ValueError(f"Unsupported file format: {ext}")

            # Clean the dataframe - replace empty strings and 'NULL' with NaN
            df = df.replace(r'^\s*$', np.nan, regex=True)
            df = df.replace('NULL', np.nan)
            return df
        except Exception as e:
            raise ValueError(f"Could not load {filepath}: {e}")

    def _load_all_files(self):
        """Load all supported files recursively from the data directory."""
        if not self.data_dir:
            return

        supported_extensions = ['*.csv', '*.jsonl', '*.parquet']
        for pattern in supported_extensions:
            for filepath in self.data_dir.rglob(pattern):
                rel_path = filepath.relative_to(self.data_dir)
                table_name = str(rel_path.with_suffix(""))
                table_name = table_name.replace(os.sep, ".").replace(".", "_")

                try:
                    df = self._load_file(filepath)
                    self.tables[table_name] = df
                except Exception as e:
                    print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)

    def add_sharded_table(self, table_name: str, glob_pattern: str):
        """Add a sharded table by matching files using a glob pattern."""
        # Expand the glob pattern (supports both absolute and relative paths)
        search_path = glob_pattern

        matched_files = sorted(glob.glob(search_path))

        if not matched_files:
            # Glob matching zero files produces an empty table with no schema
            self.tables[table_name] = pd.DataFrame()
            return

        # Load and concatenate all matched files in lexicographic order
        dataframes = []
        for filepath in matched_files:
            try:
                df = self._load_file(Path(filepath))
                if not df.empty:
                    dataframes.append(df)
            except Exception as e:
                print(f"Warning: Could not load shard file {filepath}: {e}", file=sys.stderr)

        if dataframes:
            # Concatenate all dataframes
            combined_df = pd.concat(dataframes, ignore_index=True, sort=False)
            self.tables[table_name] = combined_df
        else:
            # All files failed to load or were empty - produce empty table
            self.tables[table_name] = pd.DataFrame()

    def get_table(self, name: str) -> pd.DataFrame:
        """Get a table by name (case-sensitive)."""
        if name not in self.tables:
            raise ValueError(f"Table '{name}' not found. Available tables: {list(self.tables.keys())}")
        return self.tables[name]


class SQLQueryEngine:
    """Execute simplified SQL queries on CSV data."""

    def __init__(self, data_loader: DataLoader):
        self.loader = data_loader

    def execute(self, sql: str) -> pd.DataFrame:
        """Execute a SQL query and return result DataFrame."""
        sql = sql.strip()
        if not sql.upper().startswith("SELECT"):
            raise ValueError("Query must start with SELECT")

        sql = sql.rstrip(';')
        parsed = self._parse_query(sql)
        return self._execute_parsed(parsed)

    def _parse_query(self, sql: str) -> Dict:
        """Parse the complete query into structured components."""
        result = {
            'select': '',
            'tables': [],
            'where': None,
            'group_by': None,
            'having': None,
            'order_by': None,
            'limit': None,
            'offset': None,
            'distinct': False
        }

        # Extract SELECT clause
        select_match = re.match(r'SELECT\s+', sql, re.IGNORECASE)
        if not select_match:
            raise ValueError("No SELECT clause found")

        start = select_match.end()

        # Check for DISTINCT
        rest_after_select = sql[start:]
        if rest_after_select.upper().startswith('DISTINCT'):
            result['distinct'] = True
            # Move past DISTINCT
            distinct_match = re.match(r'DISTINCT\s+', rest_after_select, re.IGNORECASE)
            start += distinct_match.end()
            rest_after_select = rest_after_select[distinct_match.end():]

        # Find FROM
        from_match = re.search(r'\s+FROM\s+', rest_after_select, re.IGNORECASE)
        if not from_match:
            raise ValueError("No FROM clause found")

        select_end = from_match.start()
        result['select'] = rest_after_select[:select_end].strip()

        # Get remaining after FROM
        after_from = rest_after_select[from_match.end():]

        # Parse FROM table reference (could include JOINs)
        from_part, rest = self._split_at_next_clause(after_from)
        result['tables'] = self._parse_table_references(from_part)

        # Parse remaining clauses
        if rest:
            self._parse_remaining_clauses(rest, result)

        return result

    def _split_at_next_clause(self, sql: str) -> tuple:
        """Split SQL at the next clause keyword."""
        clauses = ['WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT', 'OFFSET']

        for clause in clauses:
            pattern = rf'\s+{clause}\s+'
            match = re.search(pattern, sql, re.IGNORECASE)
            if match:
                return sql[:match.start()].strip(), sql[match.start():].strip()

        return sql.strip(), ''

    def _parse_table_references(self, from_part: str) -> List[Dict]:
        """Parse FROM and JOIN clauses into table references."""
        tables = []
        from_part_upper = from_part.upper()

        # Detect all JOIN types
        join_positions = []
        join_types = []

        # Find all JOIN positions and types
        temp = from_part
        while True:
            join_match = re.search(r'\s+(INNER|LEFT|RIGHT|FULL)\s+JOIN\s+', temp, re.IGNORECASE)
            if not join_match:
                break
            join_types.append(join_match.group(1).upper())
            join_positions.append((join_match.start(), join_match.end()))
            temp = temp[join_match.end():]

        # Split by JOIN keywords
        join_pattern = re.compile(r'\s+(?:INNER|LEFT|RIGHT|FULL)?\s*JOIN\s+', re.IGNORECASE)
        parts = join_pattern.split(from_part)
        join_matches = list(join_pattern.finditer(from_part))

        # First part is the main (left) table
        if parts:
            main_table = parts[0].strip()
            table_info = self._parse_table_spec(main_table)
            tables.append({**table_info, 'join_type': None, 'on_predicate': None})

            # Parse JOINed tables
            for i, (jpart, jmatch) in enumerate(zip(parts[1:], join_matches)):
                join_part = jpart.strip()
                join_type = join_types[i] if i < len(join_types) else 'INNER'

                # Check for ON clause
                on_match = re.search(r'\s+ON\s+', join_part, re.IGNORECASE)
                if on_match:
                    table_part = join_part[:on_match.start()].strip()
                    on_predicate = join_part[on_match.end():].strip()
                else:
                    table_part = join_part
                    on_predicate = None

                table_info = self._parse_table_spec(table_part)
                tables.append({**table_info, 'join_type': join_type, 'on_predicate': on_predicate})

        return tables

    def _parse_table_spec(self, spec: str) -> Dict:
        """Parse a table reference with optional alias."""
        # Match: table_name [AS alias]
        match = re.match(r'([\w.]+)(?:\s+AS\s+(\w+))?', spec, re.IGNORECASE)
        if match:
            return {
                'name': match.group(1),
                'alias': match.group(2) if match.group(2) else None
            }
        return {'name': spec, 'alias': None}

    def _parse_remaining_clauses(self, rest: str, result: Dict):
        """Parse remaining SQL clauses."""
        clauses_order = ['WHERE', 'GROUP BY', 'HAVING', 'ORDER BY', 'LIMIT', 'OFFSET']

        for clause in clauses_order:
            pattern = rf'{clause}\s+'
            match = re.search(pattern, rest, re.IGNORECASE)
            if not match:
                continue

            clause_start = match.end()

            # Find end of this clause
            next_pos = len(rest)
            for next_clause in clauses_order:
                if next_clause == clause:
                    continue
                next_match = re.search(rf'\s+{next_clause}\s+', rest[clause_start:], re.IGNORECASE)
                if next_match:
                    next_pos = min(next_pos, clause_start + next_match.start())

            value = rest[clause_start:next_pos].strip()

            if clause == 'WHERE':
                result['where'] = value
            elif clause == 'GROUP BY':
                result['group_by'] = [c.strip() for c in value.split(',')]
            elif clause == 'HAVING':
                result['having'] = value
            elif clause == 'ORDER BY':
                result['order_by'] = value
            elif clause == 'LIMIT':
                try:
                    result['limit'] = int(value)
                except ValueError:
                    raise ValueError(f"Invalid LIMIT value: {value}")
            elif clause == 'OFFSET':
                try:
                    result['offset'] = int(value)
                except ValueError:
                    raise ValueError(f"Invalid OFFSET value: {value}")

    def _execute_parsed(self, parsed: Dict) -> pd.DataFrame:
        """Execute a parsed query."""
        # Load all tables
        tables_data = []
        table_aliases = {}

        for tbl in parsed['tables']:
            df = self.loader.get_table(tbl['name'])
            df = df.copy()
            tables_data.append(df)

            alias = tbl.get('alias')
            if alias:
                table_aliases[alias.upper()] = tbl['name']
            else:
                table_aliases[tbl['name'].upper()] = tbl['name']

        # Handle JOINs if present
        merged_df = self._merge_tables(tables_data, parsed['tables']) if len(parsed['tables']) > 1 else tables_data[0]

        # Apply WHERE clause
        if parsed.get('where'):
            merged_df = self._apply_where(merged_df, parsed['where'], table_aliases)

        # Parse SELECT items
        select_items, _ = self._parse_select_items(parsed['select'])

        # Handle GROUP BY
        group_cols = parsed.get('group_by')
        if group_cols:
            # Resolve group by columns
            resolved_group_cols = self._resolve_columns(group_cols, merged_df)
            result_df = self._apply_aggregations(merged_df, select_items, resolved_group_cols, table_aliases)
        elif any(item.get('type') == 'aggregate' for item in select_items):
            result_df = self._apply_aggregations(merged_df, select_items, None, table_aliases)
        else:
            result_df = self._apply_select(merged_df, select_items)

        # Apply HAVING
        if parsed.get('having'):
            result_df = self._apply_having(result_df, parsed['having'])

        # Apply ORDER BY
        if parsed.get('order_by'):
            result_df = self._apply_order_by(result_df, parsed['order_by'], select_items)

        # Apply LIMIT
        if parsed.get('limit') is not None:
            result_df = result_df.head(parsed['limit'])

        # Apply OFFSET
        if parsed.get('offset') is not None:
            offset = parsed['offset']
            result_df = result_df.iloc[offset:] if len(result_df) > offset else result_df.iloc[0:0]

        return result_df

    def _merge_tables(self, tables: List[pd.DataFrame], table_refs: List[Dict]) -> pd.DataFrame:
        """Merge multiple DataFrames using JOIN operations."""
        result = tables[0]

        for i, tbl in enumerate(table_refs[1:], 1):
            right_df = tables[i]

            if not tbl.get('on_predicate'):
                raise ValueError(f"Missing ON clause for JOIN on table {tbl['name']}")

            # Parse ON condition
            on_predicate = tbl['on_predicate'].strip()
            if '=' not in on_predicate:
                raise ValueError(f"Unsupported ON condition: {on_predicate}")

            left_col, right_col = on_predicate.split('=', 1)
            left_col = left_col.strip().upper()
            right_col = right_col.strip().upper()

            # Resolve column names in DataFrames
            left_col_resolved = self._find_column(result, left_col)
            right_col_resolved = self._find_column(right_df, right_col)

            join_type = (tbl.get('join_type') or 'INNER').lower()

            # Perform join
            result = pd.merge(
                result, right_df,
                left_on=left_col_resolved,
                right_on=right_col_resolved,
                how=join_type
            )

        return result

    def _find_column(self, df: pd.DataFrame, col_name: str) -> str:
        """Find column in DataFrame, case-insensitively. Handles table-prefixed column names."""
        if df.empty or len(df.columns) == 0:
            # For empty tables, return the column name as-is
            return col_name.strip()

        # Strip table prefix (e.g., 'p.id' -> 'id')
        col_name = col_name.strip()
        if '.' in col_name:
            col_name = col_name.split('.')[-1]

        col_name_upper = col_name.upper()
        for c in df.columns:
            if c.upper() == col_name_upper:
                return c
        return col_name

    def _apply_where(self, df: pd.DataFrame, where_clause: str, table_aliases: Dict) -> pd.DataFrame:
        """Apply WHERE clause using pandas query."""
        try:
            query_expr = self._sql_to_pandas_expr(where_clause, df)
            return df.query(query_expr)
        except Exception as e:
            raise ValueError(f"Error in WHERE clause: {e}")

    def _sql_to_pandas_expr(self, sql_expr: str, df: pd.DataFrame) -> str:
        """Convert SQL WHERE expression to pandas query syntax."""
        expr = sql_expr.strip()

        # Tokenize - need to replace column references with proper column names
        # Handle comparisons first
        expr = re.sub(r'!=', '!=', expr)
        expr = re.sub(r'=\s*(?!\s*=)', '==', expr)
        expr = re.sub(r'\b(<|>|<=|>=)\b', r'\1', expr)

        # Handle LIKE with double quotes for pattern (inside a double-quoted replacement)
        def replace_like(m):
            col_full = m.group(1)
            pattern = m.group(2)
            pandas_pattern = pattern.replace('%', '.*')
            # Strip table prefix if any
            col_name = col_full.split('.')[-1]
            return f'{col_name}.str.contains("{pandas_pattern}", na=False, regex=True)'

        # Match LIKE with single-quoted pattern
        expr = re.sub(r"(\w+(?:\.\w+)*)\s+LIKE\s+'([^']*)'", replace_like, expr)
        # Match LIKE with double-quoted pattern
        expr = re.sub(r'(\w+(\.\w+)*)\s+LIKE\s+"([^"]*)"', replace_like, expr)

        # Handle boolean operators
        expr = expr.replace(' AND ', ' and ').replace(' OR ', ' or ').replace(' NOT ', ' not ')

        # Remove parentheses
        expr = expr.replace('(', '').replace(')', '')

        return expr

    def _parse_select_items(self, select_str: str) -> tuple:
        """Parse SELECT clause items."""
        items = []
        distinct = 'DISTINCT' in select_str.upper()

        # Remove DISTINCT keyword if present
        select_clean = re.sub(r'^\s*DISTINCT\s+', '', select_str, flags=re.IGNORECASE)
        parts = [p.strip() for p in select_clean.split(',') if p.strip()]

        for part in parts:
            if part == '*':
                items.append({'type': 'wildcard'})
                continue

            # Check for aggregate functions
            agg_match = re.match(
                r'(COUNT|SUM|MIN|MAX)\s*\(([^)]+)\)\s*(?:AS\s+(\w+))?',
                part, re.IGNORECASE
            )
            if agg_match:
                func = agg_match.group(1).upper()
                args_str = agg_match.group(2).strip()
                alias = agg_match.group(3)

                if args_str == '*':
                    args = ['*']
                else:
                    args = [a.strip() for a in args_str.split(',')]

                items.append({
                    'type': 'aggregate',
                    'function': func,
                    'args': args,
                    'alias': alias
                })
                continue

            # Check for AS alias
            as_match = re.match(r'([\w.]+)\s+AS\s+(\w+)', part, re.IGNORECASE)
            if as_match:
                items.append({
                    'type': 'column',
                    'name': as_match.group(1),
                    'alias': as_match.group(2)
                })
                continue

            # Check for implicit alias
            implicit_match = re.match(r'([\w.]+)\s+(\w+)$', part, re.IGNORECASE)
            if implicit_match:
                items.append({
                    'type': 'column',
                    'name': implicit_match.group(1),
                    'alias': implicit_match.group(2)
                })
                continue

            # Plain column reference
            items.append({'type': 'column', 'name': part})

        return items, distinct

    def _resolve_columns(self, cols: List[str], df: pd.DataFrame) -> List[str]:
        """Resolve column names considering case-insensitivity. Handles table-prefixed names."""
        resolved = []
        for col in cols:
            # Strip table prefix (e.g., 'p.id' -> 'id')
            col = col.strip()
            if '.' in col:
                col = col.split('.')[-1]

            found = None
            for c in df.columns:
                if c.upper() == col.upper():
                    found = c
                    break
            resolved.append(found if found else col)
        return resolved

    def _apply_select(self, df: pd.DataFrame, select_items: List[Dict]) -> pd.DataFrame:
        """Apply SELECT clause."""
        result_cols = {}
        final_order = []

        for item in select_items:
            if item['type'] == 'wildcard':
                for col in df.columns:
                    result_cols[col] = df[col].values
                    final_order.append(col)
                continue

            col_name = item['name']
            alias = item.get('alias')

            # Find matching column
            matched = None
            for c in df.columns:
                if c.upper() == col_name.upper():
                    matched = c
                    break

            output_name = alias if alias else col_name

            if matched:
                result_cols[output_name] = df[matched].values
            else:
                result_cols[output_name] = [None] * len(df)

            final_order.append(output_name)

        return pd.DataFrame(result_cols, columns=final_order)

    def _apply_aggregations(
        self,
        df: pd.DataFrame,
        select_items: List[Dict],
        group_cols: Optional[List[str]],
        table_aliases: Dict
    ) -> pd.DataFrame:
        """Apply aggregation functions with GROUP BY support."""

        # Build aggregation mapping for pandas
        agg_mapping = {}
        has_count_star = False
        non_agg_select = []

        for item in select_items:
            if item['type'] == 'aggregate':
                func = item['function'].lower()

                for arg in item['args']:
                    if arg == '*':
                        has_count_star = True
                        continue

                    # Strip table prefix (e.g., 'p.id' -> 'id')
                    arg_clean = arg.strip()
                    if '.' in arg_clean:
                        arg_clean = arg_clean.split('.')[-1]

                    # Find matching column
                    matched_col = None
                    for c in df.columns:
                        if c.upper() == arg_clean.upper():
                            matched_col = c
                            break

                    if matched_col:
                        agg_mapping[matched_col] = func
            elif item['type'] == 'column':
                non_agg_select.append(item)

        # Perform aggregation
        if group_cols:
            # GROUP BY with aggregations
            groupby_valid = [c for c in group_cols if c in df.columns]
            if not groupby_valid and df.columns.tolist():
                # No valid group columns, just return first row
                result_df = df.head(1)
            else:
                result_df = df.groupby(groupby_valid, dropna=False).agg(agg_mapping).reset_index()
        elif agg_mapping:
            # Aggregation without GROUP BY - single result row
            result_df = df.agg(agg_mapping).to_frame().T
        elif has_count_star:
            # COUNT(*) without GROUP BY
            result_df = pd.DataFrame({'_count_star': [len(df)]})
        else:
            result_df = df.head(1)

        # Build final result with proper column naming
        result_data = {}
        final_columns = []

        # Add group columns first
        if group_cols:
            for gc in group_cols:
                # Strip table prefix for column lookup (e.g., 'p.id' -> 'id')
                gc_lookup = gc.strip()
                if '.' in gc_lookup:
                    gc_lookup = gc_lookup.split('.')[-1]

                if gc_lookup in result_df.columns:
                    result_data[gc_lookup] = result_df[gc_lookup].values
                    final_columns.append(gc_lookup)
                else:
                    # Fall back to original if stripped version not found
                    result_data[gc] = [None] * len(result_df)
                    final_columns.append(gc)

        # Process SELECT items
        for item in select_items:
            if item['type'] == 'wildcard':
                for col in result_df.columns:
                    if col not in final_columns:
                        col_name = item.get('alias') if item.get('alias') else col
                        result_data[col_name] = result_df[col].values
                        final_columns.append(col_name)
                continue

            if item['type'] == 'column':
                output_name = item.get('alias', item['name'])

                # Strip table prefix for column lookup (e.g., 'p.id' -> 'id')
                col_lookup = item['name'].strip()
                if '.' in col_lookup:
                    col_lookup = col_lookup.split('.')[-1]

                # Find column in result DataFrame
                col_found = None
                for c in result_df.columns:
                    if c.upper() == col_lookup.upper():
                        col_found = c
                        break

                if col_found:
                    result_data[output_name] = result_df[col_found].values
                elif has_count_star and col_lookup.upper() == '_COUNT_STAR':
                    result_data[output_name] = result_df['_count_star'].values
                else:
                    result_data[output_name] = [None] * len(result_df)

                final_columns.append(output_name)

            elif item['type'] == 'aggregate':
                func = item['function'].lower()

                for arg in item['args']:
                    if arg == '*':
                        output_name = item.get('alias', 'count')

                        if '_count_star' in result_df.columns:
                            result_data[output_name] = result_df['_count_star'].values
                        elif len(result_df) == 1:
                            # Get count from the aggregation result
                            # The count column should be the first column or named 'count'
                            count_cols = [c for c in result_df.columns if 'count' in c.lower()]
                            if count_cols:
                                result_data[output_name] = result_df[count_cols[0]].values
                        else:
                            result_data[output_name] = [None] * len(result_df)

                        if output_name not in final_columns:
                            final_columns.append(output_name)
                        break

        return pd.DataFrame(result_data, columns=final_columns)

    def _apply_having(self, df: pd.DataFrame, having_clause: str) -> pd.DataFrame:
        """Apply HAVING clause."""
        try:
            query_expr = self._sql_to_pandas_expr(having_clause, df)
            return df.query(query_expr)
        except Exception as e:
            raise ValueError(f"Error in HAVING clause: {e}")

    def _apply_order_by(self, df: pd.DataFrame, order_by_str: str, select_items: List[Dict]) -> pd.DataFrame:
        """Apply ORDER BY clause."""
        if df.empty:
            return df

        sort_specs = []

        for part in order_by_str.split(','):
            part = part.strip()
            if not part:
                continue

            ascending = True
            col_name = part

            if part.upper().endswith(' DESC'):
                ascending = False
                col_name = part[:-5].strip()
            elif part.upper().endswith(' ASC'):
                ascending = True
                col_name = part[:-4].strip()

            col_name = col_name.strip("'\"")
            sort_specs.append((col_name, ascending))

        if not sort_specs:
            return df

        sort_cols = []
        ascending_list = []

        for col_name, ascending in sort_specs:
            # Find matching column in result_df
            found = None
            for c in df.columns:
                if c.upper() == col_name.upper():
                    found = c
                    break

            if found:
                sort_cols.append(found)
                ascending_list.append(ascending)

        if sort_cols:
            df = df.sort_values(by=sort_cols, ascending=ascending_list)

        return df


def format_markdown_table(df: pd.DataFrame) -> str:
    """Format DataFrame as a markdown table."""
    if df.empty:
        return "| No results |\n|------------|\n|            |\n"

    # Handle NULL values
    df_display = df.copy()
    for col in df_display.columns:
        df_display[col] = df_display[col].apply(lambda x: '' if pd.isna(x) else str(x))

    # Build header and separator
    header = "| " + " | ".join(str(c) for c in df_display.columns) + " |"
    separator = "| " + " | ".join(["---"] * len(df_display.columns)) + " |"

    # Build data rows
    rows = []
    for _, row in df_display.iterrows():
        row_str = "| " + " | ".join(str(v) for v in row.values) + " |"
        rows.append(row_str)

    return "\n".join([header, separator] + rows) + "\n"


def write_csv(df: pd.DataFrame, path: str):
    """Write DataFrame to CSV file."""
    if df.empty:
        return

    df_output = df.copy()
    for col in df_output.columns:
        df_output[col] = df_output[col].apply(lambda x: '' if pd.isna(x) else str(x))

    df_output.to_csv(path, index=False)


def parse_sql_input(sql_input: str) -> str:
    """Parse SQL input - either raw SQL or path to SQL file."""
    sql_input = sql_input.strip()

    if os.path.isfile(sql_input):
        with open(sql_input, 'r') as f:
            return f.read().strip()

    return sql_input


def main():
    parser = argparse.ArgumentParser(
        description="Run read-only SQL queries on CSV/JSONL/Parquet files."
    )
    parser.add_argument(
        '--data',
        required=True,
        help='Directory containing data files (can be nested)'
    )
    parser.add_argument(
        '--sql',
        required=True,
        help='Raw SQL query or path to SQL file'
    )
    parser.add_argument(
        '--sharded',
        action='append',
        default=[],
        metavar='<table_name>=<glob path>',
        help='Sharded table definition: table_name=glob_pattern (can be specified multiple times)'
    )
    parser.add_argument(
        '--out',
        required=False,
        help='Path to output CSV file (optional)'
    )

    args = parser.parse_args()

    # Validate data directory
    if not os.path.isdir(args.data):
        print(f"Error: Data directory not found: {args.data}", file=sys.stderr)
        sys.exit(1)

    # Parse SQL input
    try:
        sql = parse_sql_input(args.sql)
    except FileNotFoundError:
        print(f"Error: SQL file not found: {args.sql}", file=sys.stderr)
        sys.exit(1)

    # Load data
    try:
        loader = DataLoader(args.data)

        # Process sharded tables
        for sharded_spec in args.sharded:
            if '=' not in sharded_spec:
                print(f"Warning: Invalid sharded spec format: {sharded_spec}. Expected: table_name=glob_pattern", file=sys.stderr)
                continue

            table_name, glob_pattern = sharded_spec.split('=', 1)
            table_name = table_name.strip()
            glob_pattern = glob_pattern.strip()

            if not table_name or not glob_pattern:
                print(f"Warning: Invalid sharded spec: {sharded_spec}", file=sys.stderr)
                continue

            loader.add_sharded_table(table_name, glob_pattern)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)

    # Execute query
    try:
        engine = SQLQueryEngine(loader)
        result_df = engine.execute(sql)

        # Output to STDOUT as markdown table
        print(format_markdown_table(result_df))

        # Write to output file if specified
        if args.out:
            write_csv(result_df, args.out)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
