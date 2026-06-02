#!/usr/bin/env python3
"""
A CLI to run read-only SQL queries across a folder of CSVs with matching headers.
"""

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd


# ──────────────────────────────────────────────
# Tokenizer
# ──────────────────────────────────────────────

KEYWORDS = {
    'SELECT', 'DISTINCT', 'FROM', 'AS', 'INNER', 'LEFT', 'RIGHT', 'FULL',
    'JOIN', 'ON', 'WHERE', 'AND', 'OR', 'NOT', 'LIKE', 'GROUP', 'BY',
    'HAVING', 'ORDER', 'ASC', 'DESC', 'LIMIT', 'OFFSET',
    'COUNT', 'SUM', 'MIN', 'MAX',
}

TOK_KEYWORD = 'KEYWORD'
TOK_IDENT = 'IDENT'
TOK_NUMBER = 'NUMBER'
TOK_STRING = 'STRING'
TOK_OP = 'OP'
TOK_LPAREN = 'LPAREN'
TOK_RPAREN = 'RPAREN'
TOK_COMMA = 'COMMA'
TOK_DOT = 'DOT'
TOK_STAR = 'STAR'
TOK_EQ = 'EQ'
TOK_NEQ = 'NEQ'
TOK_LT = 'LT'
TOK_LTE = 'LTE'
TOK_GT = 'GT'
TOK_GTE = 'GTE'
TOK_EOF = 'EOF'


class Token:
    __slots__ = ('type', 'value')

    def __init__(self, type: str, value: str):
        self.type = type
        self.value = value

    def __repr__(self):
        return f'Token({self.type}, {self.value!r})'


