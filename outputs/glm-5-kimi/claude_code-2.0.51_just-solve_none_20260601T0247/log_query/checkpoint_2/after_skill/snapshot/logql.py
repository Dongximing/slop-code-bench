#!/usr/bin/env python3
"""
LogQL Part 2 - A command-line program that reads NDJSON logs,
parses a SQL-like query with GROUP BY and aggregations,
filters rows with boolean logic, and returns results as JSON.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, List, Optional, Union


# Token Types

class TokenType:
    # Keywords
    SELECT = 'SELECT'
    FROM = 'FROM'
    WHERE = 'WHERE'
    AND = 'AND'
    OR = 'OR'
    TRUE = 'TRUE'
    FALSE = 'FALSE'
    NULL = 'NULL'
    AS = 'AS'
    GROUP = 'GROUP'
    BY = 'BY'
    COUNT = 'COUNT'
    SUM = 'SUM'
    AVG = 'AVG'
    AVERAGE = 'AVERAGE'
    MIN = 'MIN'
    MAX = 'MAX'
    UNIQUE = 'UNIQUE'

    # Literals and identifiers
    NUMBER = 'NUMBER'
    STRING = 'STRING'
    IDENT = 'IDENT'

    # Operators
    EQ = '='
    NE = '!='
    LT = '<'
    LE = '<='
    GT = '>'
    GE = '>='

    # Punctuation
    COMMA = ','
    DOT = '.'
    STAR = '*'
    LPAREN = '('
    RPAREN = ')'

    EOF = 'EOF'


@dataclass
class Token:
    type: str
    value: Any
    pos: int  # Position in the query string


# Lexer

class LexerError(Exception):
    pass


class Lexer:
    KEYWORDS = {
        'select': TokenType.SELECT,
        'from': TokenType.FROM,
        'where': TokenType.WHERE,
        'and': TokenType.AND,
        'or': TokenType.OR,
        'true': TokenType.TRUE,
        'false': TokenType.FALSE,
        'null': TokenType.NULL,
        'as': TokenType.AS,
        'group': TokenType.GROUP,
        'by': TokenType.BY,
        'count': TokenType.COUNT,
        'sum': TokenType.SUM,
        'avg': TokenType.AVG,
        'average': TokenType.AVERAGE,
        'min': TokenType.MIN,
        'max': TokenType.MAX,
        'unique': TokenType.UNIQUE,
    }

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.length = len(query)

    def peek(self, offset: int = 0) -> Optional[str]:
        pos = self.pos + offset
        if pos < self.length:
            return self.query[pos]
        return None

    def advance(self) -> Optional[str]:
        if self.pos < self.length:
            ch = self.query[self.pos]
            self.pos += 1
            return ch
        return None

    def skip_whitespace(self):
        while self.pos < self.length and self.query[self.pos].isspace():
            self.pos += 1

    def read_number(self) -> Token:
        start = self.pos
        has_dot = False

        # Optional negative sign
        if self.peek() == '-':
            self.advance()

        # Integer part
        if self.peek() == '0':
            self.advance()
        elif self.peek() and self.peek().isdigit():
            while self.peek() and self.peek().isdigit():
                self.advance()
        else:
            raise LexerError(f"Invalid number at position {start}")

        # Decimal part
        if self.peek() == '.':
            has_dot = True
            self.advance()  # consume '.'
            if not (self.peek() and self.peek().isdigit()):
                raise LexerError(f"Invalid number at position {start}")
            while self.peek() and self.peek().isdigit():
                self.advance()

        value_str = self.query[start:self.pos]
        if has_dot:
            value = float(value_str)
        else:
            value = int(value_str)

        return Token(TokenType.NUMBER, value, start)

    def read_string(self) -> Token:
        start = self.pos
        self.advance()  # consume opening quote

        result = []
        while True:
            ch = self.peek()
            if ch is None:
                raise LexerError(f"Unterminated string starting at position {start}")
            if ch == '"':
                self.advance()  # consume closing quote
                break
            if ch == '\\':
                self.advance()
                next_ch = self.peek()
                if next_ch == '"':
                    result.append('"')
                    self.advance()
                elif next_ch == '\\':
                    result.append('\\')
                    self.advance()
                else:
                    raise LexerError(f"Invalid escape sequence at position {self.pos}")
            else:
                result.append(ch)
                self.advance()

        return Token(TokenType.STRING, ''.join(result), start)

    def read_identifier(self) -> Token:
        start = self.pos

        # First character must be letter or underscore
        ch = self.peek()
        if not (ch and (ch.isalpha() or ch == '_')):
            raise LexerError(f"Invalid identifier at position {start}")

        while True:
            ch = self.peek()
            if ch and (ch.isalnum() or ch == '_'):
                self.advance()
            else:
                break

        value = self.query[start:self.pos]

        # Check if it's a keyword (case-insensitive)
        lower_value = value.lower()
        if lower_value in self.KEYWORDS:
            return Token(self.KEYWORDS[lower_value], value, start)

        return Token(TokenType.IDENT, value, start)

    def next_token(self) -> Token:
        self.skip_whitespace()

        if self.pos >= self.length:
            return Token(TokenType.EOF, None, self.pos)

        start = self.pos
        ch = self.peek()

        # Number (including negative)
        if ch.isdigit() or (ch == '-' and self.peek(1) and self.peek(1).isdigit()):
            return self.read_number()

        # String
        if ch == '"':
            return self.read_string()

        # Identifier or keyword
        if ch.isalpha() or ch == '_':
            return self.read_identifier()

        # Operators and punctuation
        if ch == '=':
            self.advance()
            return Token(TokenType.EQ, '=', start)

        if ch == '!' and self.peek(1) == '=':
            self.advance()
            self.advance()
            return Token(TokenType.NE, '!=', start)

        if ch == '<':
            self.advance()
            if self.peek() == '=':
                self.advance()
                return Token(TokenType.LE, '<=', start)
            return Token(TokenType.LT, '<', start)

        if ch == '>':
            self.advance()
            if self.peek() == '=':
                self.advance()
                return Token(TokenType.GE, '>=', start)
            return Token(TokenType.GT, '>', start)

        if ch == ',':
            self.advance()
            return Token(TokenType.COMMA, ',', start)

        if ch == '.':
            self.advance()
            return Token(TokenType.DOT, '.', start)

        if ch == '*':
            self.advance()
            return Token(TokenType.STAR, '*', start)

        if ch == '(':
            self.advance()
            return Token(TokenType.LPAREN, '(', start)

        if ch == ')':
            self.advance()
            return Token(TokenType.RPAREN, ')', start)

        raise LexerError(f"Unexpected character '{ch}' at position {self.pos}")

    def tokenize(self) -> List[Token]:
        tokens = []
        while True:
            token = self.next_token()
            tokens.append(token)
            if token.type == TokenType.EOF:
                break
        return tokens


# AST Nodes

@dataclass
class FieldRef:
    parts: List[str]

    def to_string(self) -> str:
        return '.'.join(self.parts)


@dataclass
class Literal:
    value: Any


@dataclass
class Predicate:
    field: FieldRef
    op: str
    literal: Literal


@dataclass
class BinaryExpr:
    left: Union['BooleanExpr', Predicate]
    op: str  # 'AND' or 'OR'
    right: Union['BooleanExpr', Predicate]


BooleanExpr = Union[Predicate, BinaryExpr]


@dataclass
class AggCall:
    func: str  # 'COUNT', 'SUM', 'AVG', 'MIN', 'MAX', 'UNIQUE'
    field: Optional[FieldRef]  # None for COUNT(*)
    is_star: bool = False  # True for COUNT(*)

    def canonical_string(self) -> str:
        if self.is_star:
            return f"{self.func}(*)"
        return f"{self.func}({self.field.to_string()})"


@dataclass
class SelectItem:
    field: Optional[FieldRef]  # None if this is an aggregate
    agg: Optional[AggCall]  # None if this is a field reference
    alias: Optional[str] = None

    def output_key(self) -> str:
        if self.alias:
            return self.alias
        if self.field:
            return self.field.to_string()
        if self.agg:
            return self.agg.canonical_string()
        return ""


@dataclass
class SelectClause:
    star: bool
    items: List[SelectItem]  # For non-star selects


@dataclass
class Query:
    select: SelectClause
    where: Optional[BooleanExpr]
    group_by: Optional[List[FieldRef]]


# Parser

class ParseError(Exception):
    pass


AGGREGATE_FUNCS = {
    TokenType.COUNT: 'COUNT',
    TokenType.SUM: 'SUM',
    TokenType.AVG: 'AVG',
    TokenType.AVERAGE: 'AVG',  # AVERAGE is synonym for AVG
    TokenType.MIN: 'MIN',
    TokenType.MAX: 'MAX',
    TokenType.UNIQUE: 'UNIQUE',
}


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def current(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 0) -> Token:
        pos = self.pos + offset
        if pos < len(self.tokens):
            return self.tokens[pos]
        return self.tokens[-1]  # EOF

    def advance(self) -> Token:
        token = self.current()
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return token

    def expect(self, token_type: str) -> Token:
        token = self.current()
        if token.type != token_type:
            raise ParseError(f"Expected {token_type}, got {token.type} at position {token.pos}")
        return self.advance()

    def match(self, *token_types: str) -> bool:
        return self.current().type in token_types

    def parse(self) -> Query:
        # SELECT
        self.expect(TokenType.SELECT)
        select = self.parse_select_list()

        # FROM logs
        self.expect(TokenType.FROM)
        ident = self.expect(TokenType.IDENT)
        if ident.value != 'logs':
            raise ParseError(f"Table must be 'logs', got '{ident.value}'")

        # Optional WHERE
        where = None
        if self.match(TokenType.WHERE):
            self.advance()
            where = self.parse_boolean_expr()

        # Optional GROUP BY
        group_by = None
        if self.match(TokenType.GROUP):
            self.advance()
            self.expect(TokenType.BY)
            group_by = self.parse_group_list()

        self.expect(TokenType.EOF)

        # Semantic validation
        self.validate_query(select, group_by)

        return Query(select, where, group_by)

    def validate_query(self, select: SelectClause, group_by: Optional[List[FieldRef]]):
        if select.star:
            has_agg = any(item.agg is not None for item in select.items)
            if has_agg:
                raise ParseError("SELECT * is invalid when aggregate functions are used")

        output_keys = []
        for item in select.items:
            key = item.output_key()
            if key in output_keys:
                raise ParseError(f"Duplicate output key '{key}' in select list")
            output_keys.append(key)

        if group_by:
            group_keys = []
            for field in group_by:
                key = field.to_string()
                if key in group_keys:
                    raise ParseError(f"Duplicate field '{key}' in GROUP BY")
                group_keys.append(key)

        has_aggregates = any(item.agg is not None for item in select.items)
        non_agg_fields = [item.field for item in select.items if item.field is not None]

        if has_aggregates:
            if group_by is None:
                for field in non_agg_fields:
                    raise ParseError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")
            else:
                group_by_strs = [f.to_string() for f in group_by]
                for field in non_agg_fields:
                    if field.to_string() not in group_by_strs:
                        raise ParseError(f"Non-aggregate field '{field.to_string()}' must be in GROUP BY")

    def parse_select_list(self) -> SelectClause:
        if self.match(TokenType.STAR):
            self.advance()
            return SelectClause(star=True, items=[])

        items = []
        item = self.parse_select_item()
        items.append(item)

        while self.match(TokenType.COMMA):
            self.advance()
            item = self.parse_select_item()
            items.append(item)

        return SelectClause(star=False, items=items)

    def parse_select_item(self) -> SelectItem:
        if self.match(*AGGREGATE_FUNCS.keys()):
            agg = self.parse_agg_call()
            alias = None
            if self.match(TokenType.AS):
                self.advance()
                alias_token = self.expect(TokenType.IDENT)
                alias = alias_token.value
            return SelectItem(field=None, agg=agg, alias=alias)
        else:
            field = self.parse_field_ref()
            alias = None
            if self.match(TokenType.AS):
                self.advance()
                alias_token = self.expect(TokenType.IDENT)
                alias = alias_token.value
            return SelectItem(field=field, agg=None, alias=alias)

    def parse_agg_call(self) -> AggCall:
        func_token = self.advance()
        func_name = AGGREGATE_FUNCS[func_token.type]

        self.expect(TokenType.LPAREN)

        if self.match(TokenType.STAR):
            self.advance()
            self.expect(TokenType.RPAREN)
            if func_name != 'COUNT':
                raise ParseError(f"{func_name}(*) is not valid, only COUNT(*) supports *")
            return AggCall(func='COUNT', field=None, is_star=True)
        else:
            field = self.parse_field_ref()
            self.expect(TokenType.RPAREN)
            return AggCall(func=func_name, field=field, is_star=False)

    def parse_group_list(self) -> List[FieldRef]:
        fields = []
        field = self.parse_field_ref()
        fields.append(field)

        while self.match(TokenType.COMMA):
            self.advance()
            field = self.parse_field_ref()
            field_str = field.to_string()
            for existing in fields:
                if existing.to_string() == field_str:
                    raise ParseError(f"Duplicate field '{field_str}' in GROUP BY")
            fields.append(field)

        return fields

    def parse_field_ref(self) -> FieldRef:
        parts = []

        ident = self.expect(TokenType.IDENT)
        parts.append(ident.value)

        while self.match(TokenType.DOT):
            self.advance()
            ident = self.expect(TokenType.IDENT)
            parts.append(ident.value)

        return FieldRef(parts)

    def parse_boolean_expr(self) -> BooleanExpr:
        return self.parse_or_expr()

    def parse_or_expr(self) -> BooleanExpr:
        left = self.parse_and_expr()

        while self.match(TokenType.OR):
            self.advance()
            right = self.parse_and_expr()
            left = BinaryExpr(left, 'OR', right)

        return left

    def parse_and_expr(self) -> BooleanExpr:
        left = self.parse_primary_expr()

        while self.match(TokenType.AND):
            self.advance()
            right = self.parse_primary_expr()
            left = BinaryExpr(left, 'AND', right)

        return left

    def parse_primary_expr(self) -> BooleanExpr:
        if self.match(TokenType.LPAREN):
            self.advance()
            expr = self.parse_boolean_expr()
            self.expect(TokenType.RPAREN)
            return expr

        return self.parse_predicate()

    def parse_predicate(self) -> Predicate:
        field = self.parse_field_ref()

        if self.match(TokenType.EQ, TokenType.NE, TokenType.LT,
                      TokenType.LE, TokenType.GT, TokenType.GE):
            op_token = self.advance()
            op = op_token.value
        else:
            raise ParseError(f"Expected comparison operator at position {self.current().pos}")

        literal = self.parse_literal()

        return Predicate(field, op, literal)

    def parse_literal(self) -> Literal:
        token = self.current()

        if token.type == TokenType.NUMBER:
            self.advance()
            return Literal(token.value)

        if token.type == TokenType.STRING:
            self.advance()
            return Literal(token.value)

        if token.type == TokenType.TRUE:
            self.advance()
            return Literal(True)

        if token.type == TokenType.FALSE:
            self.advance()
            return Literal(False)

        if token.type == TokenType.NULL:
            self.advance()
            return Literal(None)

        raise ParseError(f"Expected literal value at position {token.pos}")


# Evaluator

class EvaluationError(Exception):
    pass


def get_field_value(record: dict, field: FieldRef) -> Any:
    """Get the value of a dotted field reference from a record."""
    value = record
    for part in field.parts:
        if value is None:
            return None
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def compare_values(left: Any, op: str, right: Any) -> bool:
    """Compare two values with the given operator."""
    # Handle null comparisons
    if op == '=':
        if right is None:
            # = null matches null values and missing fields (which resolve to null)
            return left is None
        if left is None:
            return False  # non-null != null
    elif op == '!=':
        if right is None:
            # != null matches present, non-null scalar values
            if left is None:
                return False
            # Objects and arrays are not scalars
            if isinstance(left, (dict, list)):
                return False
            return True
        if left is None:
            return True  # null != non-null
    else:
        # Ordering operators with null always false
        if left is None or right is None:
            return False

    # Both non-null from here
    if left is None or right is None:
        return False

    # Type mismatch
    if type(left) != type(right):
        return False

    # Objects and arrays cannot be compared (except = null which is handled above)
    if isinstance(left, (dict, list)):
        return False

    # Boolean comparisons
    if isinstance(left, bool):
        if op in ('<', '<=', '>', '>='):
            return False
        if op == '=':
            return left == right
        if op == '!=':
            return left != right

    # String and number comparisons
    if op == '=':
        return left == right
    if op == '!=':
        return left != right
    if op == '<':
        return left < right
    if op == '<=':
        return left <= right
    if op == '>':
        return left > right
    if op == '>=':
        return left >= right

    return False


def evaluate_predicate(record: dict, pred: Predicate) -> bool:
    """Evaluate a predicate against a record."""
    left = get_field_value(record, pred.field)
    right = pred.literal.value
    return compare_values(left, pred.op, right)


def evaluate_boolean_expr(record: dict, expr: BooleanExpr) -> bool:
    """Evaluate a boolean expression against a record."""
    if isinstance(expr, Predicate):
        return evaluate_predicate(record, expr)

    if isinstance(expr, BinaryExpr):
        left_result = evaluate_boolean_expr(record, expr.left)

        if expr.op == 'OR':
            return left_result or evaluate_boolean_expr(record, expr.right)
        elif expr.op == 'AND':
            return left_result and evaluate_boolean_expr(record, expr.right)

    return False


def project_record(record: dict, select: SelectClause) -> dict:
    """Project a record according to the select clause."""
    if select.star:
        return record

    result = {}
    for item in select.items:
        key = item.output_key()
        value = get_field_value(record, item.field)
        result[key] = value

    return result


# Aggregation

def deep_equal(a: Any, b: Any) -> bool:
    """Deep equality check for values, including arrays and objects."""
    if type(a) != type(b):
        return False

    if isinstance(a, dict):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(deep_equal(a[k], b[k]) for k in a)

    if isinstance(a, list):
        if len(a) != len(b):
            return False
        return all(deep_equal(x, y) for x, y in zip(a, b))

    return a == b


def normalize_for_grouping(value: Any) -> Any:
    """Normalize a value for use as a group key.

    Arrays and objects are coerced to null for grouping.
    """
    if isinstance(value, (dict, list)):
        return None
    return value


def make_hashable(value: Any) -> Any:
    """Convert a value to a hashable representation for dict keys."""
    if value is None:
        return None
    if isinstance(value, bool):
        # Need to distinguish True from 1
        return ('__bool__', value)
    if isinstance(value, int):
        return ('__int__', value)
    if isinstance(value, float):
        return ('__float__', value)
    if isinstance(value, str):
        return ('__str__', value)
    # For other types, use the value directly
    return value


class AggregateState:
    def update(self, value: Any, record: dict):
        raise NotImplementedError

    def result(self) -> Any:
        raise NotImplementedError


class CountStarState(AggregateState):
    def __init__(self):
        self.count = 0

    def update(self, value: Any, record: dict):
        self.count += 1

    def result(self) -> Any:
        return self.count


class CountFieldState(AggregateState):
    def __init__(self):
        self.count = 0

    def update(self, value: Any, record: dict):
        if value is not None:
            self.count += 1

    def result(self) -> Any:
        return self.count


class SumState(AggregateState):
    def __init__(self):
        self.total = 0.0
        self.has_values = False

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.total += value
            self.has_values = True

    def result(self) -> Any:
        if not self.has_values:
            return None
        if self.total == int(self.total):
            return int(self.total)
        return self.total


class AvgState(AggregateState):
    def __init__(self):
        self.total = 0.0
        self.count = 0

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.total += value
            self.count += 1

    def result(self) -> Any:
        if self.count == 0:
            return None
        return self.total / self.count


class MinMaxState(AggregateState):
    def __init__(self, is_min: bool):
        self.is_min = is_min
        self.number_result = None
        self.string_result = None
        self.has_number = False
        self.has_string = False

    def update(self, value: Any, record: dict):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not self.has_number:
                self.number_result = value
                self.has_number = True
            else:
                if self.is_min:
                    self.number_result = min(self.number_result, value)
                else:
                    self.number_result = max(self.number_result, value)
        elif isinstance(value, str):
            if not self.has_string:
                self.string_result = value
                self.has_string = True
            else:
                if self.is_min:
                    self.string_result = min(self.string_result, value)
                else:
                    self.string_result = max(self.string_result, value)

    def result(self) -> Any:
        if self.has_number:
            if isinstance(self.number_result, float) and self.number_result == int(self.number_result):
                return int(self.number_result)
            return self.number_result
        if self.has_string:
            return self.string_result
        return None


class UniqueState(AggregateState):
    def __init__(self):
        self.values = []
        self.seen = []

    def update(self, value: Any, record: dict):
        for seen_val in self.seen:
            if deep_equal(value, seen_val):
                return
        self.seen.append(value)
        self.values.append(value)

    def result(self) -> Any:
        return self.values


def create_aggregate_state(agg: AggCall) -> AggregateState:
    if agg.func == 'COUNT':
        if agg.is_star:
            return CountStarState()
        return CountFieldState()
    elif agg.func == 'SUM':
        return SumState()
    elif agg.func == 'AVG':
        return AvgState()
    elif agg.func == 'MIN':
        return MinMaxState(is_min=True)
    elif agg.func == 'MAX':
        return MinMaxState(is_min=False)
    elif agg.func == 'UNIQUE':
        return UniqueState()
    else:
        raise EvaluationError(f"Unknown aggregate function: {agg.func}")


class GroupKey:
    def __init__(self, values: List[Any]):
        self.values = values

    def __hash__(self):
        return hash(tuple(make_hashable(v) for v in self.values))

    def __eq__(self, other):
        if not isinstance(other, GroupKey):
            return False
        if len(self.values) != len(other.values):
            return False
        return all(deep_equal(a, b) for a, b in zip(self.values, other.values))


class GroupState:
    def __init__(self, agg_calls: List[AggCall]):
        self.agg_states = [create_aggregate_state(agg) for agg in agg_calls]

    def update(self, record: dict, agg_calls: List[AggCall]):
        for i, agg in enumerate(agg_calls):
            value = None
            if not agg.is_star:
                value = get_field_value(record, agg.field)
            self.agg_states[i].update(value, record)

    def get_results(self) -> List[Any]:
        return [state.result() for state in self.agg_states]


def compute_aggregations(records: List[dict], query: Query) -> List[dict]:
    """Compute aggregations on filtered records."""
    select = query.select
    group_by = query.group_by

    # Collect aggregate calls and their output keys
    agg_calls = []
    agg_keys = []
    for item in select.items:
        if item.agg:
            agg_calls.append(item.agg)
            agg_keys.append(item.output_key())

    has_aggregates = len(agg_calls) > 0

    if not has_aggregates:
        return [project_record(r, select) for r in records]

    if group_by is None:
        group = GroupState(agg_calls)
        for record in records:
            group.update(record, agg_calls)
        results = group.get_results()
        return [{k: v for k, v in zip(agg_keys, results)}]

    groups = {}
    order_counter = 0

    for record in records:
        key_values = []
        for field in group_by:
            value = get_field_value(record, field)
            value = normalize_for_grouping(value)
            key_values.append(value)

        group_key = GroupKey(key_values)

        if group_key not in groups:
            groups[group_key] = (GroupState(agg_calls), order_counter)
            order_counter += 1

        group_state, _ = groups[group_key]
        group_state.update(record, agg_calls)

    sorted_groups = sorted(groups.values(), key=lambda x: x[1])

    results = []
    for group_state, _ in sorted_groups:
        agg_results = group_state.get_results()
        row = {}
        for i, item in enumerate(select.items):
            if item.agg:
                row[item.output_key()] = agg_results[agg_keys.index(item.output_key())]
            else:
                field_str = item.field.to_string()
                group_by_strs = [f.to_string() for f in group_by]
                idx = group_by_strs.index(field_str)
                for gk, (gs, _) in groups.items():
                    if gs is group_state:
                        row[item.output_key()] = gk.values[idx]
                        break
        results.append(row)

    return results


# Error Output

def output_error(error_type: str, message: str):
    """Output a structured error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message
    }
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


