#!/usr/bin/env python3
"""
Lightweight CLI to run read-only SQL queries across a folder of CSV files.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Optional

import pandas as pd


class SQLParseError(Exception):
    """Raised when SQL parsing fails."""
    pass


class SQLParser:
    """Parser for the supported SQL subset."""

    def __init__(self, sql_text: str):
        self.sql = sql_text.strip()
        self.pos = 0
        self.length = len(self.sql)
        # Pre-compile token patterns for performance
        self._token_patterns = [
            (re.compile(r'^\s+', re.IGNORECASE), None),
            (re.compile(r'^SELECT', re.IGNORECASE), 'SELECT'),
            (re.compile(r'^FROM', re.IGNORECASE), 'FROM'),
            (re.compile(r'^WHERE', re.IGNORECASE), 'WHERE'),
            (re.compile(r'^GROUP\s+BY', re.IGNORECASE), 'GROUP_BY'),
            (re.compile(r'^HAVING', re.IGNORECASE), 'HAVING'),
            (re.compile(r'^ORDER\s+BY', re.IGNORECASE), 'ORDER_BY'),
            (re.compile(r'^LIMIT', re.IGNORECASE), 'LIMIT'),
            (re.compile(r'^OFFSET', re.IGNORECASE), 'OFFSET'),
            (re.compile(r'^INNER', re.IGNORECASE), 'INNER'),
            (re.compile(r'^LEFT', re.IGNORECASE), 'LEFT'),
            (re.compile(r'^RIGHT', re.IGNORECASE), 'RIGHT'),
            (re.compile(r'^FULL', re.IGNORECASE), 'FULL'),
            (re.compile(r'^JOIN', re.IGNORECASE), 'JOIN'),
            (re.compile(r'^ON', re.IGNORECASE), 'ON'),
            (re.compile(r'^AND', re.IGNORECASE), 'AND'),
            (re.compile(r'^OR', re.IGNORECASE), 'OR'),
            (re.compile(r'^NOT', re.IGNORECASE), 'NOT'),
            (re.compile(r'^DISTINCT', re.IGNORECASE), 'DISTINCT'),
            (re.compile(r'^AS', re.IGNORECASE), 'AS'),
            (re.compile(r'^\*', re.IGNORECASE), 'MULTIPLY'),
            (re.compile(r'^LIKE', re.IGNORECASE), 'LIKE'),
            (re.compile(r'^!=', re.IGNORECASE), 'NE'),
            (re.compile(r'^<=', re.IGNORECASE), 'LE'),
            (re.compile(r'^>=', re.IGNORECASE), 'GE'),
            (re.compile(r'<>', re.IGNORECASE), 'NE'),  # also !=
            (re.compile(r'^<', re.IGNORECASE), 'LT'),
            (re.compile(r'^>', re.IGNORECASE), 'GT'),
            (re.compile(r'^=', re.IGNORECASE), 'EQ'),
            (re.compile(r'^\(', re.IGNORECASE), 'LPAREN'),
            (re.compile(r'^\)', re.IGNORECASE), 'RPAREN'),
            (re.compile(r'^,', re.IGNORECASE), 'COMMA'),
            (re.compile(r'^\.', re.IGNORECASE), 'DOT'),
            (re.compile(r'"[^"]*"', re.IGNORECASE), 'STRING_LITERAL'),  # double-quoted string
            (re.compile(r"'[^']*'", re.IGNORECASE), 'STRING_LITERAL'),  # single-quoted string
            (re.compile(r'^-?\d+', re.IGNORECASE), 'INTEGER_LITERAL'),
            (re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*', re.IGNORECASE), 'IDENT'),  # identifier
        ]
        self.tokens = self._tokenize()
        self.token_pos = 0

    def _tokenize(self) -> list:
        """Tokenize SQL into keywords, identifiers, operators, literals."""
        tokens = []
        i = 0
        while i < self.length:
            matched = False
            for regex, token_type in self._token_patterns:
                match = regex.match(self.sql[i:])
                if match:
                    matched = True
                    value = match.group(0)
                    if token_type is None:
                        # whitespace, skip
                        pass
                    else:
                        tokens.append((token_type, value.upper() if token_type in ['SELECT', 'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'DISTINCT', 'LIKE'] else value))
                    i += len(value)
                    break
            if not matched:
                # Skip invalid characters
                i += 1

        return tokens
    def _peek(self, offset=0) -> Optional[tuple]:
        """Peek at the next token without consuming."""
        idx = self.token_pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return None

    def _consume(self, expected_type: Optional[str] = None, expected_value: Optional[str] = None) -> Optional[tuple]:
        """Consume and return the next token."""
        if self.token_pos >= len(self.tokens):
            return None
        token = self.tokens[self.token_pos]
        self.token_pos += 1
        if expected_type and token[0] != expected_type:
            raise SQLParseError(f"Expected {expected_type}, got {token[0]}")
        if expected_value and token[1].upper() != expected_value.upper():
            raise SQLParseError(f"Expected '{expected_value}', got '{token[1]}'")
        return token

    def _match(self, token_type: str, value: Optional[str] = None) -> bool:
        """Check if the next token matches."""
        token = self._peek()
        if token is None:
            return False
        if token[0] == token_type:
            if value is None or token[1].upper() == value.upper():
                return True
        return False

    def parse(self) -> dict:
        """Parse the full SQL statement."""
        result = {
            'distinct': False,
            'select_list': [],
            'from_clause': [],
            'where_expr': None,
            'group_by': [],
            'having_expr': None,
            'order_by': [],
            'limit': None,
            'offset': None
        }

        # Expect SELECT
        if not self._match('SELECT'):
            raise SQLParseError("Expected SELECT")
        self._consume('SELECT')

        # Check DISTINCT
        if self._match('DISTINCT'):
            result['distinct'] = True
            self._consume('DISTINCT')

        # Parse select list
        result['select_list'] = self._parse_select_list()

        # Expect FROM
        if not self._match('FROM'):
            raise SQLParseError("Expected FROM")
        self._consume('FROM')

        # Parse from clause (tables with optional joins)
        result['from_clause'] = self._parse_from_clause()

        # Parse WHERE (optional)
        if self._match('WHERE'):
            self._consume('WHERE')
            result['where_expr'] = self._parse_boolean_expr()

        # Parse GROUP BY (optional)
        if self._match('GROUP_BY'):
            self._consume('GROUP_BY')
            self._consume(None, 'BY')  # "BY" keyword
            result['group_by'] = self._parse_column_refs()

        # Parse HAVING (optional)
        if self._match('HAVING'):
            self._consume('HAVING')
            result['having_expr'] = self._parse_boolean_expr()

        # Parse ORDER BY (optional)
        if self._match('ORDER_BY'):
            self._consume('ORDER_BY')
            self._consume(None, 'BY')
            result['order_by'] = self._parse_order_items()

        # Parse LIMIT (optional)
        if self._match('LIMIT'):
            self._consume('LIMIT')
            limit_token = self._consume('INTEGER_LITERAL')
            result['limit'] = int(limit_token[1])

        # Parse OFFSET (optional)
        if self._match('OFFSET'):
            self._consume('OFFSET')
            offset_token = self._consume('INTEGER_LITERAL')
            result['offset'] = int(offset_token[1])

        return result

    def _parse_select_list(self) -> list:
        """Parse SELECT list items."""
        items = []
        while True:
            item = self._parse_select_item()
            items.append(item)
            if not self._match('COMMA'):
                break
            self._consume('COMMA')
        return items

    def _parse_select_item(self) -> dict:
        """Parse a single SELECT item."""
        # Check for aggregate functions
        token = self._peek()
        if token and token[0] in ['COUNT', 'SUM', 'MIN', 'MAX']:
            func = self._consume()[0]
            self._consume('LPAREN')

            if self._match('MULTIPLY'):
                arg = '*'
                self._consume('MULTIPLY')
            else:
                arg = self._parse_column_ref()

            self._consume('RPAREN')

            result = {
                'type': 'aggregate',
                'function': func.lower(),
                'arg': arg
            }
        else:
            # Regular column or expression
            expr = self._parse_column_ref()

            # Check for alias
            alias = None
            if self._match('AS'):
                self._consume('AS')
            if self._peek() and self._peek()[0] == 'IDENT':
                alias = self._consume('IDENT')[1]

            result = {
                'type': 'column',
                'expr': expr,
                'alias': alias
            }

        return result

    def _parse_from_clause(self) -> list:
        """Parse FROM clause with optional joins."""
        tables = []

        # First table in FROM
        first_table = self._parse_table_ref()
        tables.append(first_table)

        # Parse joins
        while self._match('JOIN'):
            join_type = 'INNER'  # default
            if self._peek(offset=1) and self._peek(offset=1)[0] in ['INNER', 'LEFT', 'RIGHT', 'FULL']:
                join_type = self._consume()[1].upper()

            self._consume('JOIN')
            join_table = self._parse_table_ref()

            # Parse ON clause
            self._consume('ON')
            on_predicate = self._parse_equality_predicate()

            tables.append({
                'type': 'join',
                'join_type': join_type,
                'table': join_table,
                'on': on_predicate
            })

        return tables

    def _parse_table_ref(self) -> dict:
        """Parse a table reference (table name with optional alias)."""
        # Table name can include dots for nested directories (e.g., payments.pampas)
        parts = []
        while self._peek() and self._peek()[0] == 'IDENT':
            parts.append(self._consume('IDENT')[1])
            if self._match('DOT'):
                self._consume('DOT')

        if not parts:
            raise SQLParseError("Expected table name")

        table_name = '.'.join(parts)

        # Optional alias
        alias = None
        if self._match('AS'):
            self._consume('AS')
        if self._peek() and self._peek()[0] == 'IDENT':
            alias = self._consume('IDENT')[1]

        return {
            'type': 'table',
            'name': table_name,
            'alias': alias
        }

    def _parse_equality_predicate(self) -> dict:
        """Parse ON equality predicate (simple column = column)."""
        left = self._parse_column_ref()
        self._consume('EQ')
        right = self._parse_column_ref()
        return {
            'type': 'equality',
            'left': left,
            'right': right
        }

    def _parse_column_ref(self) -> dict:
        """Parse a column reference (e.g., table.column, column)."""
        parts = []

        # Check for aggregate function first (in case it's nested)
        if self._peek() and self._peek()[0] in ['COUNT', 'SUM', 'MIN', 'MAX']:
            func = self._consume()[0]
            self._consume('LPAREN')

            if self._match('MULTIPLY') or self._consume()[1] == '*':
                arg = '*'
            else:
                # Roll back, this is a column ref
                self.token_pos -= 2
                # Parse as regular column ref
                while self._peek() and self._peek()[0] == 'IDENT':
                    parts.append(self._consume('IDENT')[1])
                    if self._match('DOT'):
                        self._consume('DOT')
                arg = '.'.join(parts)
                return {'type': 'column', 'name': arg}

            self._consume('RPAREN')
            return {'type': 'aggregate_call', 'function': func.lower(), 'arg': arg}

        # Regular column reference: table.column or column
        while self._peek() and self._peek()[0] == 'IDENT':
            parts.append(self._consume('IDENT')[1])
            if self._match('DOT'):
                self._consume('DOT')

        if not parts:
            raise SQLParseError("Expected column reference")

        name = '.'.join(parts)
        return {'type': 'column', 'name': name}

    def _parse_column_refs(self) -> list:
        """Parse comma-separated column references."""
        refs = []
        while True:
            col = self._parse_column_ref()
            refs.append(col)
            if not self._match('COMMA'):
                break
            self._consume('COMMA')
        return refs

    def _parse_boolean_expr(self) -> dict:
        """Parse a boolean expression (recursive descent)."""
        return self._parse_or_expr()

    def _parse_or_expr(self) -> dict:
        """Parse OR expressions."""
        left = self._parse_and_expr()

        while self._match('OR'):
            self._consume('OR')
            right = self._parse_and_expr()
            left = {
                'type': 'binary_op',
                'operator': 'OR',
                'left': left,
                'right': right
            }

        return left

    def _parse_and_expr(self) -> dict:
        """Parse AND expressions."""
        left = self._parse_unary_expr()

        while self._match('AND'):
            self._consume('AND')
            right = self._parse_unary_expr()
            left = {
                'type': 'binary_op',
                'operator': 'AND',
                'left': left,
                'right': right
            }

        return left

    def _parse_unary_expr(self) -> dict:
        """Parse NOT expressions and primary expressions."""
        if self._match('NOT'):
            self._consume('NOT')
            expr = self._parse_primary()
            return {
                'type': 'unary_op',
                'operator': 'NOT',
                'expr': expr
            }

        return self._parse_primary()

    def _parse_primary(self) -> dict:
        """Parse primary expressions: column, literal, or parenthesized expr."""
        # Check for parentheses
        if self._match('LPAREN'):
            self._consume('LPAREN')
            expr = self._parse_boolean_expr()
            self._consume('RPAREN')
            return expr

        # Check for aggregate functions in HAVING/WHERE
        if self._peek() and self._peek()[0] in ['COUNT', 'SUM', 'MIN', 'MAX']:
            func = self._consume()[0]
            self._consume('LPAREN')

            if self._match('MULTIPLY'):
                arg = '*'
                self._consume('MULTIPLY')
            else:
                arg = self._parse_column_ref()
                if arg['type'] != 'column':
                    raise SQLParseError("Aggregate function expects column reference")
                arg = arg['name']

            self._consume('RPAREN')
            return {'type': 'aggregate_call', 'function': func.lower(), 'arg': arg}

        # Column reference
        if self._peek() and self._peek()[0] == 'IDENT':
            col = self._parse_column_ref()

            # Check for comparison operator
            if self._peek() and self._peek()[0] in ['EQ', 'NE', 'LT', 'LE', 'GT', 'GE', 'LIKE']:
                op_token = self._consume()
                operator = op_token[0]
                right = self._parse_operand()
                return {
                    'type': 'comparison',
                    'left': col,
                    'operator': operator,
                    'right': right
                }

            return col

        # Literal
        return self._parse_operand()

    def _parse_operand(self) -> dict:
        """Parse an operand: literal, column ref, or aggregate."""
        token = self._peek()

        if token[0] == 'STRING_LITERAL':
            self._consume('STRING_LITERAL')
            value = token[1].strip('"\'')  # Remove quotes
            return {'type': 'literal', 'value': value, 'literal_type': 'string'}

        if token[0] == 'INTEGER_LITERAL':
            self._consume('INTEGER_LITERAL')
            value = int(token[1])
            return {'type': 'literal', 'value': value, 'literal_type': 'integer'}

        if token[0] == 'IDENT':
            return self._parse_column_ref()

        raise SQLParseError(f"Unexpected token: {token}")

    def _parse_order_items(self) -> list:
        """Parse ORDER BY items."""
        items = []
        while True:
            col = self._parse_column_ref()

            asc_desc = 'ASC'
            if self._match('ASC'):
                self._consume('ASC')
            elif self._match('DESC'):
                asc_desc = 'DESC'
                self._consume('DESC')

            items.append({
                'column': col,
                'direction': asc_desc
            })

            if not self._match('COMMA'):
                break
            self._consume('COMMA')

        return items


class CSVTableManager:
    """Manages loading and accessing CSV files as tables."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables = {}
        self._load_tables()

    def _load_tables(self):
        """Load all CSV files from the directory."""
        for csv_path in self.data_dir.rglob('*.csv'):
            # Calculate table name from path
            rel_path = csv_path.relative_to(self.data_dir)

            # Convert path to table name: use '.' for subdirectories
            # e.g., data/payments/pampas.csv -> payments.pampas
            path_parts = list(rel_path.parts)
            filename = path_parts[-1]
            table_name = filename[:-4]  # Remove .csv

            if len(path_parts) > 1:
                # Include parent directories with dots
                dir_parts = path_parts[:-1]
                table_name = '.'.join(dir_parts) + '.' + table_name

            try:
                df = pd.read_csv(csv_path, dtype=str)  # Read all as strings initially
                # Convert numeric columns later as needed
                self.tables[table_name.lower()] = {
                    'df': df,
                    'path': csv_path,
                    'columns': list(df.columns)
                }
                print(f"Loaded table '{table_name}' with {len(df)} rows and {len(df.columns)} columns")
            except Exception as e:
                print(f"Warning: Could not load {csv_path}: {e}", file=sys.stderr)

    def get_table(self, table_name: str) -> pd.DataFrame:
        """Get a table by name."""
        key = table_name.lower()
        if key not in self.tables:
            raise ValueError(f"Table '{table_name}' not found. Available: {list(self.tables.keys())}")
        return self.tables[key]['df'].copy()


