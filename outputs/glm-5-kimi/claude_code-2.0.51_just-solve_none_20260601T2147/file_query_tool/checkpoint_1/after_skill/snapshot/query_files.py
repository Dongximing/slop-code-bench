#!/usr/bin/env python3
"""CLI to run read-only SQL queries across a folder of CSVs with matching headers."""
import argparse
import os
import re
import sys
import traceback
from typing import Dict, List, Optional, Tuple

import pandas as pd


KEYWORDS = {
    'SELECT', 'DISTINCT', 'FROM', 'AS', 'INNER', 'LEFT', 'RIGHT', 'FULL',
    'JOIN', 'ON', 'WHERE', 'AND', 'OR', 'NOT', 'LIKE', 'GROUP', 'BY',
    'HAVING', 'ORDER', 'ASC', 'DESC', 'LIMIT', 'OFFSET',
    'COUNT', 'SUM', 'MIN', 'MAX',
}

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
        if sql[i].isspace():
            i += 1
            continue

        if sql[i] == "'":
            j = i + 1
            while j < n and sql[j] != "'":
                j += 1
            tokens.append(Token('STRING', sql[i + 1:j]))
            i = j + 1
            continue

        if sql[i] == '"':
            j = i + 1
            while j < n and sql[j] != '"':
                j += 1
            tokens.append(Token('IDENT', sql[i + 1:j]))
            i = j + 1
            continue

        if i + 1 < n and sql[i:i + 2] in ('!=', '<=', '>='):
            two = sql[i:i + 2]
            tokens.append(Token({'!=': 'NEQ', '<=': 'LTE', '>=': 'GTE'}[two], two))
            i += 2
            continue

        c = sql[i]
        if c in '=(),.*':
            tokens.append(Token({'=': 'EQ', '(': 'LPAREN', ')': 'RPAREN', ',': 'COMMA', '.': 'DOT', '*': 'STAR'}[c], c))
            i += 1
            continue
        if c == '<':
            tokens.append(Token('LT', '<'))
            i += 1
            continue
        if c == '>':
            tokens.append(Token('GT', '>'))
            i += 1
            continue

        if c.isdigit() or (c == '-' and i + 1 < n and sql[i + 1].isdigit()):
            j = i
            if c == '-':
                j += 1
            while j < n and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            tokens.append(Token('NUMBER', sql[i:j]))
            i = j
            continue

        if c.isalpha() or c == '_':
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == '_'):
                j += 1
            word = sql[i:j]
            tok_type = 'KEYWORD' if word.upper() in KEYWORDS else 'IDENT'
            tokens.append(Token(tok_type, word.upper() if tok_type == 'KEYWORD' else word))
            i = j
            continue

        raise ValueError(f"Unexpected character: {c!r} at position {i}")

    tokens.append(Token(TOK_EOF, ''))
    return tokens


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

    def parse_column_ref(self) -> str:
        name = self.expect('IDENT').value
        if self.match('DOT'):
            col = self.expect('IDENT').value
            return f"{name}.{col}"
        return name

    def parse_aggregate(self) -> dict:
        func_name = self.expect('KEYWORD').value
        self.expect('LPAREN')
        if func_name == 'COUNT' and self.is_at('STAR'):
            self.advance()
            self.expect('RPAREN')
            return {'type': 'aggregate', 'func': func_name, 'arg': '*'}
        arg = self.parse_column_ref()
        self.expect('RPAREN')
        return {'type': 'aggregate', 'func': func_name, 'arg': arg}

    def parse_select_item(self) -> dict:
        if self.is_at('KEYWORD') and self.peek().value in ('COUNT', 'SUM', 'MIN', 'MAX'):
            agg = self.parse_aggregate()
            alias = self._parse_alias()
            return {**agg, 'alias': alias}

        if self.is_at('STAR'):
            self.advance()
            return {'type': 'wildcard', 'alias': '*'}

        col = self.parse_column_ref()
        alias = self._parse_alias()
        return {'type': 'column', 'ref': col, 'alias': alias or col.split('.')[-1]}

    def _parse_alias(self) -> Optional[str]:
        has_as = self.match('KEYWORD', 'AS')
        if self.is_at('IDENT') or self.is_at('STRING'):
            tok = self.advance()
            return tok.value
        if has_as:
            raise ValueError("Expected alias after AS")
        return None

    def parse_table_ref(self) -> str:
        parts = [self.expect('IDENT').value]
        while self.match('DOT'):
            parts.append(self.expect('IDENT').value)
        return '.'.join(parts)

    def parse_table_with_alias(self) -> Tuple[str, Optional[str]]:
        table = self.parse_table_ref()
        alias = self._parse_table_alias()
        return table, alias

    def _parse_table_alias(self) -> Optional[str]:
        has_as = self.match('KEYWORD', 'AS')
        if self.is_at('IDENT'):
            tok = self.advance()
            return tok.value
        if has_as:
            raise ValueError("Expected alias after AS")
        return None

    def parse_boolean_expr(self) -> dict:
        return self._parse_or()

    def _parse_or(self) -> dict:
        left = self._parse_and()
        while self.match('KEYWORD', 'OR'):
            right = self._parse_and()
            left = {'type': 'or', 'left': left, 'right': right}
        return left

    def _parse_and(self) -> dict:
        left = self._parse_not()
        while self.match('KEYWORD', 'AND'):
            right = self._parse_not()
            left = {'type': 'and', 'left': left, 'right': right}
        return left

    def _parse_not(self) -> dict:
        if self.match('KEYWORD', 'NOT'):
            operand = self._parse_not()
            return {'type': 'not', 'operand': operand}
        return self._parse_atom()

    def _parse_atom(self) -> dict:
        if self.match('LPAREN'):
            expr = self.parse_boolean_expr()
            self.expect('RPAREN')
            return expr

        left = self._parse_value()
        op = self._parse_comparison_op()
        if op is None:
            if self.match('KEYWORD', 'LIKE'):
                pattern = self.expect('STRING').value
                return {'type': 'like', 'column': left, 'pattern': pattern}
            return left
        right = self._parse_value()
        return {'type': 'comparison', 'op': op, 'left': left, 'right': right}

    def _parse_value(self) -> dict:
        if self.is_at('KEYWORD') and self.peek().value in ('COUNT', 'SUM', 'MIN', 'MAX'):
            return self.parse_aggregate()
        if self.is_at('IDENT'):
            return {'type': 'column', 'ref': self.parse_column_ref()}
        if self.is_at('NUMBER'):
            return {'type': 'number', 'value': self.advance().value}
        if self.is_at('STRING'):
            return {'type': 'string', 'value': self.advance().value}
        raise ValueError(f"Expected value, got {self.peek()}")

    def _parse_comparison_op(self) -> Optional[str]:
        tok = self.peek()
        if tok.type in ('EQ', 'NEQ', 'LT', 'LTE', 'GT', 'GTE'):
            self.advance()
            return tok.value
        return None

    def parse_select(self) -> dict:
        self.expect('KEYWORD', 'SELECT')

        distinct = bool(self.match('KEYWORD', 'DISTINCT'))

        select_items = [self.parse_select_item()]
        while self.match('COMMA'):
            select_items.append(self.parse_select_item())

        self.expect('KEYWORD', 'FROM')
        main_table, main_alias = self.parse_table_with_alias()

        joins = []
        while True:
            join_type = None
            if self.match('KEYWORD', 'INNER'):
                join_type = 'INNER'
                self.expect('KEYWORD', 'JOIN')
            elif self.match('KEYWORD', 'LEFT'):
                join_type = 'LEFT'
                self.match('KEYWORD', 'JOIN')
            elif self.match('KEYWORD', 'RIGHT'):
                join_type = 'RIGHT'
                self.match('KEYWORD', 'JOIN')
            elif self.match('KEYWORD', 'FULL'):
                join_type = 'FULL'
                self.match('KEYWORD', 'JOIN')
            elif self.is_at('KEYWORD', 'JOIN'):
                join_type = 'INNER'
                self.advance()
            else:
                break

            right_table, right_alias = self.parse_table_with_alias()
            self.expect('KEYWORD', 'ON')
            left_col = self.parse_column_ref()
            self.expect('EQ')
            right_col = self.parse_column_ref()
            joins.append({
                'type': join_type,
                'table': right_table,
                'alias': right_alias,
                'left_col': left_col,
                'right_col': right_col,
            })

        where = None
        if self.match('KEYWORD', 'WHERE'):
            where = self.parse_boolean_expr()

        group_by = None
        if self.match('KEYWORD', 'GROUP'):
            self.expect('KEYWORD', 'BY')
            group_by = [self.parse_column_ref()]
            while self.match('COMMA'):
                group_by.append(self.parse_column_ref())

        having = None
        if self.match('KEYWORD', 'HAVING'):
            having = self.parse_boolean_expr()

        order_by = None
        if self.match('KEYWORD', 'ORDER'):
            self.expect('KEYWORD', 'BY')
            order_by = []
            while True:
                col = self.parse_column_ref()
                direction = 'DESC' if self.match('KEYWORD', 'DESC') else 'ASC'
                self.match('KEYWORD', 'ASC')  # consume if present
                order_by.append((col, direction))
                if not self.match('COMMA'):
                    break

        limit = None
        if self.match('KEYWORD', 'LIMIT'):
            limit = int(self.expect('NUMBER').value)

        offset = None
        if self.match('KEYWORD', 'OFFSET'):
            offset = int(self.expect('NUMBER').value)

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


