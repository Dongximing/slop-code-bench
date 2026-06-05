#!/usr/bin/env python3
"""
LogQL - Part 1: Parsing, Filtering, Projection
A command-line program that reads NDJSON logs and executes SQL-like queries.
"""

import sys
import json
import argparse
from enum import Enum
from typing import Any, Optional


class TokenType(Enum):
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    AND = "AND"
    OR = "OR"
    GROUP = "GROUP"
    BY = "BY"
    AS = "AS"
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    AVERAGE = "AVERAGE"
    MIN = "MIN"
    MAX = "MAX"
    UNIQUE = "UNIQUE"
    STAR = "*"
    COMMA = ","
    IDENT = "IDENT"
    STRING = "STRING"
    NUMBER = "NUMBER"
    TRUE = "TRUE"
    FALSE = "FALSE"
    NULL = "NULL"
    EQ = "="
    NE = "!="
    LT = "<"
    LTE = "<="
    GT = ">"
    GTE = ">="
    LPAREN = "("
    RPAREN = ")"
    DOT = "."
    EOF = "EOF"


class Token:
    __slots__ = ('type', 'value')

    def __init__(self, type: TokenType, value: str):
        self.type = type
        self.value = value

    def __repr__(self):
        return f"Token({self.type}, {repr(self.value)})"


class Lexer:
    """Tokenizes the query string."""

    KEYWORDS = {
        'SELECT': TokenType.SELECT,
        'FROM': TokenType.FROM,
        'WHERE': TokenType.WHERE,
        'AND': TokenType.AND,
        'OR': TokenType.OR,
        'GROUP': TokenType.GROUP,
        'BY': TokenType.BY,
        'AS': TokenType.AS,
        'COUNT': TokenType.COUNT,
        'SUM': TokenType.SUM,
        'AVG': TokenType.AVG,
        'AVERAGE': TokenType.AVERAGE,
        'MIN': TokenType.MIN,
        'MAX': TokenType.MAX,
        'UNIQUE': TokenType.UNIQUE,
        'true': TokenType.TRUE,
        'false': TokenType.FALSE,
        'null': TokenType.NULL,
    }

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.tokens = []

    def _peek(self) -> Optional[str]:
        if self.pos < len(self.query):
            return self.query[self.pos]
        return None

    def _advance(self) -> str:
        char = self.query[self.pos]
        self.pos += 1
        return char

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.query):
            char = self._peek()

            # Skip whitespace
            if char in ' \t\n\r':
                self._advance()
                continue

            # Check for two-character operators
            if self.pos + 1 < len(self.query):
                two_char = self.query[self.pos:self.pos + 2]
                if two_char == '<=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.LTE, '<='))
                    continue
                if two_char == '>=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.GTE, '>='))
                    continue
                if two_char == '!=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.NE, '!='))
                    continue

            # Check for single-character tokens
            if char == '*':
                self._advance()
                self.tokens.append(Token(TokenType.STAR, '*'))
                continue
            if char == ',':
                self._advance()
                self.tokens.append(Token(TokenType.COMMA, ','))
                continue
            if char == '(':
                self._advance()
                self.tokens.append(Token(TokenType.LPAREN, '('))
                continue
            if char == ')':
                self._advance()
                self.tokens.append(Token(TokenType.RPAREN, ')'))
                continue
            if char == '<':
                self._advance()
                self.tokens.append(Token(TokenType.LT, '<'))
                continue
            if char == '>':
                self._advance()
                self.tokens.append(Token(TokenType.GT, '>'))
                continue
            if char == '=':
                self._advance()
                self.tokens.append(Token(TokenType.EQ, '='))
                continue
            if char == '.':
                self._advance()
                self.tokens.append(Token(TokenType.DOT, '.'))
                continue
            if char == '!':
                self._advance()
                raise SyntaxError(f"Unexpected character '!'")

            # Check for string literal
            if char == '"':
                self._advance()  # Skip opening quote
                value_chars = []
                while self.pos < len(self.query):
                    c = self.query[self.pos]
                    if c == '\\':
                        self._advance()
                        if self.pos >= len(self.query):
                            raise SyntaxError("Unterminated string literal")
                        next_char = self.query[self.pos]
                        self._advance()
                        if next_char in '"\\':
                            value_chars.append(next_char)
                        else:
                            # Invalid escape, keep as-is
                            value_chars.append('\\')
                            value_chars.append(next_char)
                    elif c == '"':
                        self._advance()  # Skip closing quote
                        break
                    else:
                        self._advance()
                        value_chars.append(c)
                else:
                    raise SyntaxError("Unterminated string literal")
                self.tokens.append(Token(TokenType.STRING, ''.join(value_chars)))
                continue

            # Check for number
            if char.isdigit() or (char == '-' and self.pos + 1 < len(self.query) and self.query[self.pos + 1].isdigit()):
                start = self.pos
                if char == '-':
                    self._advance()
                while self.pos < len(self.query) and self.query[self.pos].isdigit():
                    self._advance()
                if self.pos < len(self.query) and self.query[self.pos] == '.':
                    self._advance()
                    while self.pos < len(self.query) and self.query[self.pos].isdigit():
                        self._advance()
                num_str = self.query[start:self.pos]
                self.tokens.append(Token(TokenType.NUMBER, num_str))
                continue

            # Check for identifier/keyword
            if char.isalpha() or char == '_':
                start = self.pos
                while self.pos < len(self.query) and (self.query[self.pos].isalnum() or self.query[self.pos] == '_'):
                    self._advance()
                ident = self.query[start:self.pos]
                ident_upper = ident.upper()
                token_type = self.KEYWORDS.get(ident_upper, TokenType.IDENT)
                self.tokens.append(Token(token_type, ident))
                continue

            # Invalid character
            raise SyntaxError(f"Unexpected character '{char}'")

        self.tokens.append(Token(TokenType.EOF, ''))
        return self.tokens