def tokenize(sql: str) -> List[Token]:
    tokens = []
    i = 0
    n = len(sql)

    while i < n:
        # Skip whitespace
        if sql[i].isspace():
            i += 1
            continue

        # String literal (single-quoted)
        if sql[i] == "'":
            j = i + 1
            while j < n and sql[j] != "'":
                j += 1
            tokens.append(Token(TOK_STRING, sql[i + 1:j]))
            i = j + 1
            continue

        # Double-quoted identifier
        if sql[i] == '"':
            j = i + 1
            while j < n and sql[j] != '"':
                j += 1
            tokens.append(Token(TOK_IDENT, sql[i + 1:j]))
            i = j + 1
            continue

        # Two-character operators
        if i + 1 < n:
            two = sql[i:i + 2]
            if two == '!=':
                tokens.append(Token(TOK_NEQ, '!='))
                i += 2
                continue
            if two == '<=':
                tokens.append(Token(TOK_LTE, '<='))
                i += 2
                continue
            if two == '>=':
                tokens.append(Token(TOK_GTE, '>='))
                i += 2
                continue

        # Single character operators
        c = sql[i]
        if c == '=':
            tokens.append(Token(TOK_EQ, '='))
            i += 1
            continue
        if c == '<':
            tokens.append(Token(TOK_LT, '<'))
            i += 1
            continue
        if c == '>':
            tokens.append(Token(TOK_GT, '>'))
            i += 1
            continue
        if c == '(':
            tokens.append(Token(TOK_LPAREN, '('))
            i += 1
            continue
        if c == ')':
            tokens.append(Token(TOK_RPAREN, ')'))
            i += 1
            continue
        if c == ',':
            tokens.append(Token(TOK_COMMA, ','))
            i += 1
            continue
        if c == '.':
            tokens.append(Token(TOK_DOT, '.'))
            i += 1
            continue
        if c == '*':
            tokens.append(Token(TOK_STAR, '*'))
            i += 1
            continue

        # Number literal
        if c.isdigit() or (c == '-' and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            if c == '-':
                j += 1
            while j < n and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            tokens.append(Token(TOK_NUMBER, sql[i:j]))
            i = j
            continue

        # Identifier or keyword
        if c.isalpha() or c == '_':
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            word = sql[i:j]
            if word.upper() in KEYWORDS:
                tokens.append(Token(TOK_KEYWORD, word.upper()))
            else:
                tokens.append(Token(TOK_IDENT, word))
            i = j
            continue

        raise ValueError(f"Unexpected character: {c!r} at position {i}")

    tokens.append(Token(TOK_EOF, ''))
    return tokens


# ──────────────────────────────────────────────
# Parser  (recursive descent)
# ──────────────────────────────────────────────

class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, type: str, value: str = None) -> Token:
        tok = self.advance()
        if tok.type != type or (value is not None and tok.value != value):
            raise ValueError(f"Expected {type} {value!r}, got {tok}")
        return tok

    def match(self, type: str, value: str = None) -> Optional[Token]:
        tok = self.peek()
        if tok.type == type and (value is None or tok.value == value):
            return self.advance()
        return None

    def is_at(self, type: str, value: str = None) -> bool:
        tok = self.peek()
        return tok.type == type and (value is None or tok.value == value)

    # ── Column reference ──────────────────────

    def parse_column_ref(self) -> str:
        """Parse a column reference: [table.]column"""
        name = self.expect(TOK_IDENT).value
        if self.match(TOK_DOT):
            col = self.expect(TOK_IDENT).value
            return f"{name}.{col}"
        return name

    # ── Aggregate function ────────────────────

    def parse_aggregate(self) -> dict:
        """Parse COUNT(*) / COUNT(col) / SUM(col) / MIN(col) / MAX(col)"""
        func_name = self.expect(TOK_KEYWORD).value  # COUNT/SUM/MIN/MAX
        self.expect(TOK_LPAREN)
        if func_name == 'COUNT' and self.is_at(TOK_STAR):
            self.advance()
            self.expect(TOK_RPAREN)
            return {'type': 'aggregate', 'func': func_name, 'arg': '*'}
        arg = self.parse_column_ref()
        self.expect(TOK_RPAREN)
        return {'type': 'aggregate', 'func': func_name, 'arg': arg}

    # ── Select item ───────────────────────────

    def parse_select_item(self) -> dict:
        """Parse one item in the SELECT list."""
        # Check for aggregate
        if self.is_at(TOK_KEYWORD) and self.peek().value in ('COUNT', 'SUM', 'MIN', 'MAX'):
            agg = self.parse_aggregate()
            alias = self._parse_alias()
            return {**agg, 'alias': alias}

        # Check for *
        if self.is_at(TOK_STAR):
            self.advance()
            return {'type': 'wildcard', 'alias': '*'}

        # Column ref
        col = self.parse_column_ref()
        alias = self._parse_alias()
        return {'type': 'column', 'ref': col, 'alias': alias or col.split('.')[-1]}

    def _parse_alias(self) -> Optional[str]:
        """Parse optional [AS] alias"""
        has_as = self.match(TOK_KEYWORD, 'AS')
        if self.is_at(TOK_IDENT) or self.is_at(TOK_STRING):
            tok = self.advance()
            return tok.value
        if has_as:
            raise ValueError("Expected alias after AS")
        return None

    # ── Table reference ───────────────────────

    def parse_table_ref(self) -> str:
        """Parse table reference (may contain dots for nested dirs)."""
        parts = [self.expect(TOK_IDENT).value]
        while self.match(TOK_DOT):
            parts.append(self.expect(TOK_IDENT).value)
        return '.'.join(parts)

    def parse_table_with_alias(self) -> Tuple[str, Optional[str]]:
        """Parse table_ref [AS alias]"""
        table = self.parse_table_ref()
        alias = self._parse_table_alias()
        return table, alias

    def _parse_table_alias(self) -> Optional[str]:
        has_as = self.match(TOK_KEYWORD, 'AS')
        if self.is_at(TOK_IDENT):
            tok = self.advance()
            return tok.value
        if has_as:
            raise ValueError("Expected alias after AS")
        return None

    # ── Boolean expression ────────────────────

    def parse_boolean_expr(self) -> dict:
        """Parse boolean_expr (OR precedence, AND next, NOT/atom highest)."""
        return self._parse_or()

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while self.match(TOK_KEYWORD, 'OR'):
            right = self._parse_and()
            left = {'type': 'or', 'left': left, 'right': right}
        return left

    def _parse_and(self) -> dict:
        left = self._parse_not()
        while self.match(TOK_KEYWORD, 'AND'):
            right = self._parse_not()
            left = {'type': 'and', 'left': left, 'right': right}
        return left

    def _parse_not(self) -> dict:
        if self.match(TOK_KEYWORD, 'NOT'):
            operand = self._parse_not()
            return {'type': 'not', 'operand': operand}
        return self._parse_atom()

    def _parse_atom(self) -> dict:
        if self.match(TOK_LPAREN):
            expr = self.parse_boolean_expr()
            self.expect(TOK_RPAREN)
            return expr

        # Could be a comparison or aggregate comparison
        left = self._parse_value()
        op = self._parse_comparison_op()
        if op is None:
            # Might be LIKE
            if self.match(TOK_KEYWORD, 'LIKE'):
                pattern = self.expect(TOK_STRING).value
                return {'type': 'like', 'column': left, 'pattern': pattern}
            return left
        right = self._parse_value()
        return {'type': 'comparison', 'op': op, 'left': left, 'right': right}

    def _parse_value(self) -> dict:
        """Parse a value: column_ref, number, string, or aggregate."""
        if self.is_at(TOK_KEYWORD) and self.peek().value in ('COUNT', 'SUM', 'MIN', 'MAX'):
            return self.parse_aggregate()
        if self.is_at(TOK_IDENT):
            return {'type': 'column', 'ref': self.parse_column_ref()}
        if self.is_at(TOK_NUMBER):
            return {'type': 'number', 'value': self.advance().value}
        if self.is_at(TOK_STRING):
            return {'type': 'string', 'value': self.advance().value}
        raise ValueError(f"Expected value, got {self.peek()}")

    def _parse_comparison_op(self) -> Optional[str]:
        tok = self.peek()
        if tok.type in (TOK_EQ, TOK_NEQ, TOK_LT, TOK_LTE, TOK_GT, TOK_GTE):
            self.advance()
            return tok.value
        return None

    # ── Full SELECT ───────────────────────────

    def parse_select(self) -> dict:
        self.expect(TOK_KEYWORD, 'SELECT')

        distinct = bool(self.match(TOK_KEYWORD, 'DISTINCT'))

        # Select list
        select_items = [self.parse_select_item()]
        while self.match(TOK_COMMA):
            select_items.append(self.parse_select_item())

        # FROM
        self.expect(TOK_KEYWORD, 'FROM')
        main_table, main_alias = self.parse_table_with_alias()

        # JOINs
        joins = []
        while True:
            join_type = None
            if self.match(TOK_KEYWORD, 'INNER'):
                join_type = 'INNER'
                self.expect(TOK_KEYWORD, 'JOIN')
            elif self.match(TOK_KEYWORD, 'LEFT'):
                join_type = 'LEFT'
                self.match(TOK_KEYWORD, 'JOIN')  # optional JOIN after LEFT
            elif self.match(TOK_KEYWORD, 'RIGHT'):
                join_type = 'RIGHT'
                self.match(TOK_KEYWORD, 'JOIN')
            elif self.match(TOK_KEYWORD, 'FULL'):
                join_type = 'FULL'
                self.match(TOK_KEYWORD, 'JOIN')
            elif self.is_at(TOK_KEYWORD, 'JOIN'):
                join_type = 'INNER'
                self.advance()
            else:
                break

            right_table, right_alias = self.parse_table_with_alias()
            self.expect(TOK_KEYWORD, 'ON')
            left_col = self.parse_column_ref()
            self.expect(TOK_EQ)
            right_col = self.parse_column_ref()
            joins.append({
                'type': join_type,
                'table': right_table,
                'alias': right_alias,
                'left_col': left_col,
                'right_col': right_col,
            })

        # WHERE
        where = None
        if self.match(TOK_KEYWORD, 'WHERE'):
            where = self.parse_boolean_expr()

        # GROUP BY
        group_by = None
        if self.match(TOK_KEYWORD, 'GROUP'):
            self.expect(TOK_KEYWORD, 'BY')
            group_by = [self.parse_column_ref()]
            while self.match(TOK_COMMA):
                group_by.append(self.parse_column_ref())

        # HAVING
        having = None
        if self.match(TOK_KEYWORD, 'HAVING'):
            having = self.parse_boolean_expr()

        # ORDER BY
        order_by = None
        if self.match(TOK_KEYWORD, 'ORDER'):
            self.expect(TOK_KEYWORD, 'BY')
            order_by = []
            col = self.parse_column_ref()
            direction = 'ASC'
            if self.match(TOK_KEYWORD, 'ASC'):
                direction = 'ASC'
            elif self.match(TOK_KEYWORD, 'DESC'):
                direction = 'DESC'
            order_by.append((col, direction))
            while self.match(TOK_COMMA):
                col = self.parse_column_ref()
                direction = 'ASC'
                if self.match(TOK_KEYWORD, 'ASC'):
                    direction = 'ASC'
                elif self.match(TOK_KEYWORD, 'DESC'):
                    direction = 'DESC'
                order_by.append((col, direction))

        # LIMIT
        limit = None
        if self.match(TOK_KEYWORD, 'LIMIT'):
            limit = int(self.expect(TOK_NUMBER).value)

        # OFFSET
        offset = None
        if self.match(TOK_KEYWORD, 'OFFSET'):
            offset = int(self.expect(TOK_NUMBER).value)

        return {
            'distinct': distinct,
            'select': select_items,
            'from': {'table': main_table, 'alias': main_alias},
            'joins': joins,
            'where': where,
            'group_by': group_by,
            'having': having,
            'order_by': order_by,
            'limit': limit,
            'offset': offset,
        }


