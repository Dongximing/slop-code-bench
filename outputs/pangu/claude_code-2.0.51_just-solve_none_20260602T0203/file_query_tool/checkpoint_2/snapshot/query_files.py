#!/usr/bin/env python3
"""
SQL Query Engine for Multiple File Formats

A lightweight CLI to run read-only SQL queries across a folder of data files
including CSV, TSV, Parquet, JSON, and JSONL with optional compression.
"""

import argparse
import gzip
import bz2
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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
        'JOIN', 'ON', 'AND', 'OR', 'NOT', 'AS', 'ASC', 'DESC',
        'COUNT', 'SUM', 'MIN', 'MAX'
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
                self.pos += 1
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

        self.tokens.append(('EOF', ''))

    def peek(self, expected: str = None, token_type: str = None) -> Optional[str]:
        """Look at the next token without consuming it."""
        if len(self.tokens) == 0:
            return None
        if token_type:
            if self.tokens[0][0] == token_type:
                return self.tokens[0][1]
            return None
        if expected:
            if self.tokens[0][1] == expected:
                return self.tokens[0][1]
            if self.tokens[0][0] == expected:
                return self.tokens[0][1]
            return None
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

        if self.lexer.peek() != 'EOF':
            raise ValueError(f"Unexpected token at end: {self.lexer.peek()}")

    def parse_select(self):
        """Parse SELECT clause."""
        self.lexer.consume('KEYWORD', 'SELECT')

        distinct_all = False
        if self.lexer.peek(expected='DISTINCT'):
            self.lexer.consume('KEYWORD', 'DISTINCT')
            distinct_all = True

        self.select_items = []

        while True:
            expr = self.parse_expression()

            alias = None
            if self.lexer.peek(expected='AS'):
                self.lexer.consume('KEYWORD', 'AS')
                # Aliases can be identifiers or string literals
                if self.lexer.peek(token_type='IDENTIFIER'):
                    alias = self.lexer.consume('IDENTIFIER')[1]
                elif self.lexer.peek(token_type='STRING'):
                    alias = self.lexer.consume('STRING')[1]
                else:
                    raise ValueError("Expected identifier or string literal after AS")
            elif self.lexer.peek(token_type='IDENTIFIER'):
                alias_candidate = self.lexer.tokens[0][1]
                if alias_candidate.upper() not in ['WHERE', 'GROUP', 'BY', 'HAVING', 'ORDER', 'LIMIT', 'OFFSET']:
                    alias = alias_candidate
                    self.lexer.consume('IDENTIFIER')

            self.select_items.append({
                'expr': expr,
                'alias': alias,
                'distinct': distinct_all
            })

            if self.lexer.peek(expected=','):
                self.lexer.consume('COMMA')
            elif self.lexer.peek(expected='FROM'):
                break
            else:
                raise ValueError(f"Expected ',' or 'FROM' after SELECT item")

    def parse_expression(self) -> str:
        """Parse a SELECT expression."""
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

        if self.lexer.peek(expected='*'):
            self.lexer.consume('STAR')
            return '*'

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

        table_info = self.parse_table_ref()
        self.from_tables.append(table_info)

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

            if self.lexer.peek(expected='JOIN'):
                self.lexer.consume('KEYWORD', 'JOIN')
            else:
                if join_type:
                    raise ValueError("JOIN keyword expected after JOIN type")
                break

            table_info = self.parse_table_ref()
            table_info['join_type'] = join_type or 'INNER'
            self.from_tables.append(table_info)

    def parse_table_ref(self) -> Dict:
        """Parse a table reference."""
        name_parts = []
        name_parts.append(self.parse_identifier())

        while self.lexer.peek(expected='.'):
            self.lexer.consume('DOT')
            name_parts.append(self.parse_identifier())

        table_name = '.'.join(name_parts)

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
        """Parse primary expressions."""
        if self.lexer.peek(expected='('):
            self.lexer.consume('LPAREN')
            expr = self.parse_boolean_expr()
            self.lexer.consume('RPAREN')
            return f"({expr})"

        if self.lexer.peek(token_type='STRING'):
            value = self.lexer.consume('STRING')[1]
            return f"'{value}'"

        if self.lexer.peek(token_type='NUMBER'):
            return self.lexer.consume('NUMBER')[1]
        if self.lexer.peek(token_type='FLOAT'):
            return self.lexer.consume('FLOAT')[1]

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
# Multiple Format Table Loader
# =============================================================================