class ASTNode:
    """Base class for AST nodes."""
    pass


class FieldRef(ASTNode):
    __slots__ = ('parts',)

    def __init__(self, parts: list[str]):
        self.parts = parts

    def __repr__(self):
        return f"FieldRef({self.parts})"


class AggregateFunc(Enum):
    COUNT = "COUNT"
    SUM = "SUM"
    AVG = "AVG"
    AVERAGE = "AVERAGE"
    MIN = "MIN"
    MAX = "MAX"
    UNIQUE = "UNIQUE"


class AggregateCall(ASTNode):
    __slots__ = ('func', 'arg', 'is_star')

    def __init__(self, func: AggregateFunc, arg=None, is_star: bool = False):
        self.func = func
        self.arg = arg
        self.is_star = is_star

    def __repr__(self):
        if self.is_star:
            return f"AggregateCall({self.func.value}, *)"
        return f"AggregateCall({self.func.value}, {self.arg})"


class SelectItem(ASTNode):
    __slots__ = ('item', 'alias')

    def __init__(self, item, alias: Optional[str] = None):
        self.item = item
        self.alias = alias

    def __repr__(self):
        if self.alias:
            return f"SelectItem({self.item} AS {self.alias})"
        return f"SelectItem({self.item})"


class ColumnList(ASTNode):
    __slots__ = ('columns', 'is_star')

    def __init__(self, columns: list, is_star: bool = False):
        self.columns = columns
        self.is_star = is_star


class Query(ASTNode):
    __slots__ = ('columns', 'table', 'where', 'group_by')

    def __init__(self, columns, table: str, where=None, group_by=None):
        self.columns = columns
        self.table = table
        self.where = where
        self.group_by = group_by


class BinaryOp(ASTNode):
    __slots__ = ('left', 'op', 'right')

    def __init__(self, left, op: str, right):
        self.left = left
        self.op = op
        self.right = right

    def __repr__(self):
        return f"BinaryOp({self.left}, {self.op}, {self.right})"


class Literal(ASTNode):
    __slots__ = ('value', 'type')

    def __init__(self, value, type: str):
        self.value = value
        self.type = type  # 'string', 'number', 'boolean', 'null'

    def __repr__(self):
        return f"Literal({self.value})"


