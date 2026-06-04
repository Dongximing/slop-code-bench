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
                # Use keep_default_na=False to avoid pandas interpreting common strings like "NA" as NaN
                df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
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

    def _parse_window_specification(self, window_str: str) -> Dict:
        """Parse OVER() window specification."""
        window_str = window_str.strip()
        if not window_str.startswith('(') or not window_str.endswith(')'):
            raise ValueError(f"INVALID_WINDOW_SPEC: Missing parentheses in window specification: {window_str}")

        window_str = window_str[1:-1].strip()  # Remove parentheses

        result = {
            'partition_by': [],
            'order_by': [],
            'frame_clause': None,
            'frame_type': None,  # 'ROWS' or 'RANGE'
            'frame_start': None,
            'frame_between_end': None
        }

        if not window_str:
            # Empty OVER() means window over all rows
            return result

        # Parse PARTITION BY
        partition_match = re.search(r'PARTITION\s+BY\s+', window_str, re.IGNORECASE)
        if partition_match:
            # Extract partition by clause
            start = partition_match.end()
            # Find next keyword or end
            next_match = re.search(r'\s+(ORDER\s+BY|ROWS|RANGE|ORDER BY)', window_str[start:], re.IGNORECASE)
            if next_match:
                partition_str = window_str[start:start + next_match.start()].strip()
            else:
                partition_str = window_str[start:].strip()
            result['partition_by'] = [c.strip() for c in partition_str.split(',') if c.strip()]

            # Remove PARTITION BY part from window_str for further parsing
            window_str = window_str[:partition_match.start()] + window_str[start + len(partition_str):]
            window_str = window_str.strip()

        # Parse ORDER BY
        order_match = re.search(r'ORDER\s+BY\s+', window_str, re.IGNORECASE)
        if order_match:
            start = order_match.end()
            # Find next keyword or end
            next_match = re.search(r'\s+(ROWS|RANGE|ORDER BY)', window_str[start:], re.IGNORECASE)
            if next_match:
                order_str = window_str[start:start + next_match.start()].strip()
            else:
                order_str = window_str[start:].strip()

            # Parse ORDER BY items (col [ASC|DESC])
            order_items = []
            for item in order_str.split(','):
                item = item.strip()
                if not item:
                    continue
                # Check for ASC/DESC
                ascending = True
                item_clean = item
                if item.upper().endswith(' DESC'):
                    ascending = False
                    item_clean = item[:-5].strip()
                elif item.upper().endswith(' ASC'):
                    ascending = True
                    item_clean = item[:-4].strip()
                order_items.append({'column': item_clean.strip(), 'ascending': ascending})

            result['order_by'] = order_items

            # Remove ORDER BY part
            window_str = window_str[:order_match.start()] + window_str[start + len(order_str):]
            window_str = window_str.strip()

        # Parse frame clause
        if window_str:
            frame_match = re.match(r'(ROWS|RANGE)\s+(BETWEEN\s+)?(.*)', window_str, re.IGNORECASE)
            if frame_match:
                frame_type = frame_match.group(1).upper()
                has_between = frame_match.group(2)
                frame_body = frame_match.group(3).strip()

                result['frame_type'] = frame_type

                if has_between:
                    # Parse BETWEEN ... AND ...
                    between_match = re.match(r'UNBOUNDED\s+PRECEDING\s+AND\s+(CURRENT\s+ROW|UNBOUNDED\s+FOLLOWING)', frame_body, re.IGNORECASE)
                    if between_match:
                        result['frame_clause'] = f"BETWEEN UNBOUNDED PRECEDING AND {between_match.group(1).upper()}"
                        result['frame_between_end'] = 'CURRENT_ROW' if 'CURRENT ROW' in between_match.group(1).upper() else 'UNBOUNDED_FOLLOWING'
                    else:
                        # Try CURRENT ROW ending
                        curr_match = re.match(r'CURRENT\s+ROW\s+AND\s+(CURRENT\s+ROW|UNBOUNDED\s+FOLLOWING)', frame_body, re.IGNORECASE)
                        if curr_match:
                            result['frame_clause'] = f"BETWEEN CURRENT ROW AND {curr_match.group(1).upper()}"
                        else:
                            raise ValueError(f"FRAME_ERROR: Unsupported frame clause: {window_str}")
                else:
                    # Simple frame: UNBOUNDED PRECEDING or CURRENT ROW
                    if frame_body.upper() == 'UNBOUNDED PRECEDING':
                        result['frame_clause'] = 'UNBOUNDED PRECEDING'
                        result['frame_start'] = 'UNBOUNDED_PRECEDING'
                    elif frame_body.upper() == 'CURRENT ROW':
                        result['frame_clause'] = 'CURRENT ROW'
                        result['frame_start'] = 'CURRENT_ROW'
                    else:
                        raise ValueError(f"FRAME_ERROR: Unsupported frame clause: {window_str}")

        return result

    def _parse_window_function(self, part: str) -> Optional[Dict]:
        """Parse a window function like SUM(x) OVER (...) or ROW_NUMBER() OVER (...)."""
        # First, check if the part contains OVER
        over_pos = part.upper().find('OVER')
        if over_pos == -1:
            return None

        # Get the content after OVER (including AS alias)
        after_over = part[over_pos + 4:].strip()
        if not after_over.startswith('('):
            return None

        # Parse parentheses to find the matching closing paren for OVER()
        paren_count = 0
        j = 0
        for j, char in enumerate(after_over):
            if char == '(':
                paren_count += 1
            elif char == ')':
                paren_count -= 1
                if paren_count == 0:
                    break
        else:
            # Unmatched parentheses
            return None

        # The window spec is between the outer parentheses
        window_spec_str = after_over[1:j].strip()

        # The rest after the closing paren could be AS alias
        after_spec = after_over[j+1:].strip()

        # The function part is before OVER
        func_part = part[:over_pos].strip()

        # Parse function name and args
        func_match = re.match(r'(COUNT|SUM|MIN|MAX|AVG|ROW_NUMBER|RANK|DENSE_RANK)\s*\(([^)]*)\)', func_part, re.IGNORECASE)
        if not func_match:
            return None

        func_name = func_match.group(1).upper()
        args_str = func_match.group(2).strip()

        # Parse arguments
        if args_str == '*':
            args = ['*']
        elif args_str:
            args = [a.strip() for a in args_str.split(',')]
        else:
            args = []

        # Parse window specification
        window_spec = self._parse_window_specification(f"({window_spec_str})")

        return {
            'type': 'window_function',
            'function': func_name,
            'args': args,
            'window_spec': window_spec,
            'alias': None  # Will be set later if AS alias is present
        }

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

            # Try to parse as window function first
            window_func = self._parse_window_function(part)
            if window_func:
                # Check for AS alias after the window function
                as_match = re.match(r'.+?\s+AS\s+(\w+)$', part, re.IGNORECASE)
                if as_match:
                    window_func['alias'] = as_match.group(1)
                items.append(window_func)
                continue

            # Check for aggregate functions (non-window)
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

        # Check if there are window functions
        has_window = any(item.get('type') == 'window_function' for item in select_items)

        # Handle GROUP BY (cannot be combined with window functions in the traditional sense)
        group_cols = parsed.get('group_by')
        if group_cols:
            # Resolve group by columns
            resolved_group_cols = self._resolve_columns(group_cols, merged_df)
            result_df = self._apply_aggregations(merged_df, select_items, resolved_group_cols, table_aliases)
        elif any(item.get('type') == 'aggregate' for item in select_items):
            result_df = self._apply_aggregations(merged_df, select_items, None, table_aliases)
        else:
            result_df = self._apply_select(merged_df, select_items)

        # Apply window functions if present (after aggregation/select but before ORDER BY)
        if has_window:
            result_df = self._apply_window_functions(result_df, select_items, merged_df)

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

    def _apply_window_functions(self, df: pd.DataFrame, select_items: List[Dict], base_df: pd.DataFrame) -> pd.DataFrame:
        """Apply window functions to the DataFrame."""
        result_df = df.copy()

        # Get window functions from select items
        window_funcs = [item for item in select_items if item.get('type') == 'window_function']

        for wfunc in window_funcs:
            func_name = wfunc['function']
            args = wfunc['args']
            window_spec = wfunc['window_spec']
            alias = wfunc.get('alias')

            # Get partition columns
            partition_cols = window_spec.get('partition_by', [])
            order_by_items = window_spec.get('order_by', [])
            frame_clause = window_spec.get('frame_clause')
            frame_type = window_spec.get('frame_type')  # 'ROWS' or 'RANGE'
            frame_start = window_spec.get('frame_start')
            frame_between_end = window_spec.get('frame_between_end')

            # Resolve column names in the DataFrame
            def resolve_arg(arg):
                """Resolve argument to actual column name in DataFrame."""
                if arg == '*':
                    return '*'
                arg_clean = arg.strip()
                if '.' in arg_clean:
                    arg_clean = arg_clean.split('.')[-1]

                # Find matching column
                for c in result_df.columns:
                    if c.upper() == arg_clean.upper():
                        return c
                # If not found, try to find in base_df for window frame references
                for c in base_df.columns:
                    if c.upper() == arg_clean.upper():
                        return c
                return arg_clean

            resolved_args = [resolve_arg(arg) for arg in args]

            # Resolve partition columns
            resolved_part_cols = []
            for pc in partition_cols:
                pc_clean = pc.strip()
                if '.' in pc_clean:
                    pc_clean = pc_clean.split('.')[-1]
                for c in result_df.columns:
                    if c.upper() == pc_clean.upper():
                        resolved_part_cols.append(c)
                        break
                else:
                    # Check base_df for partition columns
                    for c in base_df.columns:
                        if c.upper() == pc_clean.upper():
                            resolved_part_cols.append(c)
                            break
                    else:
                        resolved_part_cols.append(pc_clean)

            # Resolve order by columns
            resolved_order_cols = []
            for ob in order_by_items:
                ob_col = resolve_arg(ob['column'])
                resolved_order_cols.append({'column': ob_col, 'ascending': ob['ascending']})

            # Determine output column name
            output_name = alias if alias else self._generate_window_func_name(func_name, resolved_args)

            # Compute window function values
            window_values = self._compute_window_values(
                result_df,
                resolved_part_cols,
                resolved_order_cols,
                func_name,
                resolved_args,
                frame_type,
                frame_start,
                frame_between_end,
                base_df
            )

            result_df[output_name] = window_values

        return result_df

    def _generate_window_func_name(self, func_name: str, args: List[str]) -> str:
        """Generate a default name for a window function."""
        arg_str = '_'.join(args) if args else ''
        if arg_str:
            return f"{func_name.lower()}_{arg_str.lower()}"
        return f"{func_name.lower()}"

    def _compute_window_values(
        self,
        df: pd.DataFrame,
        partition_cols: List[str],
        order_by_cols: List[Dict],
        func_name: str,
        args: List[str],
        frame_type: Optional[str],
        frame_start: Optional[str],
        frame_between_end: Optional[str],
        base_df: pd.DataFrame
    ) -> List[Any]:
        """Compute window function values for each row."""

        if df.empty:
            return []

        # For RANKING functions (ROW_NUMBER, RANK, DENSE_RANK)
        if func_name in ('ROW_NUMBER', 'RANK', 'DENSE_RANK'):
            return self._compute_ranking_function(
                df, partition_cols, order_by_cols, func_name
            )

        # For AGGREGATE window functions (SUM, AVG, MIN, MAX, COUNT)
        return self._compute_aggregate_window_function(
            df, partition_cols, order_by_cols, func_name, args,
            frame_type, frame_start, frame_between_end, base_df
        )

    def _compute_ranking_function(
        self,
        df: pd.DataFrame,
        partition_cols: List[str],
        order_by_cols: List[Dict],
        func_name: str
    ) -> List[Any]:
        """Compute ranking window function values."""
        if not partition_cols:
            # No partition - treat entire DataFrame as one partition
            return self._compute_ranking_for_partition(df, order_by_cols, func_name, is_single_partition=True)

        # Group by partition columns
        grouped = df.groupby(partition_cols, dropna=False, sort=False)

        results = []
        for _, group_df in grouped:
            partition_results = self._compute_ranking_for_partition(
                group_df, order_by_cols, func_name, is_single_partition=False
            )
            results.extend(partition_results)

        # Reorder to match original order
        # Since groupby preserves order within groups but not across groups in older pandas,
        # we need to sort back by the original index
        temp_df = df.copy()
        temp_df['_window_result'] = results

        # Sort by original order - use index if available, but we don't have original index
        # Instead, we rely on groupby which preserves order within groups
        return temp_df['_window_result'].tolist()

    def _compute_ranking_for_partition(
        self,
        partition_df: pd.DataFrame,
        order_by_cols: List[Dict],
        func_name: str,
        is_single_partition: bool
    ) -> List[Any]:
        """Compute ranking for a single partition."""
        if partition_df.empty:
            return []

        # Sort the partition according to ORDER BY
        sort_cols = []
        ascending_list = []
        for ob in order_by_cols:
            sort_cols.append(ob['column'])
            ascending_list.append(ob['ascending'])

        # Handle case where sort column doesn't exist in partition - use position order
        existing_sort_cols = [c for c in sort_cols if c in partition_df.columns]

        if existing_sort_cols:
            sorted_df = partition_df.sort_values(
                by=existing_sort_cols,
                ascending=ascending_list[:len(existing_sort_cols)],
                na_position='last'  # NULLs sort after non-NULL values
            )
        else:
            # No sort columns, preserve original order
            sorted_df = partition_df.copy()

        # Reset index for row numbering
        sorted_df = sorted_df.reset_index(drop=True)

        # Compute ranking
        if func_name == 'ROW_NUMBER':
            # Assign sequential numbers (1, 2, 3, ...)
            return list(range(1, len(sorted_df) + 1))

        elif func_name == 'RANK':
            # Assign ranks with gaps for ties
            if not order_by_cols:
                # No ORDER BY, all rows have same rank = 1
                return [1] * len(sorted_df)

            # Get the primary sort column values
            primary_col = order_by_cols[0]['column']
            ascending = order_by_cols[0]['ascending']

            if primary_col not in sorted_df.columns:
                return [1] * len(sorted_df)

            values = sorted_df[primary_col]
            ranks = []
            current_rank = 1
            i = 0

            while i < len(values):
                # Find all rows with the same value (tie)
                current_val = values.iloc[i]
                tie_count = 0
                j = i
                while j < len(values):
                    if pd.isna(values.iloc[j]) and pd.isna(current_val):
                        # Both are NULL, consider equal
                        j += 1
                        tie_count += 1
                    elif not pd.isna(values.iloc[j]) and not pd.isna(current_val):
                        if values.iloc[j] == current_val:
                            j += 1
                            tie_count += 1
                        else:
                            break
                    else:
                        # One NULL, one not - they are different
                        break

                # Assign rank to all tied rows
                for _ in range(tie_count):
                    ranks.append(current_rank)

                current_rank += tie_count
                i = j

            return ranks

        elif func_name == 'DENSE_RANK':
            # Assign dense ranks without gaps
            if not order_by_cols:
                return [1] * len(sorted_df)

            primary_col = order_by_cols[0]['column']
            ascending = order_by_cols[0]['ascending']

            if primary_col not in sorted_df.columns:
                return [1] * len(sorted_df)

            values = sorted_df[primary_col]
            ranks = []
            current_rank = 1
            i = 0

            while i < len(values):
                current_val = values.iloc[i]
                tie_count = 0
                j = i
                while j < len(values):
                    if pd.isna(values.iloc[j]) and pd.isna(current_val):
                        j += 1
                        tie_count += 1
                    elif not pd.isna(values.iloc[j]) and not pd.isna(current_val):
                        if values.iloc[j] == current_val:
                            j += 1
                            tie_count += 1
                        else:
                            break
                    else:
                        break

                for _ in range(tie_count):
                    ranks.append(current_rank)

                current_rank += 1
                i = j

            return ranks

        return [None] * len(partition_df)

    def _compute_aggregate_window_function(
        self,
        df: pd.DataFrame,
        partition_cols: List[str],
        order_by_cols: List[Dict],
        func_name: str,
        args: List[str],
        frame_type: Optional[str],
        frame_start: Optional[str],
        frame_between_end: Optional[str],
        base_df: pd.DataFrame
    ) -> List[Any]:
        """Compute aggregate window function values."""

        # Determine the value column
        if args and args[0] != '*':
            value_col = args[0] if args[0] in df.columns else base_df.columns[0] if not base_df.empty and args[0] in base_df.columns else args[0]
        else:
            # No specific column, use first numeric column or return None
            value_col = None

        # Get numeric values
        if value_col and value_col in df.columns:
            values = pd.to_numeric(df[value_col], errors='coerce')
        else:
            values = pd.Series([None] * len(df))

        # For window functions without ORDER BY and without frame clause (default: UNBOUNDED PRECEDING)
        if not order_by_cols:
            # Simple aggregation over partition (entire window)
            if not partition_cols:
                # Single window for entire DataFrame
                result = self._apply_aggregation(func_name, values, None, None)
                return [result] * len(df)
            else:
                # Multiple partitions
                results = []
                for part_val, part_df in df.groupby(partition_cols, dropna=False, sort=False):
                    part_values = pd.to_numeric(part_df.iloc[:, 0] if value_col is None else part_df[value_col], errors='coerce')
                    agg_val = self._apply_aggregation(func_name, part_values, None, None)
                    results.extend([agg_val] * len(part_df))
                return results

        # With ORDER BY - need frame-based calculation
        if not partition_cols:
            # Single partition
            return self._compute_frame_aggregation(
                df, None, values, func_name, frame_type, frame_start, frame_between_end, order_by_cols
            )
        else:
            # Multiple partitions - compute separately for each
            all_results = []
            for part_val, part_df in df.groupby(partition_cols, dropna=False, sort=False):
                part_values = pd.to_numeric(part_df.iloc[:, 0] if value_col is None else part_df[value_col], errors='coerce')
                part_results = self._compute_frame_aggregation(
                    part_df, part_val, part_values, func_name, frame_type, frame_start, frame_between_end, order_by_cols
                )
                all_results.extend(part_results)
            return all_results

    def _compute_frame_aggregation(
        self,
        df: pd.DataFrame,
        partition_val: Any,
        values: pd.Series,
        func_name: str,
        frame_type: Optional[str],
        frame_start: Optional[str],
        frame_between_end: Optional[str],
        order_by_cols: List[Dict]
    ) -> List[Any]:
        """Compute aggregate over a framing window."""

        if df.empty:
            return []

        # Sort DataFrame by order_by columns for frame computation
        sort_cols = [ob['column'] for ob in order_by_cols]
        ascending_list = [ob['ascending'] for ob in order_by_cols]

        # Only include columns that exist
        existing_sort_cols = [c for c in sort_cols if c in df.columns]

        if existing_sort_cols:
            sorted_indices = df.sort_values(by=existing_sort_cols, ascending=ascending_list, na_position='last').index
        else:
            sorted_indices = df.index.tolist()

        # Create a mapping from original index to position in sorted order
        sorted_positions = {}
        for pos, idx in enumerate(sorted_indices):
            sorted_positions[idx] = pos

        # Determine frame boundaries for each row
        n = len(df)

        # Default frame: RANGE UNBOUNDED PRECEDING (current row)
        if frame_type is None and frame_start is None and frame_between_end is None:
            # Default is RANGE UNBOUNDED PRECEDING which equals current row when no ORDER BY ties
            # Actually for aggregate functions, default is UNBOUNDED PRECEDING
            frame_type = 'RANGE'
            frame_start = 'UNBOUNDED_PRECEDING'
            frame_between_end = None  # Implicitly current row

        results = []

        for idx in df.index:
            current_pos = sorted_positions.get(idx, 0)

            # Determine frame bounds
            if frame_type == 'ROWS':
                # Row-based framing
                if frame_start == 'UNBOUNDED_PRECEDING':
                    start_pos = 0
                elif frame_start == 'CURRENT_ROW':
                    start_pos = current_pos
                else:
                    start_pos = 0  # Default

                if frame_between_end:
                    if frame_between_end == 'CURRENT_ROW':
                        end_pos = current_pos
                    elif frame_between_end == 'UNBOUNDED_FOLLOWING':
                        end_pos = n - 1
                    else:
                        end_pos = current_pos
                else:
                    # No BETWEEN means current row only
                    end_pos = current_pos
            else:
                # RANGE framing - includes all rows with same ORDER BY values
                # For simplicity, treat as ROWS framing when no value-based framing is needed
                # (i.e., all values are distinct)
                if frame_start == 'UNBOUNDED_PRECEDING':
                    start_pos = 0
                elif frame_start == 'CURRENT_ROW':
                    start_pos = current_pos
                else:
                    start_pos = 0

                if frame_between_end:
                    if frame_between_end == 'CURRENT_ROW':
                        end_pos = current_pos
                    elif frame_between_end == 'UNBOUNDED_FOLLOWING':
                        end_pos = n - 1
                    else:
                        end_pos = current_pos
                else:
                    end_pos = current_pos

            # Ensure bounds are valid
            start_pos = max(0, start_pos)
            end_pos = min(n - 1, end_pos)

            # Get values in frame
            if start_pos <= end_pos:
                frame_indices = [sorted_positions[sorted_indices[i]] for i in range(start_pos, end_pos + 1)]
                frame_values = values.iloc[list(frame_indices)]
            else:
                frame_values = pd.Series([None])

            # Apply aggregation
            result = self._apply_aggregation(func_name, frame_values, start_pos, end_pos)
            results.append(result)

        return results

    def _apply_aggregation(self, func_name: str, values: pd.Series, start_pos: Optional[int], end_pos: Optional[int]) -> Any:
        """Apply aggregation function to values."""
        # Filter out NaN for calculations
        non_null_values = values.dropna()

        if len(non_null_values) == 0:
            return None

        if func_name == 'SUM':
            return non_null_values.sum()
        elif func_name == 'AVG':
            return non_null_values.mean()
        elif func_name == 'MIN':
            return non_null_values.min()
        elif func_name == 'MAX':
            return non_null_values.max()
        elif func_name == 'COUNT':
            # COUNT counts non-NULL values
            return len(non_null_values)
        else:
            return None


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