class DataFileLoader:
    """Handles loading of various file formats with optional compression."""

    SUPPORTED_FORMATS = {
        '.csv': 'csv',
        '.tsv': 'tsv',
        '.parquet': 'parquet',
        '.json': 'json',
        '.jsonl': 'jsonl'
    }

    COMPRESSION_EXTENSIONS = {
        '.gz': 'gzip',
        '.bz2': 'bz2'
    }

    @classmethod
    def detect_format(cls, filepath: str) -> str:
        """Detect the file format based on extension."""
        path = Path(filepath)
        ext = path.suffix.lower()

        # Check for compression first
        for comp_ext in cls.COMPRESSION_EXTENSIONS:
            if str(path).endswith(comp_ext):
                # Get the actual file extension before compression
                actual_path = str(path)[:-len(comp_ext)]
                actual_ext = Path(actual_path).suffix.lower()
                if actual_ext in cls.SUPPORTED_FORMATS:
                    return cls.SUPPORTED_FORMATS[actual_ext]

        if ext in cls.SUPPORTED_FORMATS:
            return cls.SUPPORTED_FORMATS[ext]

        raise ValueError(f"Unsupported file format: {filepath}")

    @classmethod
    def is_compressed(cls, filepath: str) -> Tuple[bool, Optional[str]]:
        """Check if file is compressed and return compression type."""
        path = Path(filepath)
        ext = path.suffix.lower()

        for comp_ext, comp_type in cls.COMPRESSION_EXTENSIONS.items():
            if str(path).endswith(comp_ext):
                return True, comp_type

        return False, None

    @classmethod
    def load_dataframe(cls, filepath: str) -> DataFrame:
        """Load a file into a DataFrame based on its format."""
        file_format = cls.detect_format(filepath)
        is_compressed, comp_type = cls.is_compressed(filepath)

        if file_format == 'csv':
            return cls._load_csv(filepath, is_compressed, comp_type)
        elif file_format == 'tsv':
            return cls._load_tsv(filepath, is_compressed, comp_type)
        elif file_format == 'parquet':
            return cls._load_parquet(filepath)
        elif file_format == 'json':
            return cls._load_json(filepath)
        elif file_format == 'jsonl':
            return cls._load_jsonl(filepath)
        else:
            raise ValueError(f"Unknown format: {file_format}")

    @classmethod
    def _load_csv(cls, filepath: str, is_compressed: bool, comp_type: Optional[str]) -> DataFrame:
        """Load CSV file."""
        if is_compressed:
            if comp_type == 'gzip':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_csv(f)
            elif comp_type == 'bz2':
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_csv(f)
        return pd.read_csv(filepath)

    @classmethod
    def _load_tsv(cls, filepath: str, is_compressed: bool, comp_type: Optional[str]) -> DataFrame:
        """Load TSV file."""
        if is_compressed:
            if comp_type == 'gzip':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_csv(f, sep='\t')
            elif comp_type == 'bz2':
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_csv(f, sep='\t')
        return pd.read_csv(filepath, sep='\t')

    @classmethod
    def _load_parquet(cls, filepath: str) -> DataFrame:
        """Load Parquet file."""
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise ImportError("pyarrow is required to read Parquet files. Install with: pip install pyarrow")

        # Parquet files are not typically compressed with .gz or .bz2,
        # but we can still handle it if needed
        is_compressed, comp_type = cls.is_compressed(filepath)
        if is_compressed:
            import pyarrow as pa
            if comp_type == 'gzip':
                with gzip.open(filepath, 'rb') as f:
                    return pd.read_parquet(f)
            elif comp_type == 'bz2':
                with bz2.open(filepath, 'rb') as f:
                    return pd.read_parquet(f)
        return pd.read_parquet(filepath)

    @classmethod
    def _load_json(cls, filepath: str) -> DataFrame:
        """Load JSON file."""
        is_compressed, comp_type = cls.is_compressed(filepath)
        if is_compressed:
            if comp_type == 'gzip':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_json(f)
            elif comp_type == 'bz2':
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_json(f)
        return pd.read_json(filepath)

    @classmethod
    def _load_jsonl(cls, filepath: str) -> DataFrame:
        """Load JSONL file."""
        is_compressed, comp_type = cls.is_compressed(filepath)
        if is_compressed:
            if comp_type == 'gzip':
                with gzip.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_json(f, lines=True)
            elif comp_type == 'bz2':
                with bz2.open(filepath, 'rt', encoding='utf-8') as f:
                    return pd.read_json(f, lines=True)
        return pd.read_json(filepath, lines=True)