# Main

def parse_query(query_str: str) -> Query:
    """Parse a query string into a Query object."""
    try:
        lexer = Lexer(query_str)
        tokens = lexer.tokenize()
        parser = Parser(tokens)
        return parser.parse()
    except (LexerError, ParseError) as e:
        output_error("parse_error", str(e))


def process_logs(log_file: str, query: Query) -> List[dict]:
    """Process log file and return matching records."""
    records = []

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError as e:
                    output_error("json_error", f"Invalid JSON at line {line_num}: {e}")

                if not isinstance(record, dict):
                    output_error("json_error", f"Record at line {line_num} is not a JSON object")

                # Apply WHERE filter
                if query.where is not None:
                    if not evaluate_boolean_expr(record, query.where):
                        continue

                records.append(record)

    except FileNotFoundError:
        output_error("file_error", f"Log file not found: {log_file}")
    except IOError as e:
        output_error("file_error", f"Error reading log file: {e}")

    # Now compute aggregations
    return compute_aggregations(records, query)


def main():
    parser = argparse.ArgumentParser(description='LogQL Part 2 - Query NDJSON logs with GROUP BY and aggregations')
    parser.add_argument('--log-file', required=True, help='Path to NDJSON log file')
    parser.add_argument('--query', required=True, help='SQL-like query string')
    parser.add_argument('--output', help='Output file path (optional)')

    args = parser.parse_args()

    # Parse the query
    query = parse_query(args.query)

    # Process logs
    results = process_logs(args.log_file, query)

    # Output
    output_json = json.dumps(results)

    if args.output:
        try:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
                f.write('\n')
        except IOError as e:
            output_error("file_error", f"Error writing output file: {e}")
    else:
        print(output_json)


if __name__ == '__main__':
    main()