# ──────────────────────────────────────────────
# CSV loader
# ──────────────────────────────────────────────

def load_csv_files(data_dir: str) -> Dict[str, pd.DataFrame]:
    """Load all CSV files from directory into {table_name: DataFrame}."""
    tables = {}
    for root, _, files in os.walk(data_dir):
        for fname in files:
            if not fname.endswith('.csv'):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, data_dir)
            # Build table name: dir separator → '.', dots in filename → '_'
            base = rel[:-4]  # strip .csv
            parts = base.replace(os.sep, '/').split('/')
            # Replace dots in the filename part only
            parts[-1] = parts[-1].replace('.', '_')
            table_name = '.'.join(parts)

            try:
                df = pd.read_csv(fpath, dtype=str, keep_default_na=True,
                                 na_values=[''], skipinitialspace=True)
                if len(df.columns) == 0:
                    continue
                tables[table_name] = df
            except pd.errors.EmptyDataError:
                continue
    return tables


# ──────────────────────────────────────────────
# Executor
# ──────────────────────────────────────────────

class Executor:
    def __init__(self, tables: Dict[str, pd.DataFrame]):
        self.tables = tables
        self.alias_map: Dict[str, str] = {}   # alias_or_name → table_name

    def execute(self, query: dict) -> pd.DataFrame:
        # Build alias map
        self.alias_map = {}
        from_table = query['from']['table']
        from_alias = query['from']['alias'] or from_table
        self.alias_map[from_alias] = from_table
        for j in query['joins']:
            jt = j['table']
            ja = j['alias'] or jt
            self.alias_map[ja] = jt

        # 1. Load main table
        df = self.tables[from_table].copy()
        df = self._prefix(df, from_alias)

        # 2. Joins
        for j in query['joins']:
            df = self._do_join(df, j)

        # 3. WHERE
        if query['where']:
            mask = self._eval_bool(df, query['where'])
            df = df[mask].reset_index(drop=True)

        # 4. GROUP BY / aggregation
        select_items = query['select']
        has_agg = any(si['type'] == 'aggregate' for si in select_items)

        if query['group_by'] is not None or has_agg:
            df = self._do_group_by(df, query)
            # After GROUP BY, projection is already done
        else:
            # For non-grouped queries, apply ORDER BY before projection
            # so we can sort by columns not in the SELECT list
            if query['order_by']:
                df = self._do_order_by(df, query['order_by'])
            df = self._project(df, select_items)

        # 5. DISTINCT
        if query['distinct']:
            df = df.drop_duplicates()

        # 6. ORDER BY (only for grouped queries, since non-grouped already sorted)
        if query['order_by'] and (query['group_by'] is not None or has_agg):
            df = self._do_order_by(df, query['order_by'])

        # 7. OFFSET / LIMIT
        if query['offset']:
            df = df.iloc[query['offset']:]
        if query['limit'] is not None:
            df = df.head(query['limit'])

        return df.reset_index(drop=True)

    # ── helpers ───────────────────────────────

    def _prefix(self, df: pd.DataFrame, alias: str) -> pd.DataFrame:
        """Prefix all columns with alias."""
        rename = {}
        for c in df.columns:
            if '.' not in c:
                rename[c] = f"{alias}.{c}"
        return df.rename(columns=rename)

    def _resolve_col(self, ref: str) -> str:
        """Resolve a column reference like 'u.id' → 'users.id'."""
        if '.' in ref:
            parts = ref.split('.', 1)
            prefix = parts[0]
            if prefix in self.alias_map:
                return f"{self.alias_map[prefix]}.{parts[1]}"
        return ref

    def _find_col(self, df: pd.DataFrame, ref: str) -> str:
        """Find actual column in df matching ref (with alias resolution)."""
        resolved = self._resolve_col(ref)
        if resolved in df.columns:
            return resolved
        # Try suffix match
        for c in df.columns:
            if c.endswith(f'.{ref}') or c == ref:
                return c
        # Last resort: bare column name
        bare = ref.split('.')[-1] if '.' in ref else ref
        for c in df.columns:
            if c.split('.')[-1] == bare:
                return c
        raise ValueError(f"Column {ref!r} not found. Available: {list(df.columns)}")

    # ── joins ─────────────────────────────────

    def _do_join(self, left: pd.DataFrame, join_info: dict) -> pd.DataFrame:
        jt = join_info['type']
        right_table = join_info['table']
        right_alias = join_info['alias'] or right_table
        left_col_raw = join_info['left_col']
        right_col_raw = join_info['right_col']

        # Determine left column name in the already-prefixed left DataFrame
        # left is already prefixed with its alias
        if '.' in left_col_raw:
            left_alias_part, left_col_part = left_col_raw.split('.', 1)
            # Map alias to actual table, then use the alias we're using
            if left_alias_part in self.alias_map:
                # Find what alias this table is using in the current context
                # by checking the columns in left df
                for c in left.columns:
                    if c.endswith(f'.{left_col_part}'):
                        left_col = c
                        break
            else:
                left_col = left_col_raw
        else:
            # Find column in left df that ends with this column name
            left_col = None
            for c in left.columns:
                if c.endswith(f'.{left_col_raw}') or c == left_col_raw:
                    left_col = c
                    break
            if left_col is None:
                raise ValueError(f"Column {left_col_raw} not found in left table")

        right = self.tables[right_table].copy()
        right = self._prefix(right, right_alias)

        # Resolve the right column to the prefixed name in the right DataFrame
        if '.' in right_col_raw:
            right_col_part = right_col_raw.split('.', 1)[1]
        else:
            right_col_part = right_col_raw
        right_col = f"{right_alias}.{right_col_part}"

        how = {'INNER': 'inner', 'LEFT': 'left', 'RIGHT': 'right', 'FULL': 'outer'}[jt]
        result = pd.merge(left, right, left_on=left_col, right_on=right_col,
                          how=how, suffixes=('', '_dup'))
        # Drop duplicate columns from right (they have _dup suffix)
        dup_cols = [c for c in result.columns if c.endswith('_dup')]
        result = result.drop(columns=dup_cols)
        return result

    # ── boolean eval ──────────────────────────

    def _eval_bool(self, df: pd.DataFrame, expr: dict) -> pd.Series:
        t = expr['type']
        if t == 'and':
            return self._eval_bool(df, expr['left']) & self._eval_bool(df, expr['right'])
        if t == 'or':
            return self._eval_bool(df, expr['left']) | self._eval_bool(df, expr['right'])
        if t == 'not':
            return ~self._eval_bool(df, expr['operand'])
        if t == 'comparison':
            left = self._eval_value(df, expr['left'])
            right = self._eval_value(df, expr['right'])
            op = expr['op']
            return self._compare(left, right, op)
        if t == 'like':
            col = self._eval_value(df, expr['column'])
            pattern = expr['pattern']
            # Convert SQL LIKE pattern to regex: first escape regex specials,
            # then replace SQL wildcards
            regex = '^'
            for ch in pattern:
                if ch == '%':
                    regex += '.*'
                elif ch == '_':
                    regex += '.'
                else:
                    regex += re.escape(ch)
            regex += '$'
            return col.astype(str).str.match(regex, na=False)
        raise ValueError(f"Unknown bool expr type: {t}")

    def _eval_value(self, df: pd.DataFrame, val: dict):
        t = val['type']
        if t == 'column':
            col_name = self._find_col(df, val['ref'])
            return df[col_name]
        if t == 'number':
            return val['value']
        if t == 'string':
            return val['value']
        raise ValueError(f"Cannot eval value type {t} in WHERE")

    def _compare(self, left, right, op: str) -> pd.Series:
        if isinstance(left, pd.Series):
            try:
                left_n = pd.to_numeric(left, errors='coerce')
                right_n = float(right) if not isinstance(right, pd.Series) else pd.to_numeric(right, errors='coerce')
                if op == '=':  return left_n == right_n
                if op == '!=': return left_n != right_n
                if op == '<':  return left_n < right_n
                if op == '<=': return left_n <= right_n
                if op == '>':  return left_n > right_n
                if op == '>=': return left_n >= right_n
            except (ValueError, TypeError):
                pass
            # String comparison
            if op == '=':  return left.astype(str) == str(right)
            if op == '!=': return left.astype(str) != str(right)
            if op == '<':  return left.astype(str) < str(right)
            if op == '<=': return left.astype(str) <= str(right)
            if op == '>':  return left.astype(str) > str(right)
            if op == '>=': return left.astype(str) >= str(right)
        # scalar comparison
        if op == '=':  return left == right
        if op == '!=': return left != right
        raise ValueError(f"Unsupported comparison: {op}")

    # ── projection ────────────────────────────

    def _project(self, df: pd.DataFrame, select_items: List[dict]) -> pd.DataFrame:
        result = {}
        order = []
        for si in select_items:
            if si['type'] == 'wildcard':
                for c in df.columns:
                    bare = c.split('.')[-1]
                    if bare not in result:
                        result[bare] = df[c]
                        order.append(bare)
            elif si['type'] == 'column':
                alias = si['alias']
                col = self._find_col(df, si['ref'])
                result[alias] = df[col]
                order.append(alias)
        return pd.DataFrame(result, columns=order)

    # ── group by / aggregation ────────────────

    def _do_group_by(self, df: pd.DataFrame, query: dict) -> pd.DataFrame:
        select_items = query['select']
        group_cols_raw = query['group_by'] or []
        having_expr = query['having']

        # Resolve group columns to actual df columns
        group_cols = [self._find_col(df, c) for c in group_cols_raw]

        rows = []

        if group_cols:
            grouped = df.groupby(group_cols, dropna=False)
            for key, group_df in grouped:
                row = self._compute_row(group_df, select_items, group_cols)
                rows.append(row)
        else:
            # Aggregate entire table
            row = self._compute_row(df, select_items, [])
            rows.append(row)

        # Build output DataFrame
        out = pd.DataFrame(rows)
        col_order = []
        for si in select_items:
            if si['type'] == 'wildcard':
                col_order.extend(c for c in out.columns if c not in col_order)
            else:
                alias = si.get('alias', si.get('ref', '*'))
                if alias not in col_order:
                    col_order.append(alias)
        out = out[[c for c in col_order if c in out.columns]]

        # HAVING
        if having_expr:
            mask = self._eval_having(out, having_expr, select_items)
            out = out[mask].reset_index(drop=True)

        return out

    def _compute_row(self, group_df: pd.DataFrame, select_items: List[dict],
                     group_cols: List[str]) -> dict:
        row = {}
        for si in select_items:
            if si['type'] == 'wildcard':
                for c in group_df.columns:
                    bare = c.split('.')[-1]
                    if bare not in row:
                        row[bare] = group_df[c].iloc[0]
            elif si['type'] == 'column':
                alias = si['alias']
                col = self._find_col(group_df, si['ref'])
                row[alias] = group_df[col].iloc[0]
            elif si['type'] == 'aggregate':
                alias = si['alias']
                val = self._compute_agg(group_df, si['func'], si['arg'])
                row[alias] = val
        return row

    def _compute_agg(self, df: pd.DataFrame, func: str, arg: str):
        if func == 'COUNT':
            if arg == '*':
                return len(df)
            col = self._find_col(df, arg)
            return int(df[col].notna().sum())
        col = self._find_col(df, arg)
        vals = pd.to_numeric(df[col], errors='coerce')
        if func == 'SUM':
            result = vals.sum()
            # Round to avoid floating point artifacts
            if pd.notna(result):
                result = round(result, 2) if result != int(result) else int(result)
            return result
        if func == 'MIN':
            if vals.notna().any():
                result = vals.min()
                return round(result, 2) if result != int(result) else int(result)
            return None
        if func == 'MAX':
            if vals.notna().any():
                result = vals.max()
                return round(result, 2) if result != int(result) else int(result)
            return None
        raise ValueError(f"Unknown aggregate: {func}")

    # ── HAVING eval ───────────────────────────

    def _eval_having(self, df: pd.DataFrame, expr: dict, select_items: List[dict] = None) -> pd.Series:
        t = expr['type']
        if t == 'and':
            return self._eval_having(df, expr['left'], select_items) & self._eval_having(df, expr['right'], select_items)
        if t == 'or':
            return self._eval_having(df, expr['left'], select_items) | self._eval_having(df, expr['right'], select_items)
        if t == 'not':
            return ~self._eval_having(df, expr['operand'], select_items)
        if t == 'comparison':
            left = self._having_val(df, expr['left'], select_items)
            right = self._having_val(df, expr['right'], select_items)
            return self._compare(left, right, expr['op'])
        raise ValueError(f"Unsupported HAVING expr: {t}")

    def _having_val(self, df: pd.DataFrame, val: dict, select_items: List[dict] = None):
        t = val['type']
        if t == 'column':
            col = self._find_col(df, val['ref'])
            return df[col]
        if t == 'number':
            return float(val['value'])
        if t == 'string':
            return val['value']
        if t == 'aggregate':
            # First check if there's an alias from parsing
            alias = val.get('alias')
            if alias and alias in df.columns:
                return df[alias]

            # Look for a matching aggregate in select_items
            if select_items:
                for si in select_items:
                    if si.get('type') == 'aggregate':
                        if si['func'] == val['func'] and si['arg'] == val['arg']:
                            # Found matching aggregate, use its alias
                            si_alias = si.get('alias')
                            if si_alias and si_alias in df.columns:
                                return df[si_alias]

            # Try the generated name
            key = f"{val['func']}({val['arg']})"
            if key in df.columns:
                return df[key]
            raise ValueError(f"Aggregate {key} not found in result for HAVING")
        raise ValueError(f"Unsupported HAVING value type: {t}")

    # ── ORDER BY ──────────────────────────────

    def _do_order_by(self, df: pd.DataFrame, order_by: List[Tuple[str, str]]) -> pd.DataFrame:
        for col_ref, direction in reversed(order_by):
            col = self._find_col(df, col_ref)
            try:
                vals = pd.to_numeric(df[col], errors='coerce')
                if vals.notna().all():
                    df = df.sort_values(by=col, ascending=(direction == 'ASC'), kind='mergesort')
                    continue
            except Exception:
                pass
            df = df.sort_values(by=col, ascending=(direction == 'ASC'), kind='mergesort')
        return df.reset_index(drop=True)