class DataFileTable:
    """Represents a data file table with data and metadata."""

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
        return self._alias or self.name

    @classmethod
    def load(cls, name: str, filepath: str) -> 'DataFileTable':
        """Load a data file into a table."""
        df = DataFileLoader.load_dataframe(filepath)
        if not df.empty:
            if any(not col or col.strip() == '' for col in df.columns):
                raise ValueError(f"File {filepath} has invalid headers (empty column names)")
        return cls(name, filepath, df)


class DataFileDatabase:
    """Manages loading and querying data files from a directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.tables: Dict[str, DataFileTable] = {}
        self._table_groups: Dict[str, List[str]] = {}  # base_name -> list of table names
        self._load_tables()

    def _load_tables(self):
        """Load all supported data files from the data directory."""
        # Find all files with supported extensions
        all_files = []
        for ext in DataFileLoader.SUPPORTED_FORMATS.keys():
            all_files.extend(self.data_dir.rglob(f"*{ext}"))
            # Also check for compressed versions
            for comp_ext in [".gz", ".bz2"]:
                all_files.extend(self.data_dir.rglob(f"*{ext}{comp_ext}"))

        for filepath in all_files:
            try:
                rel_path = filepath.relative_to(self.data_dir)
                parts = str(rel_path).split('/')
                filename_part = parts[-1]

                # Get base name without any extension (including compression)
                base_name = self._get_base_name(filename_part)

                # For nested directories, replace path separators with dots
                dir_parts = parts[:-1]
                if dir_parts:
                    base_name = '.'.join(dir_parts) + '.' + base_name

                table_name = base_name

                table = DataFileTable.load(table_name, str(filepath))

                # Check for columns collision
                if table_name in self.tables:
                    existing_cols = set(self.tables[table_name].columns)
                    new_cols = set(table.columns)
                    if existing_cols != new_cols:
                        raise ValueError(
                            f"Files '{table_name}' have different columns. "
                            f"Existing: {sorted(existing_cols)}, New: {sorted(new_cols)}"
                        )

                self.tables[table_name] = table

                # Track file groups for column validation across formats
                base_key = base_name
                if base_key not in self._table_groups:
                    self._table_groups[base_key] = []
                self._table_groups[base_key].append(table_name)

            except Exception as e:
                print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)

    def _get_base_name(self, filename: str) -> str:
        """Get base name without any extensions."""
        # Try removing compression extension first
        for comp_ext in [".gz", ".bz2"]:
            if filename.endswith(comp_ext):
                filename = filename[:-len(comp_ext)]
                break

        # Then remove format extension
        for fmt_ext in DataFileLoader.SUPPORTED_FORMATS.keys():
            if filename.endswith(fmt_ext):
                filename = filename[:-len(fmt_ext)]
                break

        return filename

    def get_table(self, name: str) -> DataFileTable:
        """Get a table by name."""
        if name in self.tables:
            return self.tables[name]
        raise ValueError(f"Table '{name}' not found. Available tables: {list(self.tables.keys())}")


# =============================================================================
# Query Executor (same logic, different table class)
# =============================================================================

class QueryExecutor:
    """Executes parsed SQL queries against data tables."""

    def __init__(self, db: DataFileDatabase):
        self.db = db

    def execute(self, ast: Dict) -> DataFrame:
        """Execute a parsed SQL query."""
        tables = []
        for table_ref in ast['tables']:
            table = self.db.get_table(table_ref['name'])
            if table_ref.get('alias'):
                table.alias = table_ref['alias']
            tables.append(table)

        result_df = self._apply_joins(tables, ast['tables'])

        if ast.get('where'):
            result_df = self._apply_where(result_df, ast['where'], ast['tables'])

        if ast.get('group_by'):
            result_df = self._apply_group_by(result_df, ast, tables)
        else:
            result_df = self._apply_select(result_df, ast['select'], tables)

        if ast.get('having'):
            result_df = self._apply_having(result_df, ast['having'], ast.get('group_by', []))

        if ast.get('order_by'):
            result_df = self._apply_order_by(result_df, ast['order_by'])

        if ast.get('limit') is not None:
            result_df = self._apply_limit(result_df, ast['limit'], ast.get('offset', 0))

        return result_df

    def _apply_joins(self, tables: List[DataFileTable], table_refs: List[Dict]) -> DataFrame:
        """Apply JOIN operations between tables."""
        if len(tables) == 1:
            return tables[0].df.copy()

        result = tables[0].df.copy()

        for i, (table, table_ref) in enumerate(zip(tables[1:], table_refs[1:])):
            on_clause = table_ref.get('on_clause')
            join_type = table_ref.get('join_type', 'INNER')

            if on_clause:
                left_col, right_col = on_clause
                left_col_name = self._resolve_column_name(left_col, tables[:i+1], result.columns.tolist())
                right_col_name = self._resolve_column_name(right_col, [table], table.df.columns.tolist())

                if join_type == 'INNER':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name, right_on=right_col_name,
                                      how='inner', suffixes=('', '_dup'))
                elif join_type == 'LEFT':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name, right_on=right_col_name,
                                      how='left', suffixes=('', '_dup'))
                elif join_type == 'RIGHT':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name, right_on=right_col_name,
                                      how='right', suffixes=('', '_dup'))
                elif join_type == 'FULL':
                    result = pd.merge(result, table.df,
                                      left_on=left_col_name, right_on=right_col_name,
                                      how='outer', suffixes=('', '_dup'))
            else:
                result = pd.merge(result, table.df, how=join_type.lower(), suffixes=('', '_dup'))

        return result

    def _resolve_column_name(self, col_expr: str, tables: List[DataFileTable], available_columns: List[str]) -> str:
        """Resolve a column expression to a column name in the dataframe."""
        if col_expr in available_columns:
            return col_expr

        if '.' in col_expr:
            parts = col_expr.split('.')
            table_alias_or_name = parts[0]
            col_name = '.'.join(parts[1:])

            for table in tables:
                effective_name = table.get_effective_name()
                if table_alias_or_name == effective_name or table_alias_or_name == table.name:
                    if col_name in table.columns:
                        if col_name in available_columns:
                            return col_name
                        for c in available_columns:
                            if c == col_name:
                                return c

        for c in available_columns:
            if c == col_expr:
                return c

        raise ValueError(f"Column '{col_expr}' not found in {available_columns}")

    def _apply_where(self, df: DataFrame, where_expr: str, table_refs: List[Dict]) -> DataFrame:
        """Apply WHERE clause filtering."""
        mask = self._evaluate_boolean_expr(df, where_expr, table_refs)
        return df[mask].reset_index(drop=True)

    def _evaluate_boolean_expr(self, df: DataFrame, expr: str, table_refs: List[Dict]) -> pd.Series:
        """Evaluate a boolean expression against a dataframe."""
        expr = expr.strip()

        if expr.startswith('(') and expr.endswith(')'):
            inner = expr[1:-1].strip()
            return self._evaluate_boolean_expr(df, inner, table_refs)

        if expr.upper().startswith('NOT '):
            inner = expr[4:].strip()
            return ~self._evaluate_boolean_expr(df, inner, table_refs)

        if ' AND ' in expr:
            parts = self._split_by_operator(expr, 'AND')
            if len(parts) == 2:
                left_mask = self._evaluate_boolean_expr(df, parts[0], table_refs)
                right_mask = self._evaluate_boolean_expr(df, parts[1], table_refs)
                return left_mask & right_mask

        if ' OR ' in expr:
            parts = self._split_by_operator(expr, 'OR')
            if len(parts) == 2:
                left_mask = self._evaluate_boolean_expr(df, parts[0], table_refs)
                right_mask = self._evaluate_boolean_expr(df, parts[1], table_refs)
                return left_mask | right_mask

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
                if op.upper() in ['AND', 'OR']:
                    parts.append(current.strip())
                    current = ""
                    i += len(op) - 1
            else:
                current += c

            i += 1

        if current:
            parts.append(current.strip())

        return parts

    def _evaluate_comparison(self, df: DataFrame, expr: str, table_refs: List[Dict]) -> pd.Series:
        """Evaluate a comparison expression."""
        if ' LIKE ' in expr:
            parts = expr.split(' LIKE ', 1)
            left = parts[0].strip()
            right = parts[1].strip()

            left_val = self._evaluate_value(df, left, table_refs)
            pattern = right.strip("'")

            import re
            pattern = pattern.replace('%', '.*').replace('_', '.')

            if isinstance(left_val, pd.Series):
                return left_val.apply(lambda x: bool(re.match(pattern, str(x))) if pd.notna(x) else False)
            return False

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

                if isinstance(left_val, pd.Series):
                    if isinstance(right_val, pd.Series):
                        op_method = getattr(left_val, op_name)
                        return op_method(right_val)
                    else:
                        op_method = getattr(left_val, op_name)
                        return op_method(right_val)
                else:
                    if isinstance(right_val, pd.Series):
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

        if value_expr.startswith("'") and value_expr.endswith("'"):
            return value_expr[1:-1]

        try:
            if '.' in value_expr:
                return float(value_expr)
            return int(value_expr)
        except ValueError:
            pass

        if value_expr in df.columns:
            return df[value_expr]

        if '.' in value_expr:
            parts = value_expr.split('.')
            table_alias = parts[0]
            col_name = '.'.join(parts[1:])

            for table_ref in table_refs:
                effective_name = table_ref.get('alias') or table_ref['name']
                if table_alias == effective_name:
                    if col_name in df.columns:
                        return df[col_name]
                    for c in df.columns:
                        if c == col_name:
                            return df[c]

        for c in df.columns:
            if c == value_expr:
                return df[c]

        raise ValueError(f"Unknown column: {value_expr}")

    def _apply_select(self, df: DataFrame, select_items: List[Dict], tables: List[DataFileTable]) -> DataFrame:
        """Apply SELECT clause."""
        if not select_items or len(select_items) == 0:
            return pd.DataFrame()

        first_item = select_items[0]
        if first_item.get('expr') == '*':
            return df.copy()

        result_data = {}
        output_order = []

        for item in select_items:
            expr = item['expr']
            alias = item['alias']
            output_name = alias if alias else expr

            if expr.startswith('COUNT('):
                if expr == 'COUNT(*)':
                    result_data[output_name] = [len(df)]
                else:
                    col = expr[6:-1]
                    if col in df.columns:
                        result_data[output_name] = [df[col].notna().sum()]
                    else:
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
                col_name = expr
                if col_name in df.columns:
                    values = df[col_name].tolist()
                else:
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
                        found_cols = [c for c in df.columns if c == col_name]
                        if found_cols:
                            values = df[found_cols[0]].tolist()
                        else:
                            values = [None] * len(df)

                result_data[output_name] = values
                output_order.append(output_name)

        result = pd.DataFrame({k: result_data[k] for k in output_order})
        return result

    def _apply_group_by(self, df: DataFrame, ast: Dict, tables: List[DataFileTable]) -> DataFrame:
        """Apply GROUP BY clause with aggregations."""
        group_cols = ast['group_by']
        select_items = ast['select']

        actual_group_cols = []
        for col in group_cols:
            if col in df.columns:
                actual_group_cols.append(col)
            else:
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

        # Build list of aggregation operations
        agg_ops = []

        for item in select_items:
            expr = item['expr']
            alias = item['alias']

            if expr.startswith('COUNT('):
                # For COUNT(*), use any non-null column for counting
                if expr == 'COUNT(*)':
                    # Pick the first non-groupby column to count
                    count_col = None
                    for col in df.columns:
                        if col not in actual_group_cols:
                            count_col = col
                            break
                    if count_col:
                        agg_ops.append((count_col, 'count', alias))
                    else:
                        # All columns are groupby columns
                        agg_ops.append((actual_group_cols[0], 'count', alias))
                else:
                    # COUNT(column)
                    col = expr[6:-1]  # Extract column name
                    agg_ops.append((col, 'count', alias))
            elif expr.startswith('SUM('):
                col = expr[4:-1]  # Extract column name
                agg_ops.append((col, 'sum', alias))
            elif expr.startswith('MIN('):
                col = expr[4:-1]  # Extract column name
                agg_ops.append((col, 'min', alias))
            elif expr.startswith('MAX('):
                col = expr[4:-1]  # Extract column name
                agg_ops.append((col, 'max', alias))
            elif expr in actual_group_cols:
                # Column to group by - just include it (only once)
                if not any(op[2] == expr for op in agg_ops):
                    agg_ops.append((expr, 'first', alias))

        # Build aggregation dict
        agg_dict = {}
        for col, func, alias in agg_ops:
            if func == 'first':
                agg_dict[alias if alias else col] = col
            else:
                agg_dict[alias if alias else col] = (col, func)

        grouped = df.groupby(actual_group_cols, as_index=False)
        result = grouped.agg(agg_dict).reset_index()

        # Ensure correct column order
        final_cols = []
        for item in select_items:
            expr = item['expr']
            alias = item['alias']
            output_name = alias if alias else expr

            if expr.startswith('COUNT(') or expr.startswith('SUM(') or expr.startswith('MIN(') or expr.startswith('MAX('):
                if output_name in result.columns:
                    final_cols.append(output_name)
            elif expr in actual_group_cols:
                if output_name in result.columns:
                    final_cols.append(output_name)

        # Add group by columns that might not be in select
        for col in actual_group_cols:
            if col not in final_cols:
                final_cols.append(col)

        existing_final_cols = [c for c in final_cols if c in result.columns]
        result = result[existing_final_cols]

        return result

    def _apply_having(self, df: DataFrame, having_expr: str, group_by: List[str]) -> DataFrame:
        """Apply HAVING clause filtering."""
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

            if expr in df.columns:
                sort_cols.append(expr)
            elif '.' in expr:
                _, col_name = expr.split('.', 1)
                if col_name in df.columns:
                    sort_cols.append(col_name)
                else:
                    matching = [c for c in df.columns if c == col_name]
                    if matching:
                        sort_cols.append(matching[0])
            else:
                matching = [c for c in df.columns if expr in c or c == expr]
                if matching:
                    sort_cols.append(matching[0])

            ascending.append(asc)

        if sort_cols:
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

    df_display = df.copy()
    for col in df_display.columns:
        df_display[col] = df_display[col].apply(lambda x: 'NULL' if pd.isna(x) else x)

    lines = []
    headers = list(df_display.columns)
    lines.append('| ' + ' | '.join(str(h) for h in headers) + ' |')
    lines.append('|-' + '|-'.join(['---'] * len(headers)) + '-|')

    for _, row in df_display.iterrows():
        values = [str(row[col]) for col in headers]
        lines.append('| ' + ' | '.join(values) + ' |')

    return '\n'.join(lines)


def save_csv(df: DataFrame, filepath: str):
    """Save DataFrame to CSV file."""
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
    import sys

    parser = argparse.ArgumentParser(
        description='Execute SQL queries on data files (CSV, TSV, Parquet, JSON, JSONL).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Supported formats:
  - CSV (.csv)
  - TSV (.tsv)
  - Parquet (.parquet)
  - JSON (.json)
  - JSONL (.jsonl)

Compressed files (.gz, .bz2) are also supported for all formats.

Examples:
  python query_files.py --data ./data --sql "SELECT * FROM users"
  python query_files.py --data ./data --sql query.sql --out results.csv
  python query_files.py --data ./data --sql "SELECT country, COUNT(*) FROM users GROUP BY country"
'''
    )
    parser.add_argument('--data', required=True, help='Directory containing data files')
    parser.add_argument('--sql', required=True, help='SQL query string or path to SQL file')
    parser.add_argument('--out', help='Path to output CSV file (optional)')

    args = parser.parse_args()

    try:
        sql = read_sql_input(args.sql)

        if not sql:
            print("Error: No SQL query provided", file=sys.stderr)
            sys.exit(1)

        ast = parse_sql(sql)

        db = DataFileDatabase(args.data)

        executor = QueryExecutor(db)
        result_df = executor.execute(ast)

        output = format_markdown_table(result_df)
        print(output)

        if args.out:
            save_csv(result_df, args.out)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
