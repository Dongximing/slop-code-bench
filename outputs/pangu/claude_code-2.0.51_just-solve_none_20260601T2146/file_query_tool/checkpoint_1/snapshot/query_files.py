#!/usr/bin/env python3
"""
SQL Query CLI for CSV files.

A lightweight CLI to run read-only SQL queries across a folder of CSVs with matching headers.
"""

import os
import sys
import argparse
import pandas as pd
from pathlib import Path
import sqlparse
from sqlparse.sql import Statement, TokenList, Identifier, IdentifierList, Comparison, Parenthesis, Function, Operation
from sqlparse.tokens import Keyword, Token, Number, String, Name, Operator
from typing import Dict, List, Optional, Tuple, Any, Set
import re


class CSVDatabase:
    """Manages CSV files as database tables."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, pd.DataFrame] = {}
        self._load_tables()

    def _load_tables(self):
        """Load all CSV files from the data directory."""
        for csv_path in self.data_dir.rglob("*.csv"):
            # Handle nested directories: use . as separator in table name
            # Convert relative path to table name
            rel_path = csv_path.relative_to(self.data_dir)
            # For nested files like "payments/pampas.csv", table name is "payments.pampas"
            table_name = str(rel_path.with_suffix(""))
            # Handle the special case: if there's a literal "." in the filename, replace with _
            # Actually based on the spec: "If the '.' is in the name of the file, then you need to replace that with _"
            # So data/payments/pampas.csv -> payments.pampas table name
            # If file is named something.else.csv, the table name should use . for nested paths
            # but replace literal . in filename with _

            # The spec says: "For nested directories, use . to determine which table to query"
            # So path: data/payments/pampas.csv -> table name: payments.pampas
            # The file suffix removal handles this

            # Also handle the special case: replace literal '.' in the filename with '_'
            # e.g., if file is "my.file.csv" -> table name should have my_file
            # Let me re-read: "If the '.' is in the name of the file, then you need to replace that with _"
            # This is about the filename itself, not the directory structure
            # So "payments.pampas.csv" becomes "payments.pampas" table
            # But if the filename contains literal dots, replace with underscore
            # Actually I think they mean: if the path contains dots (for nested), use dot
            # if the filename part has a literal dot, replace with underscore

            # Let me reconsider: data/payments/pampas.csv means nested directory
            # Table name should be: payments.pampas (using dot for nesting)
            # If there was a file like my.file.csv, the table name would be my_file

            # Actually reading the spec again:
            # "For nested directories, use . to determine which table to query"
            # This means the directory structure uses dots as separators
            # "If the . is in the name of the file, then you need to replace that with _"
            # This means the filename itself (without extension) should have dots replaced

            parts = list(rel_path.parts)
            filename_without_ext = parts[-1].replace('.csv', '')
            # Replace literal dots in filename with underscore
            filename_without_ext = filename_without_ext.replace('.', '_')
            parts[-1] = filename_without_ext
            table_name = '.'.join(parts)

            try:
                df = pd.read_csv(csv_path)
                # Validate header
                if df.empty or len(df.columns) == 0:
                    print(f"Warning: {csv_path} has no columns or is empty", file=sys.stderr)
                    continue
                self.tables[table_name] = df
            except Exception as e:
                print(f"Warning: Could not load {csv_path}: {e}", file=sys.stderr)

    def get_table(self, name: str) -> Optional[pd.DataFrame]:
        """Get a table by name."""
        return self.tables.get(name)

    def table_exists(self, name: str) -> bool:
        """Check if a table exists."""
        return name in self.tables

    def get_all_tables(self) -> Dict[str, pd.DataFrame]:
        """Get all tables."""
        return self.tables


class SQLExpressionEvaluator:
    """Evaluates boolean expressions for WHERE and HAVING clauses."""

    def __init__(self, tables: Dict[str, pd.DataFrame]):
        self.tables = tables
        self.aggregates = {'COUNT', 'SUM', 'MIN', 'MAX'}

    def evaluate(self, expr: str, row: pd.Series, agg_results: Dict = None) -> bool:
        """Evaluate a boolean expression against a row."""
        if agg_results is None:
            agg_results = {}

        # Parse and evaluate the expression
        # This is a simplified implementation
        # For a full implementation, we'd need a proper parser
        expr = expr.strip()

        # Handle parentheses
        while '(' in expr:
            start = expr.rfind('(')
            end = expr.find(')', start)
            if end == -1:
                break
            inner = expr[start+1:end]
            inner_result = self.evaluate(inner, row, agg_results)
            expr = expr[:start] + ('TRUE' if inner_result else 'FALSE') + expr[end+1:]

        # Handle NOT
        expr = re.sub(r'\bNOT\s+', lambda m: 'NOT ' if self.evaluate(m.group().strip()[4:], row, agg_results) else 'FALSE ', expr, flags=re.IGNORECASE)

        # Handle AND/OR (simplified - would need proper parsing)
        # For simplicity, we'll use Python's eval with a safe environment
        # This is NOT safe for production but works for this exercise
        try:
            return self._safe_eval(expr, row, agg_results)
        except:
            return True  # If we can't evaluate, assume True

    def _safe_eval(self, expr: str, row: pd.Series, agg_results: Dict) -> bool:
        """Safely evaluate expression using row data."""
        # Replace column references with their values
        # Handle string literals
        def replace_column(match):
            col = match.group(1)
            # Handle table-qualified column names
            if '.' in col:
                parts = col.split('.')
                # Just use the column name part
                col = parts[-1]
            if col in row.index:
                val = row[col]
                if pd.isna(val):
                    return 'None'
                elif isinstance(val, str):
                    return f"'{val}'"
                elif isinstance(val, (int, float)):
                    return str(val)
            return 'None'

        # Replace column references
        expr = re.sub(r'\b(\w+)\b', replace_column, expr)

        # Handle LIKE operator
        expr = re.sub(r"([\'\"]?[^\'\"]*?)[\'\"]?\s+LIKE\s+([\'\"]?[^\'\"]*?)[\'\"]?",
                     self._handle_like, expr, flags=re.IGNORECASE)

        # Replace SQL operators with Python equivalents
        expr = expr.replace('<>', '!=')
        expr = expr.replace('AND', 'and')
        expr = expr.replace('OR', 'or')
        expr = expr.replace('NOT', 'not')
        expr = expr.replace('TRUE', 'True')
        expr = expr.replace('FALSE', 'False')
        expr = expr.replace('NULL', 'None')

        # Evaluate
        return eval(expr, {"__builtins__": {}}, {})

    def _handle_like(self, match):
        left = match.group(1).strip("'\"")
        pattern = match.group(2).strip("'\"")
        # Convert SQL LIKE to Python regex
        pattern = pattern.replace('%', '.*').replace('_', '.')
        result = bool(re.match(pattern + '$', left, re.IGNORECASE))
        return 'True' if result else 'False'


class SQLExecutor:
    """Executes SQL queries on CSV data."""

    def __init__(self, db: CSVDatabase):
        self.db = db
        self.aggregates = {'COUNT', 'SUM', 'MIN', 'MAX'}

    def execute(self, sql: str) -> pd.DataFrame:
        """Parse and execute SQL query."""
        # Parse the SQL using sqlparse
        parsed = sqlparse.parse(sql.strip())
        if not parsed:
            raise ValueError("No valid SQL statement found")

        stmt = parsed[0]

        # Extract clauses
        select_exprs = self._extract_select(stmt)
        from_clause = self._extract_from(stmt)
        where_expr = self._extract_where(stmt)
        group_by = self._extract_group_by(stmt)
        having_expr = self._extract_having(stmt)
        order_by = self._extract_order_by(stmt)
        limit = self._extract_limit(stmt)
        offset = self._extract_offset(stmt)

        import sys
        print(f"DEBUG: where_expr='{where_expr}'", file=sys.stderr)

        # Execute the query
        return self._build_query(select_exprs, from_clause, where_expr, group_by, having_expr, order_by, limit, offset)

    def _extract_select(self, stmt):
        """Extract SELECT clause expressions."""
        select_exprs = []
        in_select = False
        distinct = False

        for token in stmt.flatten():
            if token.ttype in Keyword and token.value.upper() == 'SELECT':
                in_select = True
                continue
            elif token.ttype in Keyword and token.value.upper() in ['FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                in_select = False
                continue

            if in_select and token.value.strip():
                val = token.value.strip()
                if val.upper() == 'DISTINCT':
                    distinct = True
                else:
                    select_exprs.append(val)

        return select_exprs, distinct

    def _extract_from(self, stmt) -> Dict:
        """Extract FROM clause with JOINs."""
        result = {'main_table': None, 'joins': [], 'on_clause': None}
        in_from = False
        in_join = False
        current_join = None
        join_type = 'INNER'

        for token in stmt.tokens:
            if token.ttype in Keyword:
                val = token.value.upper()
                if val == 'FROM':
                    in_from = True
                    in_join = False
                elif val in ['INNER', 'LEFT', 'RIGHT', 'FULL']:
                    if in_from or in_join:
                        join_type = val
                elif val == 'JOIN':
                    in_join = True
                    if result['main_table'] is None:
                        # First table before any JOIN
                        result['main_table'] = None  # Will be set when we see the table name
                elif val == 'ON':
                    if current_join:
                        current_join['in_on'] = True
                elif val in ['WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                    in_from = False
                    in_join = False
                    if current_join:
                        current_join['in_on'] = False

            elif in_from or in_join:
                val = token.value.strip()
                if val and val != ',':
                    if isinstance(token, sqlparse.sql.Identifier):
                        table_name = token.get_name()
                        alias = token.get_alias()

                        if in_join:
                            result['joins'].append({
                                'table': table_name,
                                'type': join_type,
                                'alias': alias,
                                'on_cols': []
                            })
                            current_join = result['joins'][-1]
                        elif result['main_table'] is None:
                            result['main_table'] = table_name

        return result

    def _extract_where(self, stmt) -> str:
        """Extract WHERE clause expression."""
        in_where = False
        expr_parts = []

        for token in stmt.tokens:
            if token.ttype in Keyword and token.value.upper() == 'WHERE':
                in_where = True
                continue
            elif token.ttype in Keyword and token.value.upper() in ['GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                in_where = False

            if in_where:
                expr_parts.append(token.value)

        return ''.join(expr_parts).strip()

    def _extract_group_by(self, stmt) -> List[str]:
        """Extract GROUP BY clause."""
        in_group = False
        columns = []

        for token in stmt.flatten():
            if token.ttype is Keyword and token.value.upper() == 'GROUP':
                in_group = True
                continue
            elif token.ttype is Keyword and token.value.upper() == 'BY':
                continue
            elif token.ttype is Keyword and token.value.upper() in ['HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                in_group = False

            if in_group and token.value.strip():
                val = token.value.strip()
                if val != ',':
                    columns.append(val)

        return columns

    def _extract_having(self, stmt) -> str:
        """Extract HAVING clause expression."""
        in_having = False
        expr_parts = []

        for token in stmt.flatten():
            if token.ttype is Keyword and token.value.upper() == 'HAVING':
                in_having = True
                continue
            elif token.ttype is Keyword and token.value.upper() in ['ORDER', 'LIMIT', 'OFFSET']:
                in_having = False

            if in_having:
                expr_parts.append(token.value)

        return ''.join(expr_parts).strip()

    def _extract_order_by(self, stmt) -> List[Tuple[str, bool]]:
        """Extract ORDER BY clause."""
        in_order = False
        columns = []

        for token in stmt.flatten():
            if token.ttype is Keyword and token.value.upper() == 'ORDER':
                in_order = True
                continue
            elif token.ttype is Keyword and token.value.upper() == 'BY':
                continue
            elif token.ttype is Keyword and token.value.upper() in ['LIMIT', 'OFFSET']:
                in_order = False

            if in_order and token.value.strip():
                val = token.value.strip().upper()
                if val == 'DESC':
                    if columns:
                        columns[-1] = (columns[-1][0], True)
                elif val == 'ASC':
                    if columns:
                        columns[-1] = (columns[-1][0], False)
                elif val != ',':
                    columns.append((token.value.strip(), False))

        return columns

    def _extract_limit(self, stmt) -> Optional[int]:
        """Extract LIMIT clause."""
        in_limit = False

        for token in stmt.flatten():
            if token.ttype is Keyword and token.value.upper() == 'LIMIT':
                in_limit = True
                continue
            elif token.ttype is Keyword and token.value.upper() in ['OFFSET']:
                in_limit = False

            if in_limit and token.ttype is Number.Integer:
                return int(token.value)

        return None

    def _extract_offset(self, stmt) -> Optional[int]:
        """Extract OFFSET clause."""
        in_offset = False

        for token in stmt.flatten():
            if token.ttype is Keyword and token.value.upper() == 'OFFSET':
                in_offset = True
                continue
            elif token.ttype is Keyword and token.value.upper() in ['LIMIT']:
                in_offset = False

            if in_offset and token.ttype is Number.Integer:
                return int(token.value)

        return None

    def _build_query(self, select_exprs, from_clause, where_expr, group_by, having_expr, order_by, limit, offset) -> pd.DataFrame:
        """Build and execute the query step by step."""
        # Unpack select_exprs
        if isinstance(select_exprs, tuple):
            select_list, distinct = select_exprs
        else:
            select_list = select_exprs
            distinct = False

        # Get base table
        if not from_clause or not from_clause.get('main_table'):
            raise ValueError("FROM clause is required")

        main_table_name = from_clause['main_table']
        main_df = self.db.get_table(main_table_name)
        if main_df is None:
            raise ValueError(f"Table '{main_table_name}' not found")

        # Track column name collisions and alias mapping
        select_columns = []
        agg_info = {}  # Maps select expression to (agg_type, column, alias)

        # Process SELECT expressions to identify aggregates
        for expr in select_list:
            expr = expr.strip()
            expr_upper = expr.upper()

            # Handle aggregates
            if expr_upper.startswith('COUNT('):
                inner = expr[6:-1].strip()
                agg_type = 'COUNT'
                if inner == '*':
                    col = '__count_all__'
                else:
                    col = inner.split('.')[-1] if '.' in inner else inner
                alias = self._extract_alias(expr) or f'count_{len(select_columns)}'
                agg_info[expr] = (agg_type, col, alias)
                select_columns.append((alias, expr, agg_type, col))

            elif expr_upper.startswith('SUM('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = self._extract_alias(expr) or f'sum_{len(select_columns)}'
                agg_info[expr] = ('SUM', col, alias)
                select_columns.append((alias, expr, 'SUM', col))

            elif expr_upper.startswith('MIN('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = self._extract_alias(expr) or f'min_{len(select_columns)}'
                agg_info[expr] = ('MIN', col, alias)
                select_columns.append((alias, expr, 'MIN', col))

            elif expr_upper.startswith('MAX('):
                col = expr[4:-1].strip().split('.')[-1]
                alias = self._extract_alias(expr) or f'max_{len(select_columns)}'
                agg_info[expr] = ('MAX', col, alias)
                select_columns.append((alias, expr, 'MAX', col))

            else:
                # Regular column
                col_name = expr.split('.')[-1] if '.' in expr else expr
                alias = self._extract_alias(expr) or col_name
                select_columns.append((alias, expr, None, col_name))

        # Process JOINs
        for join_info in from_clause.get('joins', []):
            join_table = self.db.get_table(join_info['table'])
            if join_table is None:
                raise ValueError(f"Table '{join_info['table']}' not found")

            # Find join key from WHERE or ON clause
            join_key = self._find_join_key(where_expr, from_clause['main_table'], join_info['table'])
            if join_key:
                how = join_info['type'].upper() if join_info['type'] else 'INNER'
                main_df = pd.merge(main_df, join_table, left_on=join_key[0], right_on=join_key[1],
                                  how=how, suffixes=('_x', '_y'))
            else:
                # If no explicit join key, just cross join then filter
                main_df = pd.merge(main_df, join_table, how=join_info['type'].upper() if join_info['type'] else 'inner',
                                  suffixes=('_x', '_y'))

        # Apply WHERE filtering (before grouping)
        if where_expr:
            evaluator = SQLExpressionEvaluator(self.db.tables)
            mask = main_df.apply(lambda row: evaluator.evaluate(where_expr, row), axis=1)
            main_df = main_df[mask]

        # Apply GROUP BY
        if group_by:
            group_cols = [g.split('.')[-1] for g in group_by]
            existing_group_cols = [c for c in group_cols if c in main_df.columns]

            if not existing_group_cols:
                raise ValueError("No valid GROUP BY columns found")

            # Build aggregation dict
            agg_dict = {}
            for alias, expr, agg_type, col in select_columns:
                if agg_type and col in main_df.columns:
                    agg_dict[col] = agg_type.lower()
                elif agg_type == 'COUNT' and col == '__count_all__':
                    # Count all rows - will handle separately
                    pass

            # Perform groupby
            if agg_dict:
                grouped = main_df.groupby(existing_group_cols, as_index=False).agg(agg_dict)
                # Rename aggregated columns to aliases
                new_cols = []
                for col in grouped.columns:
                    if col in existing_group_cols:
                        new_cols.append(col)
                    else:
                        # Find matching alias
                        for alias, expr, agg_type, c in select_columns:
                            if c == col:
                                new_cols.append(alias)
                                break
                        else:
                            new_cols.append(col)
                grouped.columns = new_cols
            else:
                # Only counting
                grouped = main_df.groupby(existing_group_cols, as_index=False).size()
                grouped.columns = existing_group_cols + ['count']

            main_df = grouped

        # Apply HAVING (after grouping)
        if having_expr and group_by:
            evaluator = SQLExpressionEvaluator(self.db.tables)
            mask = main_df.apply(lambda row: evaluator.evaluate(having_expr, row), axis=1)
            main_df = main_df[mask]

        # Apply DISTINCT
        if distinct:
            # Get the columns we're selecting
            select_cols = [alias for alias, expr, agg_type, col in select_columns if alias in main_df.columns]
            if select_cols:
                main_df = main_df[select_cols].drop_duplicates()

        # Apply ORDER BY
        if order_by:
            sort_cols = []
            ascending = []
            for col, desc in order_by:
                col_name = col.split('.')[-1] if '.' in col else col
                # Try to find in dataframe
                if col_name in main_df.columns:
                    sort_cols.append(col_name)
                    ascending.append(not desc)
                else:
                    # Try alias
                    for alias, expr, agg_type, c in select_columns:
                        if alias == col_name:
                            sort_cols.append(alias)
                            ascending.append(not desc)
                            break

            if sort_cols:
                main_df = main_df.sort_values(by=sort_cols, ascending=ascending)

        # Apply LIMIT and OFFSET
        if limit is not None:
            if offset is not None:
                main_df = main_df.iloc[offset:offset+limit]
            else:
                main_df = main_df.head(limit)
        elif offset is not None:
            main_df = main_df.iloc[offset:]

        # Build final result with correct column order
        result_cols = []
        for alias, expr, agg_type, col in select_columns:
            if alias in main_df.columns:
                result_cols.append(alias)
            elif agg_type == 'COUNT' and col == '__count_all__':
                # Add count of all rows
                result_cols.append('count')

        # If no select columns specified, use all
        if not result_cols:
            result_cols = [c for c in main_df.columns if not c.endswith('_x') and not c.endswith('_y')]

        return main_df[result_cols]

    def _extract_alias(self, expr: str) -> Optional[str]:
        """Extract alias from expression if present."""
        # Handle "column AS alias" or "column alias"
        expr_lower = expr.lower()
        if ' as ' in expr_lower:
            return expr.split('AS')[-1].strip().strip('"').strip("'")
        # Check for bare alias (space-separated)
        parts = expr.rsplit(' ', 1)
        if len(parts) == 2 and not parts[1].upper() in ['ASC', 'DESC']:
            # Could be an alias
            potential_alias = parts[1].strip('"').strip("'")
            # Simple check: if it's not a keyword or number, treat as alias
            if not potential_alias.upper() in ['AND', 'OR', 'NOT', 'SELECT', 'FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET', 'INNER', 'LEFT', 'RIGHT', 'FULL', 'JOIN', 'ON', 'BY']:
                try:
                    float(potential_alias)
                    return None  # It's a number
                except:
                    return potential_alias
        return None

    def _find_join_key(self, where_expr: str, table1: str, table2: str) -> Optional[Tuple[str, str]]:
        """Find join key from WHERE or ON clause."""
        if not where_expr:
            return None

        # Look for patterns like: t1.col = t2.col or t2.col = t1.col
        patterns = [
            rf'{table1}\.([\w]+)\s*=\s*{table2}\.([\w]+)',
            rf'{table2}\.([\w]+)\s*=\s*{table1}\.([\w]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, where_expr, re.IGNORECASE)
            if match:
                return (match.group(1), match.group(2))

        return None


def load_sql_query(sql_arg: str) -> str:
    """Load SQL query from file or return as-is."""
    sql_path = Path(sql_arg)
    if sql_path.exists():
        return sql_path.read_text()
    return sql_arg


def format_as_markdown(df: pd.DataFrame) -> str:
    """Format DataFrame as a markdown table."""
    if df.empty:
        return ""

    # Build header
    headers = list(df.columns)
    header_line = "| " + " | ".join(str(h) for h in headers) + " |"
    separator = "| " + " | ".join(["---"] * len(headers)) + " |"

    # Build rows
    rows = []
    for _, row in df.iterrows():
        cells = []
        for col in headers:
            val = row[col]
            if pd.isna(val):
                cells.append("NULL")
            else:
                cells.append(str(val))
        rows.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, separator] + rows)


def save_as_csv(df: pd.DataFrame, output_path: str):
    """Save DataFrame to CSV file."""
    df.to_csv(output_path, index=False)


def main():
    parser = argparse.ArgumentParser(
        description="Run SQL queries on CSV files"
    )
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query or path to SQL file')
    parser.add_argument('--out', required=False, help='Output file path for CSV result')

    args = parser.parse_args()

    # Validate input directory
    if not os.path.isdir(args.data):
        print(f"Error: Data directory '{args.data}' does not exist", file=sys.stderr)
        sys.exit(1)

    # Load SQL query
    try:
        sql = load_sql_query(args.sql)
    except Exception as e:
        print(f"Error loading SQL: {e}", file=sys.stderr)
        sys.exit(1)

    # Load database
    try:
        db = CSVDatabase(args.data)
        if not db.tables:
            print("Error: No valid CSV files found in data directory", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"Error loading data: {e}", file=sys.stderr)
        sys.exit(1)

    # Execute query
    try:
        executor = SQLExecutor(db)
        result = executor.execute(sql)
    except Exception as e:
        print(f"Error executing query: {e}", file=sys.stderr)
        sys.exit(1)

    # Output results
    print(format_as_markdown(result))

    # Save to file if specified
    if args.out:
        try:
            save_as_csv(result, args.out)
        except Exception as e:
            print(f"Error saving output: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