class Parser:
    """Parses tokens into an AST."""

    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0
        self.current = self.tokens[0] if tokens else None

    def _eat(self, token_type: TokenType, expected_value: Optional[str] = None) -> Token:
        if self.current.type != token_type:
            raise SyntaxError(f"Expected {token_type}, got {self.current.type}")
        if expected_value is not None and self.current.value != expected_value:
            raise SyntaxError(f"Expected '{expected_value}', got '{self.current.value}'")
        token = self.current
        self.pos += 1
        self.current = self.tokens[self.pos] if self.pos < len(self.tokens) else None
        return token

    def _peek_type(self) -> TokenType:
        if self.current:
            return self.current.type
        return TokenType.EOF

    def parse(self) -> Query:
        """Parse the entire query."""
        self._eat(TokenType.SELECT, 'SELECT')
        columns = self._parse_select_list()
        self._eat(TokenType.FROM, 'FROM')
        table_token = self._eat(TokenType.IDENT)
        if table_token.value.lower() != 'logs':
            raise SyntaxError(f"Expected table name 'logs', got '{table_token.value}'")

        where = None
        if self._peek_type() == TokenType.WHERE:
            self._eat(TokenType.WHERE, 'WHERE')
            where = self._parse_boolean_expr()

        group_by = None
        if self._peek_type() == TokenType.GROUP:
            self._eat(TokenType.GROUP)
            self._eat(TokenType.BY, 'BY')
            group_by = self._parse_group_list()

        self._eat(TokenType.EOF, '')
        return Query(columns, 'logs', where, group_by)

    def _parse_select_list(self):
        """Parse SELECT * or SELECT field, field, ... or aggregate calls."""
        if self._peek_type() == TokenType.STAR:
            self._eat(TokenType.STAR)
            return ColumnList([], is_star=True)

        items = []
        seen_keys = set()
        while True:
            item = self._parse_select_item()
            if isinstance(item, SelectItem):
                # Get output key for duplicate checking
                if isinstance(item.item, FieldRef):
                    key = '.'.join(item.item.parts) if item.alias is None else item.alias
                elif isinstance(item.item, AggregateCall):
                    key = self._get_canonical_aggregate_key(item.item) if item.alias is None else item.alias
                else:
                    key = str(item.item) if item.alias is None else item.alias

                if key in seen_keys:
                    raise SyntaxError(f"Duplicate output key: {key}")
                seen_keys.add(key)

            items.append(item)

            if self._peek_type() == TokenType.COMMA:
                self._eat(TokenType.COMMA)
                if self._peek_type() in (TokenType.EOF, TokenType.WHERE, TokenType.GROUP):
                    raise SyntaxError("Unexpected comma at end of select list")
                continue
            break

        return ColumnList(items)

    def _parse_select_item(self):
        """Parse a select item: field_ref AS ident | agg_call AS ident | agg_call | field_ref."""
        # Check if it's an aggregate call
        agg_call = self._try_parse_aggregate_call()
        if agg_call:
            alias = None
            if self._peek_type() == TokenType.AS:
                self._eat(TokenType.AS)
                alias = self._eat(TokenType.IDENT).value
            return SelectItem(agg_call, alias)

        # It's a field reference
        field_ref = self._parse_field_ref()
        alias = None
        if self._peek_type() == TokenType.AS:
            self._eat(TokenType.AS)
            alias = self._eat(TokenType.IDENT).value
        return SelectItem(field_ref, alias)

    def _try_parse_aggregate_call(self):
        """Try to parse an aggregate call. Returns None if not an aggregate."""
        token_type = self._peek_type()
        func_map = {
            TokenType.COUNT: AggregateFunc.COUNT,
            TokenType.SUM: AggregateFunc.SUM,
            TokenType.AVG: AggregateFunc.AVG,
            TokenType.AVERAGE: AggregateFunc.AVG,
            TokenType.MIN: AggregateFunc.MIN,
            TokenType.MAX: AggregateFunc.MAX,
            TokenType.UNIQUE: AggregateFunc.UNIQUE,
        }

        if token_type not in func_map:
            return None

        func = func_map[token_type]
        self._eat(token_type)

        # Expect opening paren
        if self._peek_type() != TokenType.LPAREN:
            raise SyntaxError(f"Expected '(' after {func.value}")
        self._eat(TokenType.LPAREN)

        # Check for COUNT(*)
        if func == AggregateFunc.COUNT and self._peek_type() == TokenType.STAR:
            self._eat(TokenType.STAR)
            self._eat(TokenType.RPAREN)
            return AggregateCall(func, is_star=True)

        # Parse field reference argument
        arg = self._parse_field_ref()
        self._eat(TokenType.RPAREN)
        return AggregateCall(func, arg)

    def _parse_field_ref(self) -> FieldRef:
        """Parse an identifier or dotted identifiers."""
        parts = []

        while self._peek_type() == TokenType.IDENT:
            ident_token = self._eat(TokenType.IDENT)
            parts.append(ident_token.value)

            # Check for dot
            if self._peek_type() == TokenType.DOT:
                self._eat(TokenType.DOT)
                # The next token should be an identifier
                if self._peek_type() != TokenType.IDENT:
                    raise SyntaxError(f"Expected identifier after '.', got {self.current.type}")
            else:
                break

        if not parts:
            raise SyntaxError("Expected identifier in field reference")
        return FieldRef(parts)

    def _parse_boolean_expr(self):
        """Parse boolean expression with AND/OR precedence."""
        return self._parse_or_expr()

    def _parse_or_expr(self):
        """Parse OR expressions (lowest precedence)."""
        left = self._parse_and_expr()

        while self._peek_type() == TokenType.OR:
            self._eat(TokenType.OR)
            right = self._parse_and_expr()
            left = BinaryOp(left, 'OR', right)

        return left

    def _parse_and_expr(self):
        """Parse AND expressions."""
        left = self._parse_predicate()

        while self._peek_type() == TokenType.AND:
            self._eat(TokenType.AND)
            right = self._parse_predicate()
            left = BinaryOp(left, 'AND', right)

        return left

    def _parse_predicate(self):
        """Parse a predicate or parenthesized expression."""
        if self._peek_type() == TokenType.LPAREN:
            self._eat(TokenType.LPAREN)
            expr = self._parse_boolean_expr()
            self._eat(TokenType.RPAREN)
            return expr

        # field_ref op literal
        field_ref = self._parse_field_ref()

        op_type = self._peek_type()
        op_map = {
            TokenType.EQ: '=',
            TokenType.NE: '!=',
            TokenType.LT: '<',
            TokenType.LTE: '<=',
            TokenType.GT: '>',
            TokenType.GTE: '>=',
        }

        if op_type not in op_map:
            raise SyntaxError(f"Expected comparison operator, got {self.current}")

        op = op_map[op_type]
        self._eat(op_type)

        literal = self._parse_literal()
        return BinaryOp(field_ref, op, literal)

    def _parse_literal(self):
        """Parse a literal value."""
        token_type = self._peek_type()

        if token_type == TokenType.STRING:
            token = self._eat(TokenType.STRING)
            return Literal(token.value, 'string')

        elif token_type == TokenType.NUMBER:
            token = self._eat(TokenType.NUMBER)
            val = token.value
            if '.' in val:
                return Literal(float(val), 'number')
            else:
                return Literal(int(val), 'number')

        elif token_type == TokenType.TRUE:
            self._eat(TokenType.TRUE)
            return Literal(True, 'boolean')

        elif token_type == TokenType.FALSE:
            self._eat(TokenType.FALSE)
            return Literal(False, 'boolean')

        elif token_type == TokenType.NULL:
            self._eat(TokenType.NULL)
            return Literal(None, 'null')

        raise SyntaxError(f"Expected literal, got {self.current}")

    def _parse_group_list(self):
        """Parse GROUP BY field list."""
        fields = []
        seen = set()
        while True:
            field_ref = self._parse_field_ref()
            key = '.'.join(field_ref.parts)
            if key in seen:
                raise SyntaxError(f"Duplicate field in GROUP BY: {key}")
            seen.add(key)
            fields.append(field_ref)

            if self._peek_type() == TokenType.COMMA:
                self._eat(TokenType.COMMA)
                if self._peek_type() in (TokenType.EOF, TokenType.WHERE):
                    raise SyntaxError("Unexpected comma at end of GROUP BY list")
                continue
            break

        return fields

    def _get_canonical_aggregate_key(self, agg_call: AggregateCall) -> str:
        """Get the canonical key string for an aggregate call."""
        func_name = agg_call.func.value
        if agg_call.is_star:
            return f"{func_name}(*)"
        elif agg_call.arg:
            return f"{func_name}({'.'.join(agg_call.arg.parts)})"
        return func_name