def load_csv_files(data_dir: str) -> Dict[str, pd.DataFrame]:
    tables = {}
    for root, _, files in os.walk(data_dir):
        for fname in files:
            if not fname.endswith('.csv'):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, data_dir)
            base = rel[:-4]
            parts = base.replace(os.sep, '/').split('/')
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


_CMP_OPS = {
    '=':  lambda a, b: a == b,
    '!=': lambda a, b: a != b,
    '<':  lambda a, b: a < b,
    '<=': lambda a, b: a <= b,
    '>':  lambda a, b: a > b,
    '>=': lambda a, b: a >= b,
}


def _clean_numeric(result):
    if pd.notna(result) and result == int(result):
        return int(result)
    return round(result, 2) if pd.notna(result) else result


class Executor:
    def __init__(self, tables: Dict[str, pd.DataFrame]):
        self.tables = tables
        self.alias_map: Dict[str, str] = {}

    def execute(self, query: dict) -> pd.DataFrame:
        self.alias_map = {}
        from_table = query['from']['table']
        from_alias = query['from']['alias'] or from_table
        self.alias_map[from_alias] = from_table
        for j in query['joins']:
            jt = j['table']
            ja = j['alias'] or jt
            self.alias_map[ja] = jt

        df = self._prefix(self.tables[from_table].copy(), from_alias)

        for j in query['joins']:
            df = self._do_join(df, j)

        if query['where']:
            mask = self._eval_expr(df, query['where'])
            df = df[mask].reset_index(drop=True)

        select_items = query['select']
        has_agg = any(si['type'] == 'aggregate' for si in select_items)
        is_grouped = query['group_by'] is not None or has_agg

        if is_grouped:
            df = self._do_group_by(df, query)
        else:
            if query['order_by']:
                df = self._do_order_by(df, query['order_by'])
            df = self._project(df, select_items)

        if query['distinct']:
            df = df.drop_duplicates()

        if query['order_by'] and is_grouped:
            df = self._do_order_by(df, query['order_by'])

        if query['offset']:
            df = df.iloc[query['offset']:]
        if query['limit'] is not None:
            df = df.head(query['limit'])

        return df.reset_index(drop=True)

    def _prefix(self, df: pd.DataFrame, alias: str) -> pd.DataFrame:
        rename = {c: f"{alias}.{c}" for c in df.columns if '.' not in c}
        return df.rename(columns=rename)

    def _resolve_col(self, ref: str) -> str:
        if '.' in ref:
            prefix, col = ref.split('.', 1)
            if prefix in self.alias_map:
                return f"{self.alias_map[prefix]}.{col}"
        return ref

    def _find_col(self, df: pd.DataFrame, ref: str) -> str:
        resolved = self._resolve_col(ref)
        if resolved in df.columns:
            return resolved
        for c in df.columns:
            if c.endswith(f'.{ref}') or c == ref:
                return c
        bare = ref.split('.')[-1] if '.' in ref else ref
        for c in df.columns:
            if c.split('.')[-1] == bare:
                return c
        raise ValueError(f"Column {ref!r} not found. Available: {list(df.columns)}")

    def _do_join(self, left: pd.DataFrame, join_info: dict) -> pd.DataFrame:
        jt = join_info['type']
        right_table = join_info['table']
        right_alias = join_info['alias'] or right_table

        left_col = self._find_col(left, join_info['left_col'])

        right = self._prefix(self.tables[right_table].copy(), right_alias)
        right_col_raw = join_info['right_col']
        right_col_part = right_col_raw.split('.', 1)[1] if '.' in right_col_raw else right_col_raw
        right_col = f"{right_alias}.{right_col_part}"

        how = {'INNER': 'inner', 'LEFT': 'left', 'RIGHT': 'right', 'FULL': 'outer'}[jt]
        result = pd.merge(left, right, left_on=left_col, right_on=right_col,
                          how=how, suffixes=('', '_dup'))
        result = result.drop(columns=[c for c in result.columns if c.endswith('_dup')])
        return result

    def _eval_expr(self, df: pd.DataFrame, expr: dict) -> pd.Series:
        t = expr['type']
        if t == 'and':
            return self._eval_expr(df, expr['left']) & self._eval_expr(df, expr['right'])
        if t == 'or':
            return self._eval_expr(df, expr['left']) | self._eval_expr(df, expr['right'])
        if t == 'not':
            return ~self._eval_expr(df, expr['operand'])
        if t == 'comparison':
            return self._compare(
                self._eval_value(df, expr['left']),
                self._eval_value(df, expr['right']),
                expr['op'],
            )
        if t == 'like':
            col = self._eval_value(df, expr['column'])
            regex = '^' + ''.join(
                '.*' if ch == '%' else '.' if ch == '_' else re.escape(ch)
                for ch in expr['pattern']
            ) + '$'
            return col.astype(str).str.match(regex, na=False)
        raise ValueError(f"Unknown bool expr type: {t}")

    def _eval_value(self, df: pd.DataFrame, val: dict):
        t = val['type']
        if t == 'column':
            return df[self._find_col(df, val['ref'])]
        if t == 'number':
            return val['value']
        if t == 'string':
            return val['value']
        raise ValueError(f"Cannot eval value type {t} in WHERE")

    def _compare(self, left, right, op: str) -> pd.Series:
        cmp = _CMP_OPS[op]
        if isinstance(left, pd.Series):
            try:
                left_n = pd.to_numeric(left, errors='coerce')
                right_n = float(right) if not isinstance(right, pd.Series) else pd.to_numeric(right, errors='coerce')
                return cmp(left_n, right_n)
            except (ValueError, TypeError):
                pass
            return cmp(left.astype(str), str(right))
        return cmp(left, right)

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
                result[alias] = df[self._find_col(df, si['ref'])]
                order.append(alias)
        return pd.DataFrame(result, columns=order)

    def _do_group_by(self, df: pd.DataFrame, query: dict) -> pd.DataFrame:
        select_items = query['select']
        group_cols = [self._find_col(df, c) for c in (query['group_by'] or [])]

        if group_cols:
            rows = [self._compute_row(g, select_items) for _, g in df.groupby(group_cols, dropna=False)]
        else:
            rows = [self._compute_row(df, select_items)]

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

        if query['having']:
            mask = self._eval_having(out, query['having'], select_items)
            out = out[mask].reset_index(drop=True)

        return out

    def _compute_row(self, group_df: pd.DataFrame, select_items: List[dict]) -> dict:
        row = {}
        for si in select_items:
            if si['type'] == 'wildcard':
                for c in group_df.columns:
                    bare = c.split('.')[-1]
                    if bare not in row:
                        row[bare] = group_df[c].iloc[0]
            elif si['type'] == 'column':
                row[si['alias']] = group_df[self._find_col(group_df, si['ref'])].iloc[0]
            elif si['type'] == 'aggregate':
                row[si['alias']] = self._compute_agg(group_df, si['func'], si['arg'])
        return row

    def _compute_agg(self, df: pd.DataFrame, func: str, arg: str):
        if func == 'COUNT':
            if arg == '*':
                return len(df)
            return int(df[self._find_col(df, arg)].notna().sum())
        vals = pd.to_numeric(df[self._find_col(df, arg)], errors='coerce')
        if func == 'SUM':
            return _clean_numeric(vals.sum())
        if vals.notna().any():
            return _clean_numeric(vals.min() if func == 'MIN' else vals.max())
        return None

    def _eval_having(self, df: pd.DataFrame, expr: dict, select_items: List[dict] = None) -> pd.Series:
        t = expr['type']
        if t == 'and':
            return self._eval_having(df, expr['left'], select_items) & self._eval_having(df, expr['right'], select_items)
        if t == 'or':
            return self._eval_having(df, expr['left'], select_items) | self._eval_having(df, expr['right'], select_items)
        if t == 'not':
            return ~self._eval_having(df, expr['operand'], select_items)
        if t == 'comparison':
            return self._compare(
                self._having_val(df, expr['left'], select_items),
                self._having_val(df, expr['right'], select_items),
                expr['op'],
            )
        raise ValueError(f"Unsupported HAVING expr: {t}")

    def _having_val(self, df: pd.DataFrame, val: dict, select_items: List[dict] = None):
        t = val['type']
        if t == 'column':
            return df[self._find_col(df, val['ref'])]
        if t == 'number':
            return float(val['value'])
        if t == 'string':
            return val['value']
        if t == 'aggregate':
            alias = val.get('alias')
            if alias and alias in df.columns:
                return df[alias]
            if select_items:
                for si in select_items:
                    if si.get('type') == 'aggregate' and si['func'] == val['func'] and si['arg'] == val['arg']:
                        si_alias = si.get('alias')
                        if si_alias and si_alias in df.columns:
                            return df[si_alias]
            key = f"{val['func']}({val['arg']})"
            if key in df.columns:
                return df[key]
            raise ValueError(f"Aggregate {key} not found in result for HAVING")
        raise ValueError(f"Unsupported HAVING value type: {t}")

    def _do_order_by(self, df: pd.DataFrame, order_by: List[Tuple[str, str]]) -> pd.DataFrame:
        for col_ref, direction in reversed(order_by):
            col = self._find_col(df, col_ref)
            asc = direction == 'ASC'
            df = df.sort_values(by=col, ascending=asc, kind='mergesort')
        return df.reset_index(drop=True)


