#!/usr/bin/env python3
"""
MTL (Multi-Tenant Analytics Language) - File-based CLI for data transformation pipeline.
"""

import argparse
import csv
import json
import math
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import (
    Any, Callable, Dict, List, Optional, Set, Tuple, Union, get_type_hints
)


# =============================================================================
# Error Classes
# =============================================================================

class MTLException(Exception):
    """Base exception for MTL errors."""
    error_type: str

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def stderr_message(self) -> str:
        return f"ERROR:{self.error_type}:{self.message}"


class BadDSLException(MTLException):
    error_type = "bad_dsl"


class TypeErrorException(MTLException):
    error_type = "type_error"


class BadIOException(MTLException):
    error_type = "bad_io"


class RuntimeException(MTLException):
    error_type = "runtime_error"


# =============================================================================
# Type System
# =============================================================================

class ValueType(ABC):
    """Base class for value types in MTL."""
    pass


class StringType(ValueType):
    pass


class NumberType(ValueType):
    pass


class TimestampType(ValueType):
    pass


class BoolType(ValueType):
    pass


@dataclass
class MTLValue:
    """Represents a typed value in MTL."""
    value: Any
    type: ValueType

    def is_numeric(self) -> bool:
        return isinstance(self.type, NumberType)

    def is_string(self) -> bool:
        return isinstance(self.type, StringType)

    def is_timestamp(self) -> bool:
        return isinstance(self.type, TimestampType)

    def is_bool(self) -> bool:
        return isinstance(self.type, BoolType)

    def as_number(self) -> float:
        if self.is_timestamp():
            raise TypeError("Cannot convert timestamp to number")
        return float(self.value)

    def as_string(self) -> str:
        return str(self.value)

    def as_timestamp(self) -> datetime:
        if self.is_timestamp():
            return self.value
        raise TypeError("Not a timestamp")

    def as_bool(self) -> bool:
        return bool(self.value)


# =============================================================================
# Expression System
# =============================================================================

@dataclass
class Expression(ABC):
    """Base class for all MTL expressions."""
    @abstractmethod
    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        pass

    @abstractmethod
    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        pass


@dataclass
class LiteralExpr(Expression):
    value: MTLValue

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        return self.value

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        return self.value.type


@dataclass
class ColumnRefExpr(Expression):
    name: str

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        if self.name not in context:
            raise RuntimeException(f"Unknown column: {self.name}")
        return context[self.name]

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        if self.name not in schema:
            raise TypeErrorException(f"Unknown column: {self.name}")
        return schema[self.name]