class Evaluator:
    """Evaluates boolean expressions against records."""

    @staticmethod
    def get_field_value(record: dict, field_ref: FieldRef) -> Any:
        """Get the value of a dotted field reference from a record."""
        value = record
        for part in field_ref.parts:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
            if value is None:
                return None
        return value

    @staticmethod
    def evaluate(expr, record: dict) -> bool:
        """Evaluate a boolean expression against a record."""
        if isinstance(expr, BinaryOp):
            return Evaluator._eval_binary(expr, record)
        else:
            return True  # Should not happen

    @staticmethod
    def _eval_binary(expr: BinaryOp, record: dict) -> bool:
        """Evaluate a binary operation."""

        if expr.op == 'AND':
            left = Evaluator.evaluate(expr.left, record)
            if not left:
                return False
            return Evaluator.evaluate(expr.right, record)

        elif expr.op == 'OR':
            left = Evaluator.evaluate(expr.left, record)
            if left:
                return True
            return Evaluator.evaluate(expr.right, record)

        # Comparison operators
        left_val = Evaluator._get_value(expr.left, record)
        right_val = Evaluator._get_value(expr.right, record)

        if expr.op == '=':
            # Handle null comparison specially
            if isinstance(expr.right, Literal) and expr.right.type == 'null':
                return left_val is None
            return Evaluator._equal(left_val, right_val)

        elif expr.op == '!=':
            if isinstance(expr.right, Literal) and expr.right.type == 'null':
                return left_val is not None
            return not Evaluator._equal(left_val, right_val)

        elif expr.op in ('<', '<=', '>', '>='):
            # Ordering operators with null always false
            if left_val is None or right_val is None:
                return False
            # Ordering with non-scalar always false
            if not isinstance(left_val, (int, float, str, bool)) or not isinstance(right_val, (int, float, str, bool)):
                return False
            # Booleans don't support ordering
            if isinstance(left_val, bool) or isinstance(right_val, bool):
                return False
            return Evaluator._compare(left_val, right_val, expr.op)

        return False

    @staticmethod
    def _get_value(expr, record: dict) -> Any:
        """Get the value of an expression node."""
        if isinstance(expr, FieldRef):
            return Evaluator.get_field_value(record, expr)
        elif isinstance(expr, Literal):
            return expr.value
        return None

    @staticmethod
    def _equal(a: Any, b: Any) -> bool:
        """Check equality with null handling."""
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        # Type checking
        if type(a) != type(b):
            # Allow int/float comparison
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a == b
            return False
        return a == b

    @staticmethod
    def _compare(a: Any, b: Any, op: str) -> bool:
        """Compare two values."""
        if type(a) != type(b):
            # Allow int/float comparison
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                pass  # Continue with comparison
            else:
                return False

        if op == '<':
            return a < b
        elif op == '<=':
            return a <= b
        elif op == '>':
            return a > b
        elif op == '>=':
            return a >= b
        return False


