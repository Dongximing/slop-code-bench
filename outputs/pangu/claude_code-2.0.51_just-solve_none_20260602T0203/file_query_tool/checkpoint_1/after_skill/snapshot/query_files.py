#!/usr/bin/env python3
"""
SQL Query Engine for CSV Files

A lightweight CLI to run read-only SQL queries across a folder of CSVs
with matching headers.
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import pandas as pd
from pandas import DataFrame


# =============================================================================
# SQL Parser - Recursive Descent Parser
# =============================================================================

class Tokenizer:
    """Tokenizes SQL strings."""

    KEYWORDS = {
        'SELECT', 'FROM', 'WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER',
        'LIMIT', 'OFFSET', 'DISTINCT', 'INNER', 'LEFT', 'RIGHT', 'FULL',
        'JOIN', 'ON', 'AND', 'OR', 'NOT', 'AS', 'ASC', 'DESC'
    }

    def __init__(self, sql: str):
        self.sql = sql.strip()
        self.pos = 0
        self.tokens: List[Tuple[str, str]] = []
        self.tokenize()

    def tokenize(self):
        """Convert SQL string into tokens."""
        length = len(self.sql)

        while self.pos < length:
            char = self.sql[self.pos]

            # Skip whitespace
            if char.isspace():
                self.pos += 1
                continue

            # Identifier or keyword
            if char.isalpha() or char == '_':
                start = self.pos
                while self.pos < length and (self.sql[self.pos].isalnum() or self.sql[self.pos] == '_'):
                    self.pos += 1
                ident = self.sql[start:self.pos]
                # Check if it's a number in disguise
                if ident.isdigit():
                    self.tokens.append(('NUMBER', ident))
                elif ident.replace('.', '').isdigit():
                    self.tokens.append(('FLOAT', ident))
                elif ident.upper() in self.KEYWORDS:
                    self.tokens.append(('KEYWORD', ident.upper()))
                else:
                    self.tokens.append(('IDENTIFIER', ident))
                continue

            # Number (digits)
            if char.isdigit():
                start = self.pos
                while self.pos < length and self.sql[self.pos].isdigit():
                    self.pos += 1
                if self.pos < length and self.sql[self.pos] == '.':
                    # Decimal
                    self.pos += 1
                    while self.pos < length and self.sql[self.pos].isdigit():
                        self.pos += 1
                    self.tokens.append(('FLOAT', self.sql[start:self.pos]))
                else:
                    self.tokens.append(('NUMBER', self.sql[start:self.pos]))
                continue

            # String literal
            if char == "'":
                self.pos += 1
                start = self.pos
                while self.pos < length and self.sql[self.pos] != "'":
                    self.pos += 1
                self.tokens.append(('STRING', self.sql[start:self.pos]))
                self.pos += 1  # Skip closing quote
                continue

            # Operators and punctuation
            if char == '*':
                self.tokens.append(('STAR', '*'))
            elif char == '(':
                self.tokens.append(('LPAREN', '('))
            elif char == ')':
                self.tokens.append(('RPAREN', ')'))
            elif char == ',':
                self.tokens.append(('COMMA', ','))
            elif char == '.':
                self.tokens.append(('DOT', '.'))
            elif char == '=':
                self.tokens.append(('EQ', '='))
            elif char == '<':
                if self.pos < length and self.sql[self.pos + 1] == '=':
                    self.tokens.append(('LE', '<='))
                    self.pos += 1
                elif self.pos < length and self.sql[self.pos + 1] == '>':
                    self.tokens.append(('NE', '<>'))
                    self.pos += 1
                else:
                    self.tokens.append(('LT', '<'))
            elif char == '>':
                if self.pos < length and self.sql[self.pos + 1] == '=':
                    self.tokens.append(('GE', '>='))
                    self.pos += 1
                else:
                    self.tokens.append(('GT', '>'))
            elif char == '!':
                if self.pos < length and self.sql[self.pos + 1] == '=':
                    self.tokens.append(('NE', '!='))
                    self.pos += 1
                else:
                    raise ValueError(f"Unexpected character at position {self.pos}: {char}")
            else:
                raise ValueError(f"Unexpected character at position {self.pos}: {char}")

            self.pos += 1

        # Add EOF token
        self.tokens.append(('EOF', ''))

    def peek(self, expected: str = None, token_type: str = None) -> Optional[str]:
        """Look at the next token without consuming it.

        Args:
            expected: Expected token type (e.g., 'KEYWORD', 'IDENTIFIER')
            token_type: Expected token value (e.g., 'SELECT', 'FROM')

        Returns:
            Token value if match, None otherwise
        """
        if len(self.tokens) == 0:
            return None

        # If checking for token type (like 'KEYWORD')
        if token_type:
            if self.tokens[0][0] == token_type:
                return self.tokens[0][1]
            return None

        # If checking for specific value
        if expected:
            # First check by value
            if self.tokens[0][1] == expected:
                return self.tokens[0][1]
            # Also check by type if value didn't match
            if self.tokens[0][0] == expected:
                return self.tokens[0][1]
            return None

        # Return token type
        return self.tokens[0][0]

    def consume(self, expected_type: str = None, expected_value: str = None) -> Tuple[str, str]:
        """Consume and return the next token."""
        if len(self.tokens) == 0:
            raise ValueError("Unexpected end of SQL")

        token = self.tokens.pop(0)

        if expected_type and token[0] != expected_type:
            raise ValueError(f"Expected token type {expected_type}, got {token[0]}")

        if expected_value and token[1] != expected_value:
            raise ValueError(f"Expected value {expected_value}, got {token[1]}")

        return token

    def match(self, value: str) -> bool:
        """Check if the next token matches the given value."""
        if len(self.tokens) > 0 and self.tokens[0][1] == value:
            self.tokens.pop(0)
            return True
        return False


class SQLParser:
    """Parses SQL queries into an AST."""

    def __init__(self, sql: str):
        self.lexer = Tokenizer(sql)
        self.select_items: List[Dict] = []
        self.from_tables: List[Dict] = []
        self.where_condition: Optional[str] = None
        self.group_by: Optional[List[str]] = None
        self.having_condition: Optional[str] = None
        self.order_by: Optional[List[Dict]] = None
        self.limit: Optional[int] = None
        self.offset: Optional[int] = None
        self.parse()

    def parse(self):
        """Main parse method."""
        self.parse_select()
        self.parse_from()
        self.parse_where()
        self.parse_group_by()
        self.parse_having()
        self.parse_order_by()
        self.parse_limit()
        self.parse_offset()

        # Ensure we consumed all tokens
        if self.lexer.peek() != 'EOF':
            raise ValueError(f"Unexpected token at end: {self.lexer.peek()}")

    def parse_select(self):
        """Parse SELECT clause."""
        self.lexer.consume('KEYWORD', 'SELECT')

        # Check for DISTINCT
        distinct_all = False
        if self.lexer.peek(expected='DISTINCT'):
            self.lexer.consume('KEYWORD', 'DISTINCT')
            distinct_all = True

        self.select_items = []

        while True:
            # Parse expression
            expr = self.parse_expression()

            # Check for alias
            alias = None
            if self.lexer.peek(expected='AS'):
                self.lexer.consume('KEYWORD', 'AS')
                alias = self.parse_identifier()
            elif self.lexer.peek(token_type='IDENTIFIER'):
                # Potential alias without AS keyword
                alias_candidate = self.lexer.tokens[0][1]
                if alias_candidate.upper() not in ['WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                    alias = alias_candidate
                    self.lexer.consume('IDENTIFIER')

            self.select_items.append({
                'expr': expr,
                'alias': alias,
                'distinct': distinct_all
            })

            # Check for comma or next clause
            if self.lexer.peek(expected=','):
                self.lexer.consume('COMMA')
            elif self.lexer.peek(expected='FROM'):
                break
            else:
                raise ValueError(f"Expected ',' or 'FROM' after SELECT item")

    def parse_expression(self) -> str:
        """Parse a SELECT expression."""
        # Check for aggregate functions
        if self.lexer.peek(expected='COUNT'):
            self.lexer.consume('KEYWORD', 'COUNT')
            self.lexer.consume('LPAREN')
            if self.lexer.peek(expected='*'):
                self.lexer.consume('STAR')
                self.lexer.consume('RPAREN')
                return "COUNT(*)"
            else:
                col = self.parse_identifier()
                self.lexer.consume('RPAREN')
                return f"COUNT({col})"

        if self.lexer.peek(expected='SUM'):
            self.lexer.consume('KEYWORD', 'SUM')
            self.lexer.consume('LPAREN')
            col = self.parse_identifier()
            self.lexer.consume('RPAREN')
            return f"SUM({col})"

        if self.lexer.peek(expected='MIN'):
            self.lexer.consume('KEYWORD', 'MIN')
            self.lexer.consume('LPAREN')
            col = self.parse_identifier()
            self.lexer.consume('RPAREN')
            return f"MIN({col})"

        if self.lexer.peek(expected='MAX'):
            self.lexer.consume('KEYWORD', 'MAX')
            self.lexer.consume('LPAREN')
            col = self.parse_identifier()
            self.lexer.consume('RPAREN')
            return f"MAX({col})"

        # Handle wildcard
        if self.lexer.peek(expected='*'):
            self.lexer.consume('STAR')
            return '*'

        # Identifier (could be column or table-qualified column)
        return self.parse_identifier_or_qualified()

    def parse_identifier_or_qualified(self) -> str:
        """Parse an identifier, possibly qualified with table name (table.column)."""
        ident = self.parse_identifier()

        if self.lexer.peek(expected='.'):
            self.lexer.consume('DOT')
            next_ident = self.parse_identifier()
            return f"{ident}.{next_ident}"

        return ident

    def parse_identifier(self) -> str:
        """Parse a single identifier."""
        if self.lexer.peek(token_type='IDENTIFIER'):
            return self.lexer.consume('IDENTIFIER')[1]
        if self.lexer.peek(token_type='NUMBER'):
            return self.lexer.consume('NUMBER')[1]
        raise ValueError(f"Expected identifier, got {self.lexer.peek()}")

    def parse_from(self):
        """Parse FROM clause and JOINs."""
        self.lexer.consume('KEYWORD', 'FROM')
        self.from_tables = []

        # First table
        table_info = self.parse_table_ref()
        self.from_tables.append(table_info)

        # JOINs
        while True:
            join_type = None
            if self.lexer.peek(expected='INNER'):
                self.lexer.consume('KEYWORD', 'INNER')
                join_type = 'INNER'
            elif self.lexer.peek(expected='LEFT'):
                self.lexer.consume('KEYWORD', 'LEFT')
                join_type = 'LEFT'
            elif self.lexer.peek(expected='RIGHT'):
                self.lexer.consume('KEYWORD', 'RIGHT')
                join_type = 'RIGHT'
            elif self.lexer.peek(expected='FULL'):
                self.lexer.consume('KEYWORD', 'FULL')
                join_type = 'FULL'

            # Check if there's a JOIN keyword
            if self.lexer.peek(expected='JOIN'):
                self.lexer.consume('KEYWORD', 'JOIN')
            else:
                # No JOIN keyword means no more joins
                if join_type:
                    raise ValueError("JOIN keyword expected after JOIN type")
                break

            table_info = self.parse_table_ref()
            table_info['join_type'] = join_type or 'INNER'
            self.from_tables.append(table_info)

    def parse_table_ref(self) -> Dict:
        """Parse a table reference (possibly qualified)."""
        # Parse table name (could be qualified with dot)
        name_parts = []
        name_parts.append(self.parse_identifier())

        while self.lexer.peek(expected='.'):
            self.lexer.consume('DOT')
            name_parts.append(self.parse_identifier())

        table_name = '.'.join(name_parts)

        # Check for alias
        alias = None
        if self.lexer.peek(expected='AS'):
            self.lexer.consume('KEYWORD', 'AS')
            alias = self.parse_identifier()
        elif self.lexer.peek(token_type='IDENTIFIER'):
            alias_candidate = self.lexer.tokens[0][1]
            if alias_candidate.upper() not in ['ON', 'WHERE', 'GROUP', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                alias = alias_candidate
                self.lexer.consume('IDENTIFIER')

        result = {'name': table_name, 'alias': alias}

        # Check for JOIN condition
        if self.lexer.peek(expected='ON'):
            self.lexer.consume('KEYWORD', 'ON')
            on_clause = self.parse_equality_predicate()
            result['on_clause'] = on_clause

        return result

    def parse_equality_predicate(self) -> Tuple[str, str]:
        """Parse an equality predicate for JOIN ON clause."""
        left = self.parse_identifier()
        self.lexer.consume('EQ')
        right = self.parse_identifier()
        return (left, right)

    def parse_where(self):
        """Parse WHERE clause."""
        if not self.lexer.match('WHERE'):
            return

        self.where_condition = self.parse_boolean_expr()

    def parse_boolean_expr(self) -> str:
        """Parse boolean expression for WHERE/HAVING."""
        return self.parse_or_expr()

    def parse_or_expr(self) -> str:
        """Parse OR expressions."""
        left = self.parse_and_expr()

        while self.lexer.match('OR'):
            right = self.parse_and_expr()
            left = f"({left} OR {right})"

        return left

    def parse_and_expr(self) -> str:
        """Parse AND expressions."""
        left = self.parse_not_expr()

        while self.lexer.match('AND'):
            right = self.parse_not_expr()
            left = f"({left} AND {right})"

        return left

    def parse_not_expr(self) -> str:
        """Parse NOT expressions."""
        if self.lexer.match('NOT'):
            expr = self.parse_not_expr()
            return f"NOT {expr}"
        return self.parse_comparison()

    def parse_comparison(self) -> str:
        """Parse comparison expressions."""
        left = self.parse_primary()

        # Check for comparison operators
        if self.lexer.peek(expected='='):
            self.lexer.consume('EQ')
            right = self.parse_primary()
            return f"({left} = {right})"
        if self.lexer.peek(expected='!=') or self.lexer.peek(expected='<>'):
            self.lexer.peek(expected='!=') and self.lexer.consume('NE', '!=') or self.lexer.consume('NE', '<>')
            right = self.parse_primary()
            return f"({left} != {right})"
        if self.lexer.peek(expected='<'):
            self.lexer.consume('LT')
            right = self.parse_primary()
            return f"({left} < {right})"
        if self.lexer.peek(expected='>'):
            self.lexer.consume('GT')
            right = self.parse_primary()
            return f"({left} > {right})"
        if self.lexer.peek(expected='<='):
            self.lexer.consume('LE')
            right = self.parse_primary()
            return f"({left} <= {right})"
        if self.lexer.peek(expected='>='):
            self.lexer.consume('GE')
            right = self.parse_primary()
            return f"({left} >= {right})"
        if self.lexer.match('LIKE'):
            pattern = self.parse_primary()
            return f"({left} LIKE {pattern})"

        return left

    def parse_primary(self) -> str:
        """Parse primary expressions (parentheses, literals, identifiers)."""
        # Parentheses
        if self.lexer.peek(expected='('):
            self.lexer.consume('LPAREN')
            expr = self.parse_boolean_expr()
            self.lexer.consume('RPAREN')
            return f"({expr})"

        # String literal
        if self.lexer.peek(token_type='STRING'):
            value = self.lexer.consume('STRING')[1]
            return f"'{value}'"

        # Number
        if self.lexer.peek(token_type='NUMBER'):
            return self.lexer.consume('NUMBER')[1]
        if self.lexer.peek(token_type='FLOAT'):
            return self.lexer.consume('FLOAT')[1]

        # Identifier (column or table-qualified column)
        return self.parse_identifier_or_qualified()

    def parse_group_by(self):
        """Parse GROUP BY clause."""
        if not self.lexer.match('GROUP'):
            return

        self.lexer.consume('KEYWORD', 'BY')
        self.group_by = []

        while True:
            col = self.parse_identifier()
            self.group_by.append(col)

            if self.lexer.peek(expected=','):
                self.lexer.consume('COMMA')
            else:
                break

    def parse_having(self):
        """Parse HAVING clause."""
        if not self.lexer.match('HAVING'):
            return

        self.having_condition = self.parse_boolean_expr()

    def parse_order_by(self):
        """Parse ORDER BY clause."""
        if not self.lexer.match('ORDER'):
            return

        self.lexer.consume('KEYWORD', 'BY')
        self.order_by = []

        while True:
            expr = self.parse_expression()
            asc = True

            if self.lexer.match('ASC'):
                asc = True
            elif self.lexer.match('DESC'):
                asc = False

            self.order_by.append({'expr': expr, 'asc': asc})

            if self.lexer.peek(expected=','):
                self.lexer.consume('COMMA')
            else:
                break

    def parse_limit(self):
        """Parse LIMIT clause."""
        if not self.lexer.match('LIMIT'):
            return

        value = self.lexer.consume('NUMBER')[1]
        self.limit = int(value)

    def parse_offset(self):
        """Parse OFFSET clause."""
        if not self.lexer.match('OFFSET'):
            return

        value = self.lexer.consume('NUMBER')[1]
        self.offset = int(value)

    def to_dict(self) -> Dict:
        """Convert parsed query to dictionary representation."""
        return {
            'select': self.select_items,
            'tables': self.from_tables,
            'where': self.where_condition,
            'group_by': self.group_by,
            'having': self.having_condition,
            'order_by': self.order_by,
            'limit': self.limit,
            'offset': self.offset
        }


def parse_sql(sql: str) -> Dict:
    """Parse SQL string into AST."""
    parser = SQLParser(sql)
    return parser.to_dict()


# =============================================================================
# CSV Table Loader
# =============================================================================

class CSVTable:
    """Represents a CSV table with data and metadata."""

    def __init__(self, name: str, filepath: str, df: DataFrame):
        self.name = name
        self.filepath = filepath
        self.df = df
        self.columns = list(df.columns)
        self._alias = None

    @property
    def alias(self) -> str:
        return self._alias

    @alias.setter
    def alias(self, value: str):
        self._alias = value

    def get_effective_name(self) -> str:
        """Get the effective table name (alias or actual name)."""
        return self._alias or self.name

    @classmethod
    def load(cls, name: str, filepath: str) -> 'CSVTable':
        """Load a CSV file into a table."""
        df = pd.read_csv(filepath)
        if not df.empty:
            # Validate that all columns have non-empty names
            if any(not col or col.strip() == '' for col in df.columns):
                raise ValueError(f"File {filepath} has invalid headers (empty column names)")
        return cls(name, filepath, df)


class CSVDatabase:
    """Manages loading and querying CSV files from a directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, CSVTable] = {}
        self._load_tables()

    def _load_tables(self):
        """Load all CSV files from the data directory."""
        for csv_path in self.data_dir.rglob("*.csv"):
            # Get relative path from data_dir
            rel_path = csv_path.relative_to(self.data_dir)

            # Generate table name
            # For nested directories, replace path separators with dots
            # If the filename contains dots (not for nesting), replace with underscores
            parts = str(rel_path.with_suffix('')).split(os.sep)
            filename_part = parts[-1]

            # If filename has dots, replace with underscores (not for nesting)
            if '.' in filename_part:
                filename_part = filename_part.replace('.', '_')
                parts[-1] = filename_part

            table_name = '.'.join(parts)

            table = CSVTable.load(table_name, str(csv_path))
            self.tables[table_name] = table

    def get_table(self, name: str) -> CSVTable:
        """Get a table by name."""
        if name in self.tables:
            return self.tables[name]
        raise ValueError(f"Table '{name}' not found. Available tables: {list(self.tables.keys())}")