class QueryExecutor:
    """Executes parsed SQL queries on loaded tables."""

    def __init__(self, table_manager: CSVTableManager):
        self.table_manager = table_manager

    def execute(self, parsed: dict) -> pd.DataFrame:
        """Execute a parsed query and return result DataFrame."""
        # Step 1: Load base tables
        tables = self._load_from_clause(parsed['from_clause'])

        # Step 2: Apply WHERE filter (before JOIN for performance)
        if parsed['where_expr']:
            for alias, df in tables.items():
                tables[alias] = self._apply_where(df, parsed['where_expr'], tables)

        # Step 3: Perform JOINs
        if len(tables) > 1:
            result_df = self._perform_joins(list(tables.values()), parsed['from_clause'])
        else:
            result_df = list(tables.values())[0]

        # Step 4: Apply WHERE again after JOIN if needed (for join conditions)
        if parsed['where_expr'] and len(tables) > 1:
            result_df = self._apply_where(result_df, parsed['where_expr'], tables)

        # Step 5: Apply GROUP BY and aggregates
        if parsed['group_by']:
            result_df = self._apply_group_by(result_df, parsed, tables)
        else:
            # Process SELECT without GROUP BY (handle aggregates)
            result_df = self._process_select_list(result_df, parsed['select_list'], tables, aggregated=False)

        # Step 6: Apply HAVING (after GROUP BY)
        if parsed['having_expr']:
            result_df = self._apply_having(result_df, parsed['having_expr'])

        # Step 7: Apply DISTINCT
        if parsed['distinct']:
            # Get column names from select list
            select_cols = self._get_select_columns(parsed['select_list'], tables)
            result_df = result_df[select_cols].drop_duplicates()

        # Step 8: Apply ORDER BY
        if parsed['order_by']:
            result_df = self._apply_order_by(result_df, parsed['order_by'])

        # Step 9: Apply LIMIT and OFFSET
        if parsed['offset'] is not None:
            result_df = result_df.iloc[parsed['offset']:]

        if parsed['limit'] is not None:
            result_df = result_df.head(parsed['limit'])

        # Step 10: Rename columns according to SELECT list
        result_df = self._rename_select_columns(result_df, parsed['select_list'])

        return result_df

    def _load_from_clause(self, from_clause: list) -> dict:
        """Load tables from FROM clause."""
        tables = {}

        # First item is the main table
        main_table = from_clause[0]
        if main_table['type'] == 'table':
            table_name = main_table['name']
            alias = main_table.get('alias', table_name.split('.')[-1].lower())
            tables[alias] = self.table_manager.get_table(table_name)

        return tables

    def _apply_where(self, df: pd.DataFrame, where_expr: dict, tables: dict) -> pd.DataFrame:
        """Apply WHERE filter to a DataFrame."""
        mask = self._evaluate_boolean_expr(where_expr, df, tables)
        return df[mask].reset_index(drop=True)

    def _apply_having(self, df: pd.DataFrame, having_expr: dict) -> pd.DataFrame:
        """Apply HAVING filter to grouped DataFrame."""
        mask = self._evaluate_boolean_expr(having_expr, df, {})
        return df[mask].reset_index(drop=True)

    def _evaluate_boolean_expr(self, expr: dict, df: pd.DataFrame, tables: dict) -> pd.Series:
        """Evaluate a boolean expression and return a boolean mask."""
        expr_type = expr.get('type')

        if expr_type == 'binary_op':
            left = self._evaluate_boolean_expr(expr['left'], df, tables)
            right = self._evaluate_boolean_expr(expr['right'], df, tables)

            if expr['operator'] == 'AND':
                return left & right
            elif expr['operator'] == 'OR':
                return left | right

        elif expr_type == 'unary_op':
            val = self._evaluate_boolean_expr(expr['expr'], df, tables)
            if expr['operator'] == 'NOT':
                return ~val

        elif expr_type == 'comparison':
            left_val = self._evaluate_expression(expr['left'], df, tables)
            right_val = self._evaluate_expression(expr['right'], df, tables)

            op = expr['operator']

            if op == 'EQ':
                return left_val == right_val
            elif op == 'NE':
                return left_val != right_val
            elif op == 'LT':
                return left_val < right_val
            elif op == 'LE':
                return left_val <= right_val
            elif op == 'GT':
                return left_val > right_val
            elif op == 'GE':
                return left_val >= right_val
            elif op == 'LIKE':
                # LIKE pattern matching (simple % wildcard)
                # right_val could be a literal or column
                if isinstance(right_val, pd.Series):
                    # If it's a column, use first value as pattern for comparison
                    pattern_str = str(right_val.iloc[0]) if len(right_val) > 0 else ''
                else:
                    pattern_str = str(right_val)
                # Convert SQL LIKE pattern to regex: % -> .*, _ -> .
                pattern = pattern_str.replace('%', '.*').replace('_', '.')
                return left_val.str.contains(f'^{pattern}$', regex=True, na=False)

        raise ValueError(f"Unknown expression type: {expr_type}")

    def _evaluate_expression(self, expr: dict, df: pd.DataFrame, tables: dict):
        """Evaluate an expression to a pandas Series."""
        expr_type = expr.get('type')

        if expr_type == 'column':
            col_name = expr['name']
            # Check if it's qualified (table.column)
            if '.' in col_name:
                # Find the table with this column
                for alias, table_df in tables.items():
                    if col_name.split('.')[-1] in table_df.columns:
                        return table_df[col_name.split('.')[-1]]
                # Also check in df (after join)
                if col_name in df.columns:
                    return df[col_name]
            else:
                if col_name in df.columns:
                    return df[col_name]
                # Check in tables
                for alias, table_df in tables.items():
                    if col_name in table_df.columns:
                        return table_df[col_name]

            raise ValueError(f"Column '{col_name}' not found")

        elif expr_type == 'aggregate_call':
            # Aggregate functions are handled separately in GROUP BY
            raise ValueError("Aggregate call not allowed in WHERE directly")

        elif expr_type == 'literal':
            return pd.Series([expr['value']] * len(df))

        raise ValueError(f"Unknown expression type: {expr_type}")

    def _perform_joins(self, tables: list, from_clause: list) -> pd.DataFrame:
        """Perform JOIN operations."""
        if len(tables) == 1:
            return tables[0]

        # Start with the first table
        result = tables[0]
        join_infos = from_clause[1:]  # Skip first table

        for join_info in join_infos:
            if join_info['type'] == 'join':
                join_table = join_info['table']
                join_type = join_info['join_type']
                on_predicate = join_info['on']

                # Get the join table data
                join_df = None
                if join_table['type'] == 'table':
                    table_name = join_table['name']
                    join_df = self.table_manager.get_table(table_name)

                if join_df is None:
                    raise ValueError(f"Table '{table_name}' not found for JOIN")

                # Get join column names from ON clause
                left_col = on_predicate['left']['name'].split('.')[-1]
                right_col = on_predicate['right']['name'].split('.')[-1]

                # Ensure columns exist
                if left_col not in result.columns:
                    raise ValueError(f"Column '{left_col}' not found in left table")
                if right_col not in join_df.columns:
                    raise ValueError(f"Column '{right_col}' not found in right table")

                # Perform the join
                if join_type == 'INNER':
                    result = pd.merge(result, join_df, left_on=left_col, right_on=right_col,
                                      how='inner', suffixes=('_left', '_right'))
                elif join_type == 'LEFT':
                    result = pd.merge(result, join_df, left_on=left_col, right_on=right_col,
                                      how='left', suffixes=('_left', '_right'))
                elif join_type == 'RIGHT':
                    result = pd.merge(result, join_df, left_on=left_col, right_on=right_col,
                                      how='right', suffixes=('_left', '_right'))
                elif join_type == 'FULL':
                    result = pd.merge(result, join_df, left_on=left_col, right_on=right_col,
                                      how='outer', suffixes=('_left', '_right'))

        return result

    def _apply_group_by(self, df: pd.DataFrame, parsed: dict, tables: dict) -> pd.DataFrame:
        """Apply GROUP BY with aggregate functions."""
        group_cols = []
        for col_ref in parsed['group_by']:
            col_name = col_ref['name']
            # Get column from df or tables
            if col_name in df.columns:
                group_cols.append(col_name)
            else:
                for alias, table_df in tables.items():
                    if col_name in table_df.columns:
                        group_cols.append(col_name)
                        break

        if not group_cols:
            raise ValueError("GROUP BY columns not found")

        # Process select list to separate aggregates from regular columns
        agg_funcs = {}
        select_cols = []

        for select_item in parsed['select_list']:
            if select_item['type'] == 'aggregate':
                func = select_item['function']
                arg = select_item['arg']
                key = f"{func}_{arg}"
                agg_funcs[key] = func
                if func == 'count':
                    select_cols.append(key)
                else:
                    select_cols.append(key)
            else:
                col_name = select_item['expr']['name']
                select_cols.append(col_name)

        # Build aggregation dict
        agg_dict = {}
        for select_item in parsed['select_list']:
            if select_item['type'] == 'aggregate':
                func = select_item['function']
                arg = select_item['arg']
                key = f"{func}_{arg}"

                if arg == '*':
                    agg_dict[key] = 'count'
                elif func == 'count':
                    agg_dict[key] = 'count'
                elif func == 'sum':
                    agg_dict[key] = 'sum'
                elif func == 'min':
                    agg_dict[key] = 'min'
                elif func == 'max':
                    agg_dict[key] = 'max'

        # Perform groupby
        grouped = df.groupby(group_cols, as_index=False)

        # If there are aggregates, aggregate them
        if agg_dict:
            result = grouped.agg(agg_dict)
        else:
            result = grouped.first().reset_index()

        # Flatten column names if needed
        if isinstance(result.columns, pd.MultiIndex):
            result.columns = ['_'.join(col).strip() if col[1] else col[0] for col in result.columns]

        return result

    def _process_select_list(self, df: pd.DataFrame, select_list: list, tables: dict,
                             aggregated: bool = False) -> pd.DataFrame:
        """Process SELECT list to build result columns."""
        result = pd.DataFrame()

        for select_item in select_list:
            if select_item['type'] == 'aggregate':
                func = select_item['function']
                arg = select_item['arg']

                if arg == '*':
                    if func == 'count':
                        val = [len(df)]
                    else:
                        raise ValueError(f"Invalid aggregate: {func}(*)")
                elif func == 'count':
                    val = df[arg].count()
                elif func == 'sum':
                    val = pd.to_numeric(df[arg], errors='coerce').sum()
                elif func == 'min':
                    val = pd.to_numeric(df[arg], errors='coerce').min()
                elif func == 'max':
                    val = pd.to_numeric(df[arg], errors='coerce').max()
                else:
                    raise ValueError(f"Unknown aggregate: {func}")

                # Create a single-row DataFrame for the aggregate result
                if len(result) == 0:
                    result = pd.DataFrame({f"{func}_{arg}": [val]})
                else:
                    result[f"{func}_{arg}"] = [val]

            else:
                col_name = select_item['expr']['name']
                alias = select_item.get('alias')

                # Get column value
                if col_name in df.columns:
                    val = df[col_name]
                else:
                    # Check if qualified name matches
                    for c in df.columns:
                        if c.endswith('.' + col_name.split('.')[-1]):
                            val = df[c]
                            break
                    else:
                        raise ValueError(f"Column '{col_name}' not found in result")

                output_col = alias if alias else col_name.split('.')[-1]
                result[output_col] = val

        return result

    def _get_select_columns(self, select_list: list, tables: dict) -> list:
        """Get column names from SELECT list for DISTINCT."""
        cols = []
        for item in select_list:
            if item['type'] == 'column':
                col_name = item['expr']['name']
                cols.append(col_name.split('.')[-1])
            elif item['type'] == 'aggregate':
                cols.append(f"{item['function']}_{item['arg']}")
        return cols

    def _rename_select_columns(self, df: pd.DataFrame, select_list: list) -> pd.DataFrame:
        """Rename DataFrame columns according to SELECT list aliases."""
        rename_map = {}
        for i, item in enumerate(select_list):
            if item['type'] == 'aggregate':
                old_name = f"{item['function']}_{item['arg']}"
                alias = item.get('alias')
                if alias:
                    rename_map[old_name] = alias
            elif item['type'] == 'column':
                col_name = item['expr']['name']
                alias = item.get('alias')
                if alias:
                    # Find the matching column in df
                    for c in df.columns:
                        if c == col_name.split('.')[-1] or c.endswith('.' + col_name.split('.')[-1]):
                            rename_map[c] = alias
                            break

        return df.rename(columns=rename_map)

    def _apply_order_by(self, df: pd.DataFrame, order_by: list) -> pd.DataFrame:
        """Apply ORDER BY to DataFrame."""
        if not order_by:
            return df

        sort_cols = []
        ascending = []

        for order_item in order_by:
            col_ref = order_item['column']
            col_name = col_ref['name']
            direction = order_item['direction']

            # Find column in df
            sort_col = None
            for c in df.columns:
                if c == col_name or c.endswith('.' + col_name.split('.')[-1]):
                    sort_col = c
                    break

            if sort_col is None:
                raise ValueError(f"Column '{col_name}' not found for ORDER BY")

            sort_cols.append(sort_col)
            ascending.append(direction == 'ASC')

        return df.sort_values(by=sort_cols, ascending=ascending).reset_index(drop=True)