class Projection:
    """Handles projection of records to selected fields."""

    @staticmethod
    def project(record: dict, columns: ColumnList) -> dict:
        """Project a record to the selected columns."""
        if columns.is_star:
            return record

        result = {}
        for field_ref in columns.columns:
            key = '.'.join(field_ref.parts)
            value = Evaluator.get_field_value(record, field_ref)
            result[key] = value
        return result


class LogQL:
    """Main LogQL query processor."""

    def __init__(self, log_file: str):
        self.log_file = log_file
        self.records = []
        self._load_records()

    def _load_records(self):
        """Load records from NDJSON file."""
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError(f"Line {line_num}: Expected JSON object, got {type(record).__name__}")
                        self.records.append(record)
                    except json.JSONDecodeError as e:
                        raise ValueError(f"Line {line_num}: Invalid JSON - {e}")
        except FileNotFoundError:
            raise ValueError(f"Log file not found: {self.log_file}")
        except PermissionError:
            raise ValueError(f"Permission denied: {self.log_file}")

    def _deep_eq(self, a, b):
        """Deep equality check for values including objects and arrays."""
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        if type(a) != type(b):
            # Allow int/float comparison
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return a == b
            return False
        if isinstance(a, dict) and isinstance(b, dict):
            if len(a) != len(b):
                return False
            # For dicts, compare key-value pairs ignoring key order
            # Sort by keys for consistent comparison
            a_items = sorted(a.items(), key=lambda x: x[0])
            b_items = sorted(b.items(), key=lambda x: x[0])
            for (ak, av), (bk, bv) in zip(a_items, b_items):
                if ak != bk or not self._deep_eq(av, bv):
                    return False
            return True
        if isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                return False
            for av, bv in zip(a, b):
                if not self._deep_eq(av, bv):
                    return False
            return True
        return a == b

    def _get_group_key(self, record: dict, group_fields: list[FieldRef]):
        """Get a hashable group key from fields."""
        key_parts = []
        for field in group_fields:
            val = Evaluator.get_field_value(record, field)
            # Arrays and objects coerce to null for grouping
            if isinstance(val, (list, dict)):
                val = None
            key_parts.append(val)
        return tuple(key_parts)

    def _get_field_value_for_aggregate(self, record: dict, field_ref: Optional[FieldRef]):
        """Get field value for aggregate computation. Returns None if field is missing/null."""
        if field_ref is None:
            return None
        return Evaluator.get_field_value(record, field_ref)

    @staticmethod
    def _get_canonical_aggregate_key(agg_call: AggregateCall) -> str:
        """Get the canonical key string for an aggregate call."""
        func_name = agg_call.func.value
        if agg_call.is_star:
            return f"{func_name}(*)"
        elif agg_call.arg:
            return f"{func_name}({'.'.join(agg_call.arg.parts)})"
        return func_name

    def _compute_aggregates(self, records: list[dict], columns: ColumnList, query=None) -> dict:
        """Compute aggregate values over a list of records."""
        result = {}
        parser = Parser([])  # Create temporary parser for helper methods

        for item in columns.columns:
            if isinstance(item, SelectItem):
                agg_call = item.item if isinstance(item.item, AggregateCall) else None
                field_ref = item.item if isinstance(item.item, FieldRef) else None
                alias = item.alias

                if agg_call:
                    # Handle aggregate
                    output_key = alias or self._get_canonical_aggregate_key(agg_call)
                    if agg_call.func == AggregateFunc.COUNT:
                        if agg_call.is_star:
                            result[output_key] = len(records)
                        else:
                            count = 0
                            for r in records:
                                val = self._get_field_value_for_aggregate(r, agg_call.arg)
                                # Arrays and objects count as present
                                if val is not None:
                                    count += 1
                            result[output_key] = count
                    elif agg_call.func == AggregateFunc.SUM:
                        total = 0
                        has_numeric = False
                        for r in records:
                            val = self._get_field_value_for_aggregate(r, agg_call.arg)
                            if isinstance(val, (int, float)):
                                total += val
                                has_numeric = True
                        result[output_key] = total if has_numeric else None
                    elif agg_call.func in (AggregateFunc.AVG, AggregateFunc.AVERAGE):
                        total = 0
                        count = 0
                        for r in records:
                            val = self._get_field_value_for_aggregate(r, agg_call.arg)
                            if isinstance(val, (int, float)):
                                total += val
                                count += 1
                        result[output_key] = (total / count) if count > 0 else None
                    elif agg_call.func == AggregateFunc.MIN:
                        min_val = None
                        has_numeric = False
                        has_string = False
                        for r in records:
                            val = self._get_field_value_for_aggregate(r, agg_call.arg)
                            if val is None:
                                continue
                            if isinstance(val, (int, float)):
                                has_numeric = True
                                if min_val is None or val < min_val:
                                    min_val = val
                            elif isinstance(val, str):
                                has_string = True
                        # Numeric mode takes precedence
                        if has_numeric:
                            result[output_key] = min_val
                        elif has_string:
                            # Find min string
                            str_vals = []
                            for r in records:
                                val = self._get_field_value_for_aggregate(r, agg_call.arg)
                                if isinstance(val, str):
                                    str_vals.append(val)
                            result[output_key] = min(str_vals) if str_vals else None
                        else:
                            result[output_key] = None
                    elif agg_call.func == AggregateFunc.MAX:
                        max_val = None
                        has_numeric = False
                        has_string = False
                        for r in records:
                            val = self._get_field_value_for_aggregate(r, agg_call.arg)
                            if val is None:
                                continue
                            if isinstance(val, (int, float)):
                                has_numeric = True
                                if max_val is None or val > max_val:
                                    max_val = val
                            elif isinstance(val, str):
                                has_string = True
                        if has_numeric:
                            result[output_key] = max_val
                        elif has_string:
                            str_vals = []
                            for r in records:
                                val = self._get_field_value_for_aggregate(r, agg_call.arg)
                                if isinstance(val, str):
                                    str_vals.append(val)
                            result[output_key] = max(str_vals) if str_vals else None
                        else:
                            result[output_key] = None
                    elif agg_call.func == AggregateFunc.UNIQUE:
                        unique_vals = []
                        seen = []
                        for r in records:
                            val = self._get_field_value_for_aggregate(r, agg_call.arg)
                            # Check if we've seen this value before (deep equality)
                            found = False
                            for existing in seen:
                                if self._deep_eq(existing, val):
                                    found = True
                                    break
                            if not found:
                                seen.append(val)
                                unique_vals.append(val)
                        result[output_key] = unique_vals
                elif field_ref:
                    # Regular field projection
                    output_key = alias or '.'.join(field_ref.parts)
                    # For grouped queries, only include grouping fields
                    if columns.columns and not any(
                        isinstance(c, SelectItem) and isinstance(c.item, AggregateCall)
                        for c in columns.columns
                    ):
                        # Non-aggregate field in non-grouped context - Part 1 behavior
                        result[output_key] = self._get_field_value_for_aggregate(records[0] if records else {}, field_ref) if records else None
            else:
                # For backward compatibility with Part 1 ColumnList (FieldRef list)
                field_ref = item
                output_key = alias or '.'.join(field_ref.parts)
                result[output_key] = self._get_field_value_for_aggregate(records[0] if records else {}, field_ref) if records else None

        return result

    def execute(self, query_str: str) -> list[dict]:
        """Execute a query and return the results."""
        # Parse the query
        try:
            lexer = Lexer(query_str)
            tokens = lexer.tokenize()
            parser = Parser(tokens)
            query = parser.parse()
        except SyntaxError as e:
            raise ValueError(f"Syntax error: {e}")

        # Validate table name
        if query.table.lower() != 'logs':
            raise ValueError(f"Expected table 'logs', got '{query.table}'")

        # Filter records
        filtered_records = []
        for record in self.records:
            if query.where is None or Evaluator.evaluate(query.where, record):
                filtered_records.append(record)

        # Check if there are any aggregates in SELECT
        has_aggregates = False
        if isinstance(query.columns, ColumnList):
            for item in query.columns.columns:
                if isinstance(item, SelectItem) and isinstance(item.item, AggregateCall):
                    has_aggregates = True
                    break
                elif isinstance(item, AggregateCall):
                    has_aggregates = True
                    break

        # Part 1 behavior: no aggregates
        if not has_aggregates:
            # Regular projection without grouping
            results = []
            for record in filtered_records:
                if query.columns.is_star:
                    results.append(record)
                else:
                    projected = Projection.project(record, query.columns)
                    results.append(projected)
            return results

        # With aggregates
        # SELECT * is invalid when any aggregate appears
        if query.columns.is_star:
            raise ValueError("E_SEMANTIC: SELECT * is invalid with aggregate functions")

        # Validate: if GROUP BY present, all non-aggregate fields must be in GROUP BY
        if query.group_by:
            # Get non-aggregate fields from SELECT
            select_non_agg_fields = set()
            for item in query.columns.columns:
                if isinstance(item, SelectItem):
                    if isinstance(item.item, FieldRef):
                        select_non_agg_fields.add('.'.join(item.item.parts))
                elif isinstance(item, FieldRef):
                    select_non_agg_fields.add('.'.join(item.parts))

            # Get GROUP BY fields
            group_by_fields = set('.'.join(f.parts) for f in query.group_by)

            # Check if all non-aggregate select fields are in GROUP BY
            if not select_non_agg_fields.issubset(group_by_fields):
                raise ValueError("E_SEMANTIC: All non-aggregate SELECT fields must appear in GROUP BY")

        # Check for duplicate GROUP BY fields (already done in parser)

        if not query.group_by:
            # Global aggregation (no GROUP BY)
            # All items must be aggregates
            for item in query.columns.columns:
                if isinstance(item, SelectItem):
                    if not isinstance(item.item, AggregateCall):
                        raise ValueError("E_SEMANTIC: When using aggregates without GROUP BY, all SELECT items must be aggregates")
                elif isinstance(item, AggregateCall):
                    pass  # OK
                else:
                    raise ValueError("E_SEMANTIC: When using aggregates without GROUP BY, all SELECT items must be aggregates")

            # Compute global aggregates
            agg_result = self._compute_aggregates(filtered_records, query.columns)
            return [agg_result] if agg_result else []
        else:
            # GROUP BY: group records and compute aggregates per group
            groups = {}
            group_order = []

            for record in filtered_records:
                group_key = self._get_group_key(record, query.group_by)
                if group_key not in groups:
                    groups[group_key] = []
                    group_order.append(group_key)
                groups[group_key].append(record)

            results = []
            for group_key in group_order:
                group_records = groups[group_key]
                # Build result with grouping keys first, then aggregates
                result = {}

                # Add grouping key values
                for i, field in enumerate(query.group_by):
                    val = group_key[i]
                    # Determine output key - check if AS alias exists
                    output_key = None
                    for item in query.columns.columns:
                        if isinstance(item, SelectItem) and isinstance(item.item, FieldRef):
                            if '.'.join(item.item.parts) == '.'.join(field.parts):
                                output_key = item.alias or '.'.join(item.item.parts)
                                break
                        elif isinstance(item, FieldRef):
                            if '.'.join(item.parts) == '.'.join(field.parts):
                                output_key = '.'.join(item.parts)
                                break

                    if output_key is None:
                        output_key = '.'.join(field.parts)

                    result[output_key] = val

                # Add aggregate values
                for item in query.columns.columns:
                    if isinstance(item, SelectItem) and isinstance(item.item, AggregateCall):
                        agg = item.item
                        output_key = item.alias
                        if agg.func == AggregateFunc.COUNT:
                            if agg.is_star:
                                result[output_key or 'COUNT(*)'] = len(group_records)
                            else:
                                count = sum(1 for r in group_records
                                    if self._get_field_value_for_aggregate(r, agg.arg) is not None)
                                result[output_key or f"COUNT({'.'.join(agg.arg.parts)})"] = count
                        elif agg.func == AggregateFunc.SUM:
                            total = sum(
                                (self._get_field_value_for_aggregate(r, agg.arg) or 0)
                                for r in group_records
                                if isinstance(self._get_field_value_for_aggregate(r, agg.arg), (int, float))
                            )
                            has_numeric = any(isinstance(self._get_field_value_for_aggregate(r, agg.arg), (int, float))
                                            for r in group_records)
                            result[output_key or f"SUM({'.'.join(agg.arg.parts)})"] = total if has_numeric else None
                        elif agg.func in (AggregateFunc.AVG, AggregateFunc.AVERAGE):
                            vals = [self._get_field_value_for_aggregate(r, agg.arg) for r in group_records]
                            numeric_vals = [v for v in vals if isinstance(v, (int, float))]
                            result[output_key or f"AVG({'.'.join(agg.arg.parts)})"] = (
                                sum(numeric_vals) / len(numeric_vals) if numeric_vals else None
                            )
                        elif agg.func == AggregateFunc.MIN:
                            vals = [self._get_field_value_for_aggregate(r, agg.arg) for r in group_records]
                            numeric_vals = [v for v in vals if isinstance(v, (int, float))]
                            str_vals = [v for v in vals if isinstance(v, str)]
                            if numeric_vals:
                                result[output_key or f"MIN({'.'.join(agg.arg.parts)})"] = min(numeric_vals)
                            elif str_vals:
                                result[output_key or f"MIN({'.'.join(agg.arg.parts)})"] = min(str_vals)
                            else:
                                result[output_key or f"MIN({'.'.join(agg.arg.parts)})"] = None
                        elif agg.func == AggregateFunc.MAX:
                            vals = [self._get_field_value_for_aggregate(r, agg.arg) for r in group_records]
                            numeric_vals = [v for v in vals if isinstance(v, (int, float))]
                            str_vals = [v for v in vals if isinstance(v, str)]
                            if numeric_vals:
                                result[output_key or f"MAX({'.'.join(agg.arg.parts)})"] = max(numeric_vals)
                            elif str_vals:
                                result[output_key or f"MAX({'.'.join(agg.arg.parts)})"] = max(str_vals)
                            else:
                                result[output_key or f"MAX({'.'.join(agg.arg.parts)})"] = None
                        elif agg.func == AggregateFunc.UNIQUE:
                            vals = [self._get_field_value_for_aggregate(r, agg.arg) for r in group_records]
                            unique_vals = []
                            seen = []
                            for val in vals:
                                found = False
                                for existing in seen:
                                    if self._deep_eq(existing, val):
                                        found = True
                                        break
                                if not found:
                                    seen.append(val)
                                    unique_vals.append(val)
                            result[output_key or f"UNIQUE({'.'.join(agg.arg.parts)})"] = unique_vals

                results.append(result)

            return results


def main():
    parser = argparse.ArgumentParser(
        prog='logql.py',
        description='LogQL - Query NDJSON logs with SQL-like syntax',
        usage='%(prog)s --log-file <path> --query "<sql>" [--output <path>]'
    )
    parser.add_argument('--log-file', required=True, help='Path to NDJSON file')
    parser.add_argument('--query', required=True, help='Query string')
    parser.add_argument('--output', help='Output file (default: stdout)')

    args = parser.parse_args()

    try:
        logql = LogQL(args.log_file)
        results = logql.execute(args.query)

        output_json = json.dumps(results, ensure_ascii=False)

        if args.output:
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output_json)
        else:
            print(output_json)

    except ValueError as e:
        error = {"error": str(e)}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        error = {"error": f"Unexpected error: {e}"}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