# =============================================================================
# Query Executor
# =============================================================================

class QueryExecutor:
    """Executes parsed SQL queries against CSV tables."""

    def __init__(self, db: CSVDatabase):
        self.db = db

    def execute(self, ast: Dict) -> DataFrame:
        """Execute a parsed SQL query."""
        # Step 1: Resolve table references
        tables = []
        for table_ref in ast['tables']:
            table = self.db.get_table(table_ref['name'])
            if table_ref.get('alias'):
                table.alias = table_ref['alias']
            tables.append(table)

        # Step 2: Apply JOINs and get the base dataframe
        result_df = self._apply_joins(tables, ast['tables'])

        # Step 3: Apply WHERE clause
        if ast.get('where'):
            result_df = self._apply_where(result_df, ast['where'], ast['tables'])

        # Step 4: Apply GROUP BY and aggregations
        if ast.get('group_by'):
            result_df = self._apply_group_by(result_df, ast, tables)
        else:
            result_df = self._apply_select(result_df, ast['select'], tables)

        # Step 5: Apply HAVING clause
        if ast.get('having'):
            result_df = self._apply_having(result_df, ast['having'], ast.get('group_by', []))

        # Step 6: Apply ORDER BY
        if ast.get('order_by'):
            result_df = self._apply_order_by(result_df, ast['order_by'])

        # Step 7: Apply LIMIT and OFFSET
        if ast.get('limit') is not None:
            result_df = self._apply_limit(result_df, ast['limit'], ast.get('offset', 0))

        return result_df

    def _apply_joins(self, tables: List[CSVTable], table_refs: List[Dict]) -> DataFrame:
        """Apply JOIN operations between tables."""
        if len(tables) == 1:
            return tables[0].df.copy()

        # Start with first table
        result = tables[0].df.copy()

        # Apply subsequent joins
        for i, (table, table_ref) in enumerate(zip(tables[1:], table_refs[1:])):
            on_clause = table_ref.get('on_clause')
            join_type = table_ref.get('join_type', 'INNER')

            # Build the join condition
            if on_clause:
                left_col, right_col = on_clause

                # Resolve column names
                left_col_name = self._resolve_column_name(left_col, tables[:i+1], result.columns.tolist())
                right_col_name = self._resolve_column_name(right_col, [table], table.df.columns.tolist())

                # Perform the join
                if join_type == 'INNER':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name,
                                      right_on=right_col_name,
                                      how='inner',
                                      suffixes=('', '_dup'))
                elif join_type == 'LEFT':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name,
                                      right_on=right_col_name,
                                      how='left',
                                      suffixes=('', '_dup'))
                elif join_type == 'RIGHT':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name,
                                      right_on=right_col_name,
                                      how='right',
                                      suffixes=('', '_dup'))
                elif join_type == 'FULL':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name,
                                      right_on=right_col_name,
                                      how='outer',
                                      suffixes=('', '_dup'))
            else:
                # Cross join (shouldn't happen with valid SQL)
                result = pd.merge(result, table.df, how=join_type.lower(), suffixes=('', '_dup'))

        return result

    def _resolve_column_name(self, col_expr: str, tables: List[CSVTable], available_columns: List[str]) -> str:
        """Resolve a column expression to a column name in the dataframe."""
        # If it's already in the dataframe, return it
        if col_expr in available_columns:
            return col_expr

        # Check for table-qualified column
        if '.' in col_expr:
            parts = col_expr.split('.')
            table_alias_or_name = parts[0]
            col_name = '.'.join(parts[1:])

            # Find the table
            for table in tables:
                effective_name = table.get_effective_name()
                if table_alias_or_name == effective_name or table_alias_or_name == table.name:
                    # Check if column exists in this table
                    if col_name in table.columns:
                        # Check if column exists in merged dataframe
                        if col_name in available_columns:
                            return col_name
                        # Check for suffixed version
                        for c in available_columns:
                            if c == col_name:
                                return c

        # Try direct match
        for c in available_columns:
            if c == col_expr:
                return c

        raise ValueError(f"Column '{col_expr}' not found in {available_columns}")

    def _apply_where(self, df: DataFrame, where_expr: str, table_refs: List[Dict]) -> DataFrame:
        """Apply WHERE clause filtering."""
        # Create a mask by evaluating the expression
        mask = self._evaluate_boolean_expr(df, where_expr, table_refs)
        return df[mask].reset_index(drop=True)

    def _evaluate_boolean_expr(self, df: DataFrame, expr: str, table_refs: List[Dict]) -> pd.Series:
        """Evaluate a boolean expression against a dataframe."""
        expr = expr.strip()

        # Handle parentheses
        if expr.startswith('(') and expr.endswith(')'):
            inner = expr[1:-1].strip()
            return self._evaluate_boolean_expr(df, inner, table_refs)

        # Handle NOT
        if expr.upper().startswith('NOT '):
            inner = expr[4:].strip()
            return ~self._evaluate_boolean_expr(df, inner, table_refs)

        # Handle AND
        if ' AND ' in expr:
            parts = self._split_by_operator(expr, 'AND')
            if len(parts) == 2:
                left_mask = self._evaluate_boolean_expr(df, parts[0], table_refs)
                right_mask = self._evaluate_boolean_expr(df, parts[1], table_refs)
                return left_mask & right_mask

        # Handle OR
        if ' OR ' in expr:
            parts = self._split_by_operator(expr, 'OR')
            if len(parts) == 2:
                left_mask = self._evaluate_boolean_expr(df, parts[0], table_refs)
                right_mask = self._evaluate_boolean_expr(df, parts[1], table_refs)
                return left_mask | right_mask

        # Handle comparisons and LIKE
        return self._evaluate_comparison(df, expr, table_refs)

    def _split_by_operator(self, expr: str, op: str) -> List[str]:
        """Split expression by operator, respecting parentheses."""
        depth = 0
        current = ""
        parts = []

        i = 0
        while i < len(expr):
            c = expr[i]

            if c == '(':
                depth += 1
                current += c
            elif c == ')':
                depth -= 1
                current += c
            elif depth == 0 and expr[i:i+len(op)] == op and (i == 0 or not expr[i-1].isalnum()):
                # Check it's not part of a keyword like 'INNER', 'LEFT', etc.
                if op.upper() in ['AND', 'OR']:
                    parts.append(current.strip())
                    current = ""
                    i += len(op) - 1  # Will increment at end
            else:
                current += c

            i += 1

        if current:
            parts.append(current.strip())

        return parts

    def _evaluate_comparison(self, df: DataFrame, expr: str, table_refs: List[Dict]) -> pd.Series:
        """Evaluate a comparison expression."""
        # Handle LIKE
        if ' LIKE ' in expr:
            parts = expr.split(' LIKE ', 1)
            left = parts[0].strip()
            right = parts[1].strip()

            left_val = self._evaluate_value(df, left, table_refs)
            pattern = right.strip("'")

            # Convert LIKE pattern to regex
            pattern = pattern.replace('%', '.*').replace('_', '.')

            if isinstance(left_val, pd.Series):
                return left_val.apply(lambda x: bool(re.match(pattern, str(x))) if pd.notna(x) else False)
            return False

        # Handle =, !=, <>, <, <=, >, >=
        operators = [
            ('=', 'eq'),
            ('!=', 'ne'),
            ('<>', 'ne'),
            ('<=', 'le'),
            ('<', 'lt'),
            ('>=', 'ge'),
            ('>', 'gt')
        ]

        for op_symbol, op_name in operators:
            if f' {op_symbol} ' in expr:
                parts = expr.split(f' {op_symbol} ', 1)
                left = parts[0].strip()
                right = parts[1].strip()

                left_val = self._evaluate_value(df, left, table_refs)
                right_val = self._evaluate_value(df, right, table_refs)

                # Apply the operator
                if isinstance(left_val, pd.Series):
                    if isinstance(right_val, pd.Series):
                        op_method = getattr(left_val, op_name)
                        return op_method(right_val)
                    else:
                        op_method = getattr(left_val, op_name)
                        return op_method(right_val)
                else:
                    # Left is a literal
                    if isinstance(right_val, pd.Series):
                        # Flip the operator for commutativity
                        if op_name == 'eq':
                            return right_val == left_val
                        elif op_name == 'ne':
                            return right_val != left_val
                        elif op_name == 'lt':
                            return right_val > left_val
                        elif op_name == 'le':
                            return right_val >= left_val
                        elif op_name == 'gt':
                            return right_val < left_val
                        elif op_name == 'ge':
                            return right_val <= left_val

        raise ValueError(f"Unsupported comparison: {expr}")

    def _evaluate_value(self, df: DataFrame, value_expr: str, table_refs: List[Dict]) -> Union[pd.Series, Any]:
        """Evaluate a value expression (column reference or literal)."""
        value_expr = value_expr.strip()

        # String literal
        if value_expr.startswith("'") and value_expr.endswith("'"):
            return value_expr[1:-1]

        # Number
        try:
            if '.' in value_expr:
                return float(value_expr)
            return int(value_expr)
        except ValueError:
            pass

        # Column reference
        if value_expr in df.columns:
            return df[value_expr]

        # Table-qualified column
        if '.' in value_expr:
            parts = value_expr.split('.')
            table_alias = parts[0]
            col_name = '.'.join(parts[1:])

            # Find the table and get column
            for table_ref in table_refs:
                effective_name = table_ref.get('alias') or table_ref['name']
                if table_alias == effective_name:
                    if col_name in df.columns:
                        return df[col_name]
                    # Try to find with possible suffix
                    for c in df.columns:
                        if c == col_name:
                            return df[c]

        # Try to match column ignoring table qualifier
        for c in df.columns:
            if c == value_expr:
                return df[c]

        raise ValueError(f"Unknown column: {value_expr}")

    def _resolve_column_for_select(self, expr: str, tables: List[CSVTable], df: DataFrame) -> Tuple[str, str]:
        """Resolve a SELECT expression to a column name. Returns (column_name, output_name)."""
        # Aggregate functions
        if expr.startswith('COUNT('):
            if expr == 'COUNT(*)':
                return ('count_star', 'count')
            col = expr[6:-1]  # Extract column name
            return (f'count_{col}', f'count_{col}')

        if expr.startswith('SUM('):
            col = expr[4:-1]
            return (f'sum_{col}', f'sum_{col}')

        if expr.startswith('MIN('):
            col = expr[4:-1]
            return (f'min_{col}', f'min_{col}')

        if expr.startswith('MAX('):
            col = expr[4:-1]
            return (f'max_{col}', f'max_{col}')

        # Regular column
        if expr in df.columns:
            return (expr, expr)

        # Table-qualified column
        if '.' in expr:
            parts = expr.split('.')
            table_alias = parts[0]
            col_name = '.'.join(parts[1:])

            for table in tables:
                effective_name = table.get_effective_name()
                if table_alias == effective_name or table_alias == table.name:
                    if col_name in df.columns:
                        return (col_name, col_name)
                    # Check if column might have been affected by join
                    for c in df.columns:
                        if c == col_name:
                            return (c, c)

        # Try direct match
        if expr in df.columns:
            return (expr, expr)

        raise ValueError(f"Column not found: {expr}")

    def _apply_select(self, df: DataFrame, select_items: List[Dict], tables: List[CSVTable]) -> DataFrame:
        """Apply SELECT clause."""
        if not select_items or len(select_items) == 0:
            return pd.DataFrame()

        first_item = select_items[0]
        if first_item.get('expr') == '*':
            # SELECT * - return all columns
            return df.copy()

        # Process each select item
        result_data = {}
        output_order = []

        for item in select_items:
            expr = item['expr']
            alias = item['alias']

            # Determine output column name
            output_name = alias if alias else expr

            # Handle aggregate functions
            if expr.startswith('COUNT('):
                if expr == 'COUNT(*)':
                    if output_name in result_data:
                        # Already have this, use different name
                        result_data[output_name] = [len(df)]
                    else:
                        result_data[output_name] = [len(df)]
                else:
                    col = expr[6:-1]
                    if col in df.columns:
                        result_data[output_name] = [df[col].notna().sum()]
                    else:
                        # Try table-qualified
                        found_col = None
                        for c in df.columns:
                            if c == col:
                                found_col = c
                                break
                        if found_col:
                            result_data[output_name] = [df[found_col].notna().sum()]
                        else:
                            result_data[output_name] = [0]
                output_order.append(output_name)

            elif expr.startswith('SUM('):
                col = expr[4:-1]
                if col in df.columns:
                    result_data[output_name] = [df[col].sum()]
                else:
                    # Try table-qualified
                    found_col = None
                    for c in df.columns:
                        if c == col:
                            found_col = c
                            break
                    if found_col:
                        result_data[output_name] = [df[found_col].sum()]
                    else:
                        result_data[output_name] = [0]
                output_order.append(output_name)

            elif expr.startswith('MIN('):
                col = expr[4:-1]
                if col in df.columns:
                    result_data[output_name] = [df[col].min()]
                else:
                    found_col = None
                    for c in df.columns:
                        if c == col:
                            found_col = c
                            break
                    if found_col:
                        result_data[output_name] = [df[found_col].min()]
                    else:
                        result_data[output_name] = [None]
                output_order.append(output_name)

            elif expr.startswith('MAX('):
                col = expr[4:-1]
                if col in df.columns:
                    result_data[output_name] = [df[col].max()]
                else:
                    found_col = None
                    for c in df.columns:
                        if c == col:
                            found_col = c
                            break
                    if found_col:
                        result_data[output_name] = [df[found_col].max()]
                    else:
                        result_data[output_name] = [None]
                output_order.append(output_name)

            else:
                # Regular column
                col_name = expr
                if col_name in df.columns:
                    values = df[col_name].tolist()
                else:
                    # Try table-qualified column
                    if '.' in col_name:
                        parts = col_name.split('.')
                        table_alias = parts[0]
                        col_name = '.'.join(parts[1:])

                        for c in df.columns:
                            if c == col_name:
                                values = df[c].tolist()
                                break
                        else:
                            values = [None] * len(df)
                    else:
                        # Try to find by partial match
                        found_cols = [c for c in df.columns if c == col_name]
                        if found_cols:
                            values = df[found_cols[0]].tolist()
                        else:
                            values = [None] * len(df)

                result_data[output_name] = values
                output_order.append(output_name)

        # Build result dataframe in correct order
        result = pd.DataFrame({k: result_data[k] for k in output_order})
        return result

    def _apply_group_by(self, df: DataFrame, ast: Dict, tables: List[CSVTable]) -> DataFrame:
        """Apply GROUP BY clause with aggregations."""
        group_cols = ast['group_by']
        select_items = ast['select']

        # Identify grouping columns in the dataframe
        actual_group_cols = []
        for col in group_cols:
            # Direct match
            if col in df.columns:
                actual_group_cols.append(col)
            else:
                # Table-qualified match
                if '.' in col:
                    parts = col.split('.')
                    col_name = '.'.join(parts[1:])
                    for c in df.columns:
                        if c == col_name:
                            actual_group_cols.append(c)
                            break
                    else:
                        actual_group_cols.append(col)
                else:
                    actual_group_cols.append(col)

        if not actual_group_cols:
            raise ValueError("No valid GROUP BY columns found in the selected data")

        # Identify aggregation columns
        agg_dict = {}
        select_cols_map = {}  # Maps output name to input column

        for item in select_items:
            expr = item['expr']
            alias = item['alias'] or expr

            if expr.startswith('COUNT('):
                if expr == 'COUNT(*)':
                    agg_dict[alias] = 'count'
                else:
                    col = expr[6:-1]
                    agg_dict[alias] = 'count'
            elif expr.startswith('SUM('):
                agg_dict[alias] = 'sum'
            elif expr.startswith('MIN('):
                agg_dict[alias] = 'min'
            elif expr.startswith('MAX('):
                agg_dict[alias] = 'max'
            elif expr == '*' or expr in actual_group_cols:
                select_cols_map[alias] = expr

        # Perform groupby
        grouped = df.groupby(actual_group_cols, as_index=False)
        result = grouped.agg(agg_dict).reset_index()

        # Rename columns and ensure correct order
        final_cols = []
        for item in select_items:
            expr = item['expr']
            alias = item['alias'] or expr

            if expr.startswith('COUNT('):
                if expr == 'COUNT(*)':
                    col_name = 'count'
                else:
                    col = expr[6:-1]
                    col_name = f'count_{col}'
            elif expr.startswith('SUM('):
                col = expr[4:-1]
                col_name = f'sum_{col}'
            elif expr.startswith('MIN('):
                col = expr[4:-1]
                col_name = f'min_{col}'
            elif expr.startswith('MAX('):
                col = expr[4:-1]
                col_name = f'max_{col}'
            else:
                col_name = expr

            # Find matching column in result
            for res_col in result.columns:
                if res_col == col_name:
                    if res_col != alias:
                        result.rename(columns={res_col: alias}, inplace=True)
                    final_cols.append(alias)
                    break
            else:
                # Not found (might be group by column)
                for res_col in result.columns:
                    if res_col in actual_group_cols and res_col not in final_cols:
                        final_cols.append(res_col)
                        break

        # Reorder columns
        existing_final_cols = [c for c in final_cols if c in result.columns]
        result = result[existing_final_cols]

        return result

    def _apply_having(self, df: DataFrame, having_expr: str, group_by: List[str]) -> DataFrame:
        """Apply HAVING clause filtering."""
        # For now, this is a simplified implementation
        # A full implementation would need to evaluate aggregations
        mask = self._evaluate_boolean_expr(df, having_expr, [])
        return df[mask].reset_index(drop=True)

    def _apply_order_by(self, df: DataFrame, order_by: List[Dict]) -> DataFrame:
        """Apply ORDER BY clause."""
        if not order_by:
            return df

        sort_cols = []
        ascending = []

        for item in order_by:
            expr = item['expr']
            asc = item['asc']

            # Find the column in dataframe
            if expr in df.columns:
                sort_cols.append(expr)
            elif '.' in expr:
                _, col_name = expr.split('.', 1)
                if col_name in df.columns:
                    sort_cols.append(col_name)
                else:
                    # Try to find matching column
                    matching = [c for c in df.columns if c == col_name]
                    if matching:
                        sort_cols.append(matching[0])
            else:
                # Try to find by partial match
                matching = [c for c in df.columns if expr in c or c == expr]
                if matching:
                    sort_cols.append(matching[0])

            ascending.append(asc)

        if sort_cols:
            # Handle NaN values - put them at the end
            return df.sort_values(by=sort_cols, ascending=ascending, na_position='last')

        return df

    def _apply_limit(self, df: DataFrame, limit: int, offset: Optional[int] = None) -> DataFrame:
        """Apply LIMIT and OFFSET clauses."""
        offset_val = offset if offset is not None else 0
        return df.iloc[offset_val:offset_val+limit].reset_index(drop=True)