def format_markdown_table(df: pd.DataFrame) -> str:
    """Format DataFrame as a markdown table."""
    if df.empty:
        return "| No results |\n|------------|\n|            |"

    # Build header
    headers = list(df.columns)
    header_row = "| " + " | ".join(str(h) for h in headers) + " |"

    # Build separator
    separator = "| " + " | ".join("---" for _ in headers) + " |"

    # Build data rows
    rows = []
    for _, row in df.iterrows():
        row_data = []
        for val in row:
            if pd.isna(val):
                row_data.append("NULL")
            else:
                row_data.append(str(val))
        rows.append("| " + " | ".join(row_data) + " |")

    return "\n".join([header_row, separator] + rows)


def save_csv(df: pd.DataFrame, output_path: str):
    """Save DataFrame to CSV file."""
    df.to_csv(output_path, index=False, na_rep='NULL')


def read_sql_file_or_string(sql_input: str) -> str:
    """Read SQL from file or return as string if it's not a valid file path."""
    # Check if it's a file path
    if os.path.isfile(sql_input):
        with open(sql_input, 'r') as f:
            return f.read()
    return sql_input


def main():
    parser = argparse.ArgumentParser(
        description='Run read-only SQL queries across a folder of CSV files.'
    )
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True,
                        help='SQL query string or path to SQL file')
    parser.add_argument('--out', required=False, help='Output path for CSV result')

    args = parser.parse_args()

    # Read SQL
    sql_text = read_sql_file_or_string(args.sql)

    # Parse SQL
    try:
        sql_parser = SQLParser(sql_text)
        parsed_query = sql_parser.parse()
    except SQLParseError as e:
        print(f"SQL Parse Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Load tables
    try:
        table_manager = CSVTableManager(args.data)
    except Exception as e:
        print(f"Error loading tables: {e}", file=sys.stderr)
        sys.exit(1)

    # Execute query
    try:
        executor = QueryExecutor(table_manager)
        result_df = executor.execute(parsed_query)
    except Exception as e:
        print(f"Query execution error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Output results
    markdown_output = format_markdown_table(result_df)
    print(markdown_output)

    # Save to file if requested
    if args.out:
        try:
            save_csv(result_df, args.out)
            print(f"\n\nResult also saved to {args.out}", file=sys.stderr)
        except Exception as e:
            print(f"Error saving output: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