@dataclass
class BinaryOpExpr(Expression):
    left: Expression
    op: str
    right: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        lv = self.left.eval(context)
        rv = self.right.eval(context)

        if self.op in ("<", "<=", ">", ">=", "==", "!="):
            return self._compare(lv, rv)
        elif self.op == "&":
            return MTLValue(lv.as_bool() and rv.as_bool(), BoolType())
        elif self.op == "|":
            return MTLValue(lv.as_bool() or rv.as_bool(), BoolType())
        elif self.op in ("+", "-", "*", "/", "**"):
            if not (lv.is_numeric() and rv.is_numeric()):
                raise TypeErrorException(f"Numeric operation '{self.op}' requires numeric operands")
            result = self._numeric_op(lv.as_number(), rv.as_number(), self.op)
            if math.isnan(result) or math.isinf(result):
                raise RuntimeException(f"Numeric operation resulted in NaN/Inf: {self.op}")
            return MTLValue(result, NumberType())
        elif self.op == "in":
            if isinstance(lv.value, str) and isinstance(rv.value, str):
                return MTLValue(lv.value in rv.value, BoolType())
            else:
                raise TypeErrorException("'in' requires string literal on left and string column on right")
        else:
            raise RuntimeException(f"Unknown operator: {self.op}")

    def _compare(self, lv: MTLValue, rv: MTLValue) -> MTLValue:
        if isinstance(lv.type, type(rv.type)):
            if isinstance(lv.type, StringType):
                result = self._string_compare(lv.value, rv.value, self.op)
            elif isinstance(lv.type, NumberType):
                result = self._numeric_compare(lv.as_number(), rv.as_number(), self.op)
            elif isinstance(lv.type, TimestampType):
                result = self._timestamp_compare(lv.value, rv.value, self.op)
            else:
                result = self._general_compare(lv.as_bool(), rv.as_bool(), self.op)
            return MTLValue(result, BoolType())
        else:
            raise TypeErrorException(f"Cannot compare {type(lv.type).__name__} with {type(rv.type).__name__}")

    def _string_compare(self, a: str, b: str, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _numeric_compare(self, a: float, b: float, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _timestamp_compare(self, a: datetime, b: datetime, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _general_compare(self, a: Any, b: Any, op: str) -> bool:
        if op == "==": return a == b
        if op == "!=": return a != b
        raise TypeErrorException(f"Cannot use '{op}' for boolean comparison")

    def _numeric_op(self, a: float, b: float, op: str) -> float:
        if op == "+": return a + b
        if op == "-": return a - b
        if op == "*": return a * b
        if op == "/":
            if b == 0:
                raise RuntimeException("Division by zero")
            return a / b
        if op == "**": return a ** b
        raise RuntimeException(f"Unknown numeric operator: {op}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        lt = self.left.get_type(schema)
        rt = self.right.get_type(schema)

        if self.op in ("<", "<=", ">", ">=", "==", "!=", "in"):
            return BoolType()
        elif self.op in ("&", "|"):
            if not (isinstance(lt, BoolType) and isinstance(rt, BoolType)):
                raise TypeErrorException(f"Logical operation requires boolean operands, got {type(lt).__name__} and {type(rt).__name__}")
            return BoolType()
        elif self.op in ("+", "-", "*", "/", "**"):
            if not (isinstance(lt, NumberType) and isinstance(rt, NumberType)):
                raise TypeErrorException(f"Arithmetic operation requires numeric operands, got {type(lt).__name__} and {type(rt).__name__}")
            return NumberType()
        else:
            raise TypeErrorException(f"Unknown operator: {self.op}")


@dataclass
class UnaryOpExpr(Expression):
    op: str
    operand: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        val = self.operand.eval(context)
        if self.op == "!":
            if not val.is_bool():
                raise TypeErrorException(f"'!' requires boolean operand, got {type(val.type).__name__}")
            return MTLValue(not val.as_bool(), BoolType())
        else:
            raise RuntimeException(f"Unknown unary operator: {self.op}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        if self.op == "!":
            t = self.operand.get_type(schema)
            if not isinstance(t, BoolType):
                raise TypeErrorException(f"'!' requires boolean operand, got {type(t).__name__}")
            return BoolType()
        raise TypeErrorException(f"Unknown unary operator: {self.op}")


@dataclass
class TransformExpr(Expression):
    """Represents a transform function call like len(col), day(col), etc."""
    func: str
    arg: Expression
    digits: Optional[Expression] = None

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        arg_val = self.arg.eval(context)

        if self.func == "len":
            if not arg_val.is_string():
                raise TypeErrorException("len() requires string argument")
            return MTLValue(len(arg_val.value), NumberType())

        elif self.func in ("day", "month", "year"):
            if not arg_val.is_timestamp():
                raise TypeErrorException(f"{self.func}() requires timestamp argument")
            ts = arg_val.as_timestamp()
            if self.func == "day":
                return MTLValue(ts.day, NumberType())
            elif self.func == "month":
                return MTLValue(ts.month, NumberType())
            else:
                return MTLValue(ts.year, NumberType())

        elif self.func == "round":
            if not arg_val.is_numeric():
                raise TypeErrorException("round() requires numeric argument")
            digits_val = 0
            if self.digits is not None:
                d = self.digits.eval(context)
                if not isinstance(d.type, NumberType):
                    raise TypeErrorException("round() digits must be integer")
                digits_val = int(d.as_number())
                if digits_val != d.as_number():
                    raise TypeErrorException("round() digits must be integer")
            val = arg_val.as_number()
            return MTLValue(round_half_away_from_zero(val, digits_val), NumberType())

        else:
            raise RuntimeException(f"Unknown transform function: {self.func}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        arg_type = self.arg.get_type(schema)

        if self.func == "len":
            if not isinstance(arg_type, StringType):
                raise TypeErrorException("len() requires string argument")
            return NumberType()

        elif self.func in ("day", "month", "year"):
            if not isinstance(arg_type, TimestampType):
                raise TypeErrorException(f"{self.func}() requires timestamp argument")
            return NumberType()

        elif self.func == "round":
            if not isinstance(arg_type, NumberType):
                raise TypeErrorException("round() requires numeric argument")
            if self.digits is not None:
                digits_type = self.digits.get_type(schema)
                if not isinstance(digits_type, NumberType):
                    raise TypeErrorException("round() digits must be integer")
            return NumberType()

        else:
            raise TypeErrorException(f"Unknown transform function: {self.func}")


def round_half_away_from_zero(value: float, decimals: int) -> float:
    """Round half away from zero (like Python 3's round)."""
    if decimals == 0:
        return round(value)
    multiplier = 10 ** decimals
    return round(value * multiplier) / multiplier


# =============================================================================
# DSL Parser
# =============================================================================

class TokenType(Enum):
    IDENTIFIER = 1
    STRING_LITERAL = 2
    NUMBER = 3
    BOOL = 4
    LPAREN = 5
    RPAREN = 6
    COMMA = 7
    EQ = 8  # "as"
    COMMENT = 9
    WHITESPACE = 10
    UNKNOWN = 11
    STAR = 12  # For count(*)


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        keywords = {"true": TokenType.BOOL, "false": TokenType.BOOL, "as": TokenType.EQ}
        operators = {
            "(": TokenType.LPAREN,
            ")": TokenType.RPAREN,
            ",": TokenType.COMMA,
            "&": TokenType.IDENTIFIER,  # Will be handled specially
            "|": TokenType.IDENTIFIER,
            "!": TokenType.IDENTIFIER,
            "<": TokenType.IDENTIFIER,
            ">": TokenType.IDENTIFIER,
            "=": TokenType.IDENTIFIER,  # part of ==, !=
        }

        while self.pos < len(self.text):
            char = self.text[self.pos]

            # Skip whitespace
            if char.isspace():
                if char == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.pos += 1
                continue

            # Comments
            if char == "#":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
                continue

            # Identifiers and keywords
            if char.isalpha() or char == "_":
                start = self.pos
                while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
                    self.pos += 1
                value = self.text[start:self.pos]
                if value.lower() in keywords:
                    self.tokens.append(Token(keywords[value.lower()], value, self.line, self.col - len(value)))
                else:
                    self.tokens.append(Token(TokenType.IDENTIFIER, value, self.line, self.col - len(value)))
                self.col += len(value)
                continue

            # Numbers
            if char.isdigit() or (char == "." and self.pos + 1 < len(self.text) and self.text[self.pos + 1].isdigit()):
                start = self.pos
                has_dot = char == "."
                if not has_dot:
                    self.pos += 1
                while self.pos < len(self.text):
                    if self.text[self.pos].isdigit():
                        self.pos += 1
                    elif self.text[self.pos] == "." and not has_dot:
                        has_dot = True
                        self.pos += 1
                    else:
                        break
                value = self.text[start:self.pos]
                self.tokens.append(Token(TokenType.NUMBER, value, self.line, self.col))
                self.col += len(value)
                continue

            # String literals (single quotes)
            if char == "'":
                start = self.pos
                self.pos += 1
                while self.pos < len(self.text) and self.text[self.pos] != "'":
                    if self.text[self.pos] == "\\":
                        self.pos += 2
                    else:
                        self.pos += 1
                if self.pos >= len(self.text):
                    raise BadDSLException(f"Unterminated string literal at line {self.line}")
                self.pos += 1  # Skip closing quote
                value = self.text[start + 1:self.pos - 1]
                self.tokens.append(Token(TokenType.STRING_LITERAL, value, self.line, self.col))
                self.col += len(value) + 2
                continue

            # Multi-character operators
            if char in ("<", ">", "="):
                if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "=":
                    value = char + "="
                    self.tokens.append(Token(TokenType.IDENTIFIER, value, self.line, self.col))
                    self.pos += 2
                    self.col += 2
                    continue
                elif char == "!" and self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "=":
                    value = "!="
                    self.tokens.append(Token(TokenType.IDENTIFIER, value, self.line, self.col))
                    self.pos += 2
                    self.col += 2
                    continue

            # Single char tokens
            if char in operators:
                self.tokens.append(Token(operators[char], char, self.line, self.col))
                self.pos += 1
                self.col += 1
                continue

            # Star (for count(*))
            if char == "*":
                self.tokens.append(Token(TokenType.STAR, "*", self.line, self.col))
                self.pos += 1
                self.col += 1
                continue

            # Unknown character
            self.tokens.append(Token(TokenType.UNKNOWN, char, self.line, self.col))
            self.pos += 1
            self.col += 1

        return self.tokens


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type: Optional[TokenType] = None, expected_value: Optional[str] = None) -> Token:
        token = self.peek()
        if token is None:
            raise BadDSLException(f"Unexpected end of input")
        if expected_type is not None and token.type != expected_type:
            raise BadDSLException(f"Expected {expected_type}, got {token.type} at line {token.line}")
        if expected_value is not None and token.value != expected_value:
            raise BadDSLException(f"Expected '{expected_value}', got '{token.value}' at line {token.line}")
        self.pos += 1
        return token

    def parse_pipeline(self) -> List[Any]:
        """Parse a complete pipeline (sequence of statements)."""
        statements = []
        while self.peek() is not None:
            stmt = self.parse_statement()
            statements.append(stmt)
        return statements

    def parse_statement(self) -> Any:
        """Parse a single statement."""
        # Check for function call pattern: identifier(
        token = self.peek()
        if token is None:
            raise BadDSLException("Unexpected end of input")

        # Look ahead to determine statement type
        if token.type == TokenType.IDENTIFIER and token.value == "filter":
            return self.parse_filter()
        elif token.type == TokenType.IDENTIFIER and token.value == "apply":
            return self.parse_apply()
        elif token.type == TokenType.IDENTIFIER and token.value == "group_by":
            return self.parse_group_by()
        elif token.type == TokenType.IDENTIFIER and token.value == "window":
            return self.parse_window()
        elif token.type == TokenType.IDENTIFIER and token.value == "aggregate":
            return self.parse_aggregate()
        else:
            raise BadDSLException(f"Unknown statement: {token.value} at line {token.line}")

    def parse_filter(self) -> Dict[str, Expression]:
        self.consume(TokenType.IDENTIFIER, "filter")
        self.consume(TokenType.LPAREN)
        expr = self.parse_boolean_expr()
        self.consume(TokenType.RPAREN)
        return {"type": "filter", "expr": expr}

    def parse_apply(self) -> Dict[str, Any]:
        self.consume(TokenType.IDENTIFIER, "apply")
        self.consume(TokenType.LPAREN)
        expr = self.parse_expression()
        self.consume(TokenType.COMMA)
        new_col = self.consume(TokenType.IDENTIFIER)
        self.consume(TokenType.RPAREN)
        return {"type": "apply", "expr": expr, "new_col": new_col.value}

    def parse_group_by(self) -> Dict[str, Any]:
        self.consume(TokenType.IDENTIFIER, "group_by")
        self.consume(TokenType.LPAREN)
        cols = []

        if self.peek().type == TokenType.LPAREN:
            # Array syntax: group_by([col1, col2])
            self.consume(TokenType.LPAREN)
            cols.append(self.consume(TokenType.IDENTIFIER).value)
            while self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA)
                cols.append(self.consume(TokenType.IDENTIFIER).value)
            self.consume(TokenType.RPAREN)
        else:
            # Single column syntax
            cols.append(self.consume(TokenType.IDENTIFIER).value)

        self.consume(TokenType.RPAREN)
        return {"type": "group_by", "cols": cols}

    def parse_window(self) -> Dict[str, Any]:
        self.consume(TokenType.IDENTIFIER, "window")
        self.consume(TokenType.LPAREN)
        duration = self.consume(TokenType.IDENTIFIER)
        self.consume(TokenType.RPAREN)
        return {"type": "window", "duration": duration.value}

    def parse_aggregate(self) -> Dict[str, Any]:
        self.consume(TokenType.IDENTIFIER, "aggregate")
        aggs = []

        while True:
            func = self.consume(TokenType.IDENTIFIER)
            self.consume(TokenType.LPAREN)

            peek = self.peek()
            if peek is None or peek.type != TokenType.STAR:
                col = self.consume(TokenType.IDENTIFIER)
                arg = ColumnRefExpr(col.value)
            else:
                self.consume(TokenType.STAR)
                arg = LiteralExpr(MTLValue("*", StringType()))

            self.consume(TokenType.RPAREN)
            self.consume(TokenType.EQ, "as")
            alias = self.consume(TokenType.IDENTIFIER)

            aggs.append({"func": func.value, "arg": arg, "alias": alias.value})

            peek = self.peek()
            if peek is None or peek.type != TokenType.COMMA:
                break
            self.consume(TokenType.COMMA)

        return {"type": "aggregate", "aggs": aggs}

    def parse_boolean_expr(self) -> Expression:
        return self.parse_or_expr()

    def parse_or_expr(self) -> Expression:
        left = self.parse_and_expr()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "|":
            self.consume(TokenType.IDENTIFIER, "|")
            right = self.parse_and_expr()
            left = BinaryOpExpr(left, "|", right)
        return left

    def parse_and_expr(self) -> Expression:
        left = self.parse_not_expr()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "&":
            self.consume(TokenType.IDENTIFIER, "&")
            right = self.parse_not_expr()
            left = BinaryOpExpr(left, "&", right)
        return left

    def parse_not_expr(self) -> Expression:
        if self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "!":
            self.consume(TokenType.IDENTIFIER, "!")
            operand = self.parse_not_expr()
            return UnaryOpExpr("!", operand)
        return self.parse_comparison()

    def parse_comparison(self) -> Expression:
        left = self.parse_additive()

        if self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op_val = self.peek().value
            if op_val in ("<", ">", "==", "!=", "<=", ">=", "in"):
                op = self.consume(TokenType.IDENTIFIER).value
                right = self.parse_additive()
                return BinaryOpExpr(left, op, right)

        return left

    def parse_additive(self) -> Expression:
        left = self.parse_term()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op = self.peek().value
            if op in ("+", "-"):
                self.consume(TokenType.IDENTIFIER)
                right = self.parse_term()
                left = BinaryOpExpr(left, op, right)
            else:
                break
        return left

    def parse_term(self) -> Expression:
        left = self.parse_factor()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op = self.peek().value
            if op in ("*", "/", "**"):
                self.consume(TokenType.IDENTIFIER)
                right = self.parse_factor()
                left = BinaryOpExpr(left, op, right)
            else:
                break
        return left

    def parse_factor(self) -> Expression:
        if self.peek() is None:
            raise BadDSLException("Unexpected end of input")

        # Primary expression
        token = self.peek()

        # Parenthesized expression
        if token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN)
            expr = self.parse_boolean_expr()
            self.consume(TokenType.RPAREN)
            return expr

        # Function call: func(arg, ...) or func(arg, digits)
        if token.type == TokenType.IDENTIFIER and self.peek_plus(1) and self.peek_plus(1).type == TokenType.LPAREN:
            func_name = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.LPAREN)
            arg = self.parse_expression()
            digits = None
            if self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA)
                digits = self.parse_expression()
            self.consume(TokenType.RPAREN)
            return TransformExpr(func_name, arg, digits)

        # Literal values
        if token.type == TokenType.STRING_LITERAL:
            self.consume()
            return LiteralExpr(MTLValue(token.value, StringType()))

        if token.type == TokenType.NUMBER:
            self.consume()
            val = float(token.value)
            # Determine if it's an integer
            if val == int(val):
                return LiteralExpr(MTLValue(int(val), NumberType()))
            return LiteralExpr(MTLValue(val, NumberType()))

        if token.type == TokenType.BOOL:
            self.consume()
            return LiteralExpr(MTLValue(token.value.lower() == "true", BoolType()))

        # Identifier (column reference)
        if token.type == TokenType.IDENTIFIER:
            name = self.consume(TokenType.IDENTIFIER).value
            return ColumnRefExpr(name)

        raise BadDSLException(f"Unexpected token: {token.value} at line {token.line}")

    def peek_plus(self, offset: int) -> Optional[Token]:
        if self.pos + offset < len(self.tokens):
            return self.tokens[self.pos + offset]
        return None

    def parse_expression(self) -> Expression:
        """Parse a general expression (for apply arguments)."""
        return self.parse_boolean_expr()


def parse_dsl(dsl_text: str) -> List[Any]:
    """Parse DSL text into a list of statements."""
    lexer = Lexer(dsl_text)
    tokens = lexer.tokenize()

    # Filter out comments and whitespace for parsing
    clean_tokens = [t for t in tokens if t.type not in (TokenType.COMMENT, TokenType.WHITESPACE)]

    parser = Parser(clean_tokens)
    return parser.parse_pipeline()


# =============================================================================
# Data Processing Engine
# =============================================================================

@dataclass
class Row:
    """A row of data with typed values."""
    values: Dict[str, MTLValue]


class MTLEngine:
    def __init__(self, csv_path: str, dsl_path: str, tz: str = "UTC", timestamp_format: Optional[str] = None):
        self.tz = tz
        self.timestamp_format = timestamp_format
        self.schema: Dict[str, ValueType] = {}
        self.rows: List[Row] = []

        # Parse DSL
        if dsl_path == "-":
            dsl_text = sys.stdin.read()
        else:
            try:
                with open(dsl_path, 'r') as f:
                    dsl_text = f.read()
            except IOError as e:
                raise BadIOException(f"Cannot read DSL file: {e}")

        self.statements = parse_dsl(dsl_text)

        # Load CSV
        self._load_csv(csv_path)

    def _load_csv(self, csv_path: str):
        """Load CSV and infer schema."""
        try:
            with open(csv_path, 'r', newline='') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    raise BadIOException("CSV file has no header")

                # Infer types from column names and sample data
                for col in reader.fieldnames:
                    col_lower = col.lower()
                    if col_lower == "timestamp":
                        self.schema[col] = TimestampType()
                    elif col_lower in ("price", "quantity"):
                        self.schema[col] = NumberType()
                    else:
                        self.schema[col] = StringType()

                # Read all rows
                for row_dict in reader:
                    row_values = {}
                    for col, val_str in row_dict.items():
                        row_values[col] = self._parse_value(col, val_str)
                    self.rows.append(Row(row_values))

        except FileNotFoundError:
            raise BadIOException(f"CSV file not found: {csv_path}")
        except IOError as e:
            raise BadIOException(f"Cannot read CSV file: {e}")

    def _parse_value(self, col: str, val_str: str) -> MTLValue:
        """Parse a string value according to its schema type."""
        if col not in self.schema:
            # Unknown column, try to infer
            try:
                return MTLValue(int(val_str), NumberType())
            except ValueError:
                try:
                    return MTLValue(float(val_str), NumberType())
                except ValueError:
                    return MTLValue(val_str, StringType())

        col_type = self.schema[col]

        if isinstance(col_type, TimestampType):
            return self._parse_timestamp(val_str)
        elif isinstance(col_type, NumberType):
            try:
                if '.' in val_str or 'e' in val_str.lower():
                    return MTLValue(float(val_str), NumberType())
                else:
                    return MTLValue(int(val_str), NumberType())
            except ValueError:
                raise TypeErrorException(f"Cannot parse '{val_str}' as number for column {col}")
        else:
            return MTLValue(val_str, StringType())

    def _parse_timestamp(self, ts_str: str) -> MTLValue:
        """Parse a timestamp string."""
        try:
            # Try ISO 8601 first
            if 'T' in ts_str or (ts_str.count('-') == 2 and ts_str.count(':') >= 2):
                # Handle offset if present
                if '+' in ts_str or (ts_str.count('-') > 2):
                    # Has timezone offset
                    ts_str = ts_str.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(ts_str.replace('+00:00', '+00:00'))
                else:
                    # No offset - assume UTC
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            else:
                # Use custom format or parse as naive with tz
                if self.timestamp_format:
                    dt = datetime.strptime(ts_str, self.timestamp_format)
                else:
                    dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    # Apply default timezone
                    from zoneinfo import ZoneInfo
                    tz_obj = ZoneInfo(self.tz)
                    dt = dt.replace(tzinfo=tz_obj)

            return MTLValue(dt, TimestampType())
        except Exception as e:
            raise TypeErrorException(f"Cannot parse timestamp '{ts_str}': {e}")

    def execute(self) -> Any:
        """Execute the transformation pipeline."""
        # Process statements in order
        group_by_cols: List[str] = []
        window_duration: Optional[str] = None
        aggregate_spec: Optional[Dict] = None

        # Separate DML and aggregation
        dml_statements = []

        for stmt in self.statements:
            if stmt["type"] == "filter":
                dml_statements.append(stmt)
            elif stmt["type"] == "apply":
                dml_statements.append(stmt)
            elif stmt["type"] == "group_by":
                if group_by_cols:
                    raise BadDSLException("Multiple group_by statements not allowed")
                group_by_cols = stmt["cols"]
            elif stmt["type"] == "window":
                if window_duration:
                    raise BadDSLException("Multiple window statements not allowed")
                window_duration = stmt["duration"]
            elif stmt["type"] == "aggregate":
                if aggregate_spec:
                    raise BadDSLException("Multiple aggregate statements not allowed")
                aggregate_spec = stmt

        if not aggregate_spec:
            raise BadDSLException("Missing required aggregate statement")

        # Apply DML statements to filter/transform rows
        current_rows = self.rows

        for stmt in dml_statements:
            if stmt["type"] == "filter":
                current_rows = self._apply_filter(current_rows, stmt["expr"])
            elif stmt["type"] == "apply":
                current_rows = self._apply_apply(current_rows, stmt["expr"], stmt["new_col"])

        # Apply windowing if specified
        if window_duration:
            current_rows = self._apply_window(current_rows, window_duration)

        # Apply grouping and aggregation
        return self._apply_aggregation(current_rows, aggregate_spec, group_by_cols, window_duration)

    def _apply_filter(self, rows: List[Row], expr: Expression) -> List[Row]:
        """Filter rows based on expression."""
        filtered = []
        for row in rows:
            try:
                result = expr.eval(row.values)
                if result.as_bool():
                    filtered.append(row)
            except RuntimeException:
                raise
            except Exception as e:
                raise TypeErrorException(f"Filter evaluation error: {e}")
        return filtered

    def _apply_apply(self, rows: List[Row], expr: Expression, new_col: str) -> List[Row]:
        """Apply transform and add new column."""
        for row in rows:
            try:
                result = expr.eval(row.values)
                row.values[new_col] = result
                self.schema[new_col] = result.type
            except RuntimeException:
                raise
            except Exception as e:
                raise TypeErrorException(f"Apply evaluation error: {e}")
        return rows

    def _apply_window(self, rows: List[Row], duration: str) -> List[Row]:
        """Add window_start and window_end columns."""
        # Parse duration
        match = self._parse_duration(duration)
        unit_multipliers = {"m": 60, "h": 3600, "d": 86400}
        duration_seconds = match[0] * unit_multipliers[match[1]]

        # Unix epoch
        epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        for row in rows:
            ts = row.values["timestamp"].as_timestamp()
            # Convert to UTC if needed
            if ts.tzinfo is None:
                from zoneinfo import ZoneInfo
                tz_obj = ZoneInfo(self.tz)
                ts = ts.replace(tzinfo=tz_obj)
            ts_utc = ts.astimezone(timezone.utc)

            # Calculate window start
            diff_seconds = (ts_utc - epoch).total_seconds()
            window_start_seconds = int(diff_seconds // duration_seconds) * duration_seconds
            window_start = epoch + timedelta(seconds=window_start_seconds)
            window_end = window_start + timedelta(seconds=duration_seconds)

            row.values["window_start"] = MTLValue(window_start, TimestampType())
            row.values["window_end"] = MTLValue(window_end, TimestampType())

        return rows

    def _parse_duration(self, duration: str) -> Tuple[int, str]:
        """Parse duration literal like '5m', '1h', '7d'."""
        if duration.endswith('m'):
            return int(duration[:-1]), 'm'
        elif duration.endswith('h'):
            return int(duration[:-1]), 'h'
        elif duration.endswith('d'):
            return int(duration[:-1]), 'd'
        else:
            raise BadDSLException(f"Invalid duration literal: {duration}")

    def _apply_aggregation(self, rows: List[Row], aggregate_spec: Dict,
                           group_by_cols: List[str], window_duration: Optional[str]) -> Any:
        """Apply aggregation and return result."""
        if not rows:
            # Handle empty result case
            if not group_by_cols and not window_duration:
                return self._build_empty_aggregates(aggregate_spec)
            else:
                return []

        # Group rows if group_by specified
        groups: Dict[Tuple, List[Row]] = {}

        if group_by_cols:
            for row in rows:
                # Extract raw values from MTLValue objects for hashing
                key_values = []
                for col in group_by_cols:
                    val = row.values.get(col)
                    if val is not None:
                        # Extract raw value from MTLValue
                        if hasattr(val, 'value'):
                            key_values.append(val.value)
                        else:
                            key_values.append(val)
                    else:
                        key_values.append(None)
                key = tuple(key_values)
                if key not in groups:
                    groups[key] = []
                groups[key].append(row)
        else:
            # Single group
            groups[()] = rows

        # Build results
        results = []

        for key, group_rows in groups.items():
            result_obj = {}

            # Add group-by keys
            if group_by_cols:
                for i, col in enumerate(group_by_cols):
                    val = key[i]
                    if val is not None:
                        # Handle MTLValue or raw value
                        if hasattr(val, 'value'):
                            result_obj[col] = val.value
                        else:
                            result_obj[col] = val
                    else:
                        result_obj[col] = None

            # Add window keys if windowing
            if window_duration:
                # Get window_start and window_end from first row
                ws = group_rows[0].values["window_start"]
                we = group_rows[0].values["window_end"]
                result_obj["window_start"] = ws.value.strftime("%Y-%m-%dT%H:%M:%SZ")
                result_obj["window_end"] = we.value.strftime("%Y-%m-%dT%H:%M:%SZ")

            # Compute aggregates
            for agg in aggregate_spec["aggs"]:
                alias = agg["alias"]
                func = agg["func"]
                arg_expr = agg["arg"]

                if isinstance(arg_expr, LiteralExpr) and arg_expr.value.value == "*":
                    # count(*)
                    if func == "count":
                        result_obj[alias] = len(group_rows)
                    else:
                        raise TypeErrorException(f"Aggregate {func}(*) is not valid")
                else:
                    # Regular aggregate over column
                    col_expr = arg_expr

                    # Get column name
                    if isinstance(col_expr, ColumnRefExpr):
                        col_name = col_expr.name
                    else:
                        # For now, assume it's a column reference
                        col_name = col_expr.name if hasattr(col_expr, 'name') else str(col_expr)

                    # Extract numeric values
                    values = []
                    for row in group_rows:
                        if col_name in row.values:
                            val = row.values[col_name]
                            if val.is_numeric():
                                values.append(val.as_number())

                    if not values:
                        if func == "count":
                            result_obj[alias] = 0
                        else:
                            result_obj[alias] = None
                    else:
                        result_obj[alias] = self._compute_aggregate(func, values)

            results.append(result_obj)

        # Sort results
        results = self._sort_results(results, group_by_cols, window_duration)

        return results

    def _compute_aggregate(self, func: str, values: List[float]) -> Any:
        """Compute aggregate function on values."""
        n = len(values)

        if func == "sum":
            return sum(values)
        elif func == "average":
            return sum(values) / n
        elif func == "median":
            sorted_vals = sorted(values)
            mid = n // 2
            if n % 2 == 1:
                return sorted_vals[mid]
            else:
                return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
        elif func == "count":
            return n
        elif func == "std":
            if n == 0:
                return None
            mean = sum(values) / n
            variance = sum((x - mean) ** 2 for x in values) / n
            return math.sqrt(variance)
        elif func == "var":
            if n == 0:
                return None
            mean = sum(values) / n
            return sum((x - mean) ** 2 for x in values) / n
        elif func == "min":
            return min(values)
        elif func == "max":
            return max(values)
        else:
            raise TypeErrorException(f"Unknown aggregate function: {func}")

    def _build_empty_aggregates(self, aggregate_spec: Dict) -> Dict:
        """Build empty aggregate result."""
        result = {}
        for agg in aggregate_spec["aggs"]:
            alias = agg["alias"]
            func = agg["func"]
            if func == "count":
                result[alias] = 0
            else:
                result[alias] = None
        return result

    def _sort_results(self, results: List[Dict], group_by_cols: List[str],
                     window_duration: Optional[str]) -> List[Dict]:
        """Sort results according to normalization rules."""

        def sort_key(item: Dict) -> Tuple:
            key_parts = []

            if window_duration:
                ws = item.get("window_start", "")
                key_parts.append(ws)

            if group_by_cols:
                for col in group_by_cols:
                    val = item.get(col)
                    # Handle None values - put them last
                    if val is None:
                        # Use a value that sorts last
                        if isinstance(item.get("window_start"), str):
                            key_parts.append((1, ""))
                        else:
                            key_parts.append((1, float('inf') if isinstance(val, (int, float)) else ""))
                    elif isinstance(val, str):
                        key_parts.append((0, val))
                    elif isinstance(val, (int, float)):
                        key_parts.append((2, val))
                    else:
                        key_parts.append((3, str(val)))

            return tuple(key_parts)

        return sorted(results, key=sort_key)


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MTL - Multi-Tenant Analytics Language CLI"
    )
    parser.add_argument("--csv", required=True, help="Path to input CSV file")
    parser.add_argument("--dsl", required=True, help="Path to DSL file (use '-' for STDIN)")
    parser.add_argument("--out", default=None, help="Output file (default: STDOUT)")
    parser.add_argument("--tz", default="UTC", help="Default timezone (default: UTC)")
    parser.add_argument("--timestamp-format", default=None,
                        help="Timestamp format (default: ISO 8601)")

    args = parser.parse_args()

    try:
        engine = MTLEngine(
            csv_path=args.csv,
            dsl_path=args.dsl,
            tz=args.tz,
            timestamp_format=args.timestamp_format
        )

        result = engine.execute()

        # Output
        output_json = json.dumps(result, indent=2)

        if args.out:
            with open(args.out, 'w') as f:
                f.write(output_json)
        else:
            print(output_json)

        sys.exit(0)

    except MTLException as e:
        print(e.stderr_message(), file=sys.stderr)
        sys.exit(getattr(e, 'exit_code', 1))


if __name__ == "__main__":
    main()