def format_markdown(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    header = '| ' + ' | '.join(cols) + ' |'
    sep = '| ' + ' | '.join('---' for _ in cols) + ' |'
    lines = [header, sep]
    for _, row in df.iterrows():
        vals = ['NULL' if pd.isna(row[c]) else str(row[c]) for c in cols]
        lines.append('| ' + ' | '.join(vals) + ' |')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='Run SQL queries on CSV files')
    parser.add_argument('--data', required=True, help='Directory containing CSV files')
    parser.add_argument('--sql', required=True, help='SQL query string or path to .sql file')
    parser.add_argument('--out', help='Output CSV path (optional)')
    args = parser.parse_args()

    tables = load_csv_files(args.data)
    if not tables:
        print("No valid CSV files found.", file=sys.stderr)
        sys.exit(1)

    if os.path.isfile(args.sql):
        with open(args.sql, 'r') as f:
            sql = f.read()
    else:
        sql = args.sql

    sql = sql.strip().rstrip(';').strip()

    try:
        tokens = tokenize(sql)
        p = Parser(tokens)
        query = p.parse_select()
        executor = Executor(tables)
        result = executor.execute(query)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    print(format_markdown(result))

    if args.out:
        result.to_csv(args.out, index=False)


if __name__ == '__main__':
    main()