# ──────────────────────────────────────────────
# Output formatting
# ──────────────────────────────────────────────

def format_markdown(df: pd.DataFrame) -> str:
    """Format DataFrame as markdown table."""
    cols = list(df.columns)
    header = '| ' + ' | '.join(cols) + ' |'
    sep = '| ' + ' | '.join('---' for _ in cols) + ' |'

    lines = [header, sep]
    for _, row in df.iterrows():
        vals = []
        for c in cols:
            v = row[c]
            if pd.isna(v):
                vals.append('NULL')
            else:
                s = str(v)
                # Trim trailing .0 for integer-like numbers
                vals.append(s)
        lines.append('| ' + ' | '.join(vals) + ' |')

    return '\n'.join(lines)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Run SQL queries on CSV files')
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query string or path to .sql file')
    parser.add_argument('--out', help='Output CSV path (optional)')
    args = parser.parse_args()

    # Load tables
    tables = load_csv_files(args.data)
    if not tables:
        print("No valid CSV files found.", file=sys.stderr)
        sys.exit(1)

    # Read SQL
    if os.path.isfile(args.sql):
        with open(args.sql, 'r') as f:
            sql = f.read()
    else:
        sql = args.sql

    sql = sql.strip().rstrip(';').strip()

    # Parse & execute
    try:
        tokens = tokenize(sql)
        p = Parser(tokens)
        query = p.parse_select()
        executor = Executor(tables)
        result = executor.execute(query)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    # Output
    print(format_markdown(result))

    if args.out:
        # For CSV output, use the column names from the result
        result.to_csv(args.out, index=False)


if __name__ == '__main__':
    main()