# =============================================================================
# Output Formatter
# =============================================================================

def format_markdown_table(df: DataFrame) -> str:
    """Format a DataFrame as a Markdown table."""
    if df.empty:
        return ""

    # Handle NULL values
    df_display = df.copy()
    for col in df_display.columns:
        df_display[col] = df_display[col].apply(lambda x: 'NULL' if pd.isna(x) else x)

    # Build the table
    lines = []

    # Header row
    headers = list(df_display.columns)
    lines.append('| ' + ' | '.join(str(h) for h in headers) + ' |')

    # Separator row
    lines.append('|-' + '|-'.join(['---'] * len(headers)) + '-|')

    # Data rows
    for _, row in df_display.iterrows():
        values = [str(row[col]) for col in headers]
        lines.append('| ' + ' | '.join(values) + ' |')

    return '\n'.join(lines)


def save_csv(df: DataFrame, filepath: str):
    """Save DataFrame to CSV file."""
    # Replace NaN with 'NULL' string
    df.to_csv(filepath, index=False, na_rep='NULL')


# =============================================================================
# Main CLI
# =============================================================================

def read_sql_input(sql_arg: str) -> str:
    """Read SQL from either a string or a file."""
    sql_path = Path(sql_arg)
    if sql_path.exists():
        with open(sql_path, 'r') as f:
            return f.read().strip()
    return sql_arg.strip()


def main():
    parser = argparse.ArgumentParser(
        description='Execute SQL queries on CSV files.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python query_files.py --data ./data --sql "SELECT * FROM users"
  python query_files.py --data ./data --sql query.sql --out results.csv
  python query_files.py --data ./data --sql "SELECT country, COUNT(*) FROM users GROUP BY country" --out output.csv
'''
    )
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query string or path to SQL file')
    parser.add_argument('--out', help='Path to output CSV file (optional)')

    args = parser.parse_args()

    try:
        # Read SQL input
        sql = read_sql_input(args.sql)

        if not sql:
            print("Error: No SQL query provided", file=sys.stderr)
            sys.exit(1)

        # Parse SQL
        ast = parse_sql(sql)

        # Load CSV database
        db = CSVDatabase(args.data)

        # Execute query
        executor = QueryExecutor(db)
        result_df = executor.execute(ast)

        # Format and print result
        output = format_markdown_table(result_df)
        print(output)

        # Save to file if specified
        if args.out:
            save_csv(result_df, args.out)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
