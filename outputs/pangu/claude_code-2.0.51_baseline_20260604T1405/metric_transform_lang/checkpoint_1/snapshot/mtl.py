#!/usr/bin/env python3
"""
MTL (Mini Transformation Language) Interpreter
Reads CSV events and applies DSL transformation pipeline to produce deterministic JSON.
"""

import sys
import re
import json
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Any, Optional, List, Dict, Tuple, Union
from pathlib import Path
import math


# =============================================================================
# Error Classes
# =============================================================================

class MTLError(Exception):
    """Base MTL error."""
    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        self.message = message
        super().__init__(f"ERROR:{error_type}:{message}")


class DLSError(MTLError):
    """DSL syntax/parse error (exit code 1)."""
    def __init__(self, message: str):
        super().__init__("bad_dsl", message)


class TypeError(MTLError):
    """Type error (exit code 2)."""
    def __init__(self, message: str):
        super().__init__("type_error", message)


class IOError(MTLError):
    """I/O error (exit code 3)."""
    def __init__(self, message: str):
        super().__init__("bad_io", message)


class RuntimeError(MTLError):
    """Runtime execution error (exit code 4)."""
    def __init__(self, message: str):
        super().__init__("runtime_error", message)


# =============================================================================
# Tokenizer and Parser for DSL
# =============================================================================

class TokenType:
    IDENTIFIER = 'IDENTIFIER'
    STRING = 'STRING'
    NUMBER = 'NUMBER'
    BOOL = 'BOOL'
    LPAREN = 'LPAREN'
    RPAREN = 'RPAREN'
    COMMA = 'COMMA'
    EQUALS = 'EQUALS'
    DOT = 'DOT'
    COLON = 'COLON'
    LBRACKET = 'LBRACKET'
    RBRACKET = 'RBRACKET'
    AND = 'AND'      # &
    OR = 'OR'        # |
    NOT = 'NOT'      # !
    COMP = 'COMP'    # ==, !=, <=, >=, <, >
    IN = 'IN'
    EOF = 'EOF'

    @staticmethod
    def name(token_type):
        names = {v: k for k, v in TokenType.__dict__.items() if not k.startswith('_')}
        return names.get(token_type, str(token_type))


class Token:
    def __init__(self, token_type: str, value: str, pos: int):
        self.type = token_type
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({TokenType.name(self.type)}, {repr(self.value)}, pos={self.pos})"


class DSLTokenizer:
    """Tokenizer for MTL DSL."""

    COMPARISON_OPERATORS = ['==', '!=', '<=', '>=', '<', '>']
    SINGLE_CHAR_OPERATORS = {
        '(': TokenType.LPAREN,
        ')': TokenType.RPAREN,
        ',': TokenType.COMMA,
        '=': TokenType.EQUALS,
        '[': TokenType.LBRACKET,
        ']': TokenType.RBRACKET,
        '.': TokenType.DOT,
        ':': TokenType.COLON,
        '&': TokenType.AND,
        '|': TokenType.OR,
        '!': TokenType.NOT,
    }

    KEYWORDS = {
        'aggregate': 'AGGREGATE',
        'filter': 'FILTER',
        'apply': 'APPLY',
        'group_by': 'GROUP_BY',
        'window': 'WINDOW',
        'as': 'AS',
        'len': 'LEN',
        'day': 'DAY',
        'month': 'MONTH',
        'year': 'YEAR',
        'round': 'ROUND',
        'sum': 'SUM',
        'average': 'AVERAGE',
        'median': 'MEDIAN',
        'count': 'COUNT',
        'std': 'STD',
        'var': 'VAR',
        'min': 'MIN',
        'max': 'MAX',
    }

    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.length = len(text)
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        """Convert source text to tokens."""
        while self.pos < self.length:
            char = self.text[self.pos]

            # Skip whitespace
            if char.isspace():
                self.pos += 1
                continue

            # Handle line comments
            if char == '#':
                # Skip to end of line
                while self.pos < self.length and self.text[self.pos] != '\n':
                    self.pos += 1
                continue

            # Handle empty lines (we'll handle them later in parser)
            if char == '\n':
                self.pos += 1
                continue

            # Check for comparison operators (multi-char first to check ==, !=, etc.)
            for op in self.COMPARISON_OPERATORS:
                if self.text.startswith(op, self.pos):
                    self.tokens.append(Token(TokenType.COMP, op, self.pos))
                    self.pos += len(op)
                    break
            else:
                # Check for single char operators
                if char in self.SINGLE_CHAR_OPERATORS:
                    self.tokens.append(Token(self.SINGLE_CHAR_OPERATORS[char], char, self.pos))
                    self.pos += 1
                    continue

                # Check for keywords and identifiers
                start = self.pos
                while self.pos < self.length and (self.text[self.pos].isalnum() or self.text[self.pos] == '_'):
                    self.pos += 1
                ident = self.text[start:self.pos]

                # Check if it's a keyword
                if ident in self.KEYWORDS:
                    # Special handling for 'in' keyword
                    if ident == 'in':
                        # Make sure it's not part of a longer identifier
                        if self.pos == self.length or not self.text[self.pos].isalnum():
                            self.tokens.append(Token(TokenType.IN, 'in', start))
                            continue
                    elif ident in ('true', 'false'):
                        self.tokens.append(Token(TokenType.BOOL, ident, start))
                        continue
                    # For other keywords, we could map to specific token types
                    # but for simplicity, treat as IDENTIFIER (parser handles keywords)
                    self.tokens.append(Token(TokenType.IDENTIFIER, ident, start))
                    continue
                else:
                    # It's an identifier
                    self.tokens.append(Token(TokenType.IDENTIFIER, ident, start))
                    continue

                # Handle strings
                if char == "'":
                    start = self.pos
                    self.pos += 1
                    string_value = ''
                    while self.pos < self.length and self.text[self.pos] != "'":
                        string_value += self.text[self.pos]
                        self.pos += 1
                    if self.pos >= self.length:
                        raise DLSError(f"Unterminated string starting at position {start}")
                    self.pos += 1  # Skip closing quote
                    self.tokens.append(Token(TokenType.STRING, string_value, start))
                    continue

                # Handle numbers
                if char.isdigit() or (char == '.' and self.pos + 1 < self.length and self.text[self.pos + 1].isdigit()):
                    start = self.pos
                    has_decimal = (char == '.')
                    self.pos += 1
                    while self.pos < self.length:
                        c = self.text[self.pos]
                        if c.isdigit() or (c == '.' and not has_decimal):
                            if c == '.':
                                has_decimal = True
                            self.pos += 1
                        else:
                            break
                    value = self.text[start:self.pos]
                    token_type = TokenType.NUMBER
                    self.tokens.append(Token(token_type, value, start))
                    continue

                # Handle boolean literals
                if self.text.startswith('true', self.pos) or self.text.startswith('false', self.pos):
                    end = self.pos + 4 if self.text.startswith('true', self.pos) else self.pos + 5
                    if end <= self.length:
                        # Check it's not part of a longer identifier
                        if end == self.length or not self.text[end].isalnum():
                            value = self.text[self.pos:end]
                            self.tokens.append(Token(TokenType.BOOL, value, self.pos))
                            self.pos = end
                            continue

                # Handle identifiers (bare words)
                start = self.pos
                while self.pos < self.length and (self.text[self.pos].isalnum() or self.text[self.pos] == '_'):
                    self.pos += 1
                ident = self.text[start:self.pos]
                self.tokens.append(Token(TokenType.IDENTIFIER, ident, start))
                continue

        self.tokens.append(Token(TokenType.EOF, '', self.pos))
        return self.tokens


# =============================================================================
# AST Node Classes
# =============================================================================

class AST:
    """Base class for AST nodes."""
    pass


class FilterStatement(AST):
    """filter(<bool_expr>)"""
    def __init__(self, expr):
        self.expr = expr


class ApplyStatement(AST):
    """apply(<transform_expr>, <new_col_name>)"""
    def __init__(self, transform_expr: 'TransformExpr', new_col: str):
        self.transform_expr = transform_expr
        self.new_col = new_col


class GroupByStatement(AST):
    """group_by(<col_name> | [<col_name>, ...])"""
    def __init__(self, columns: List[str]):
        self.columns = columns


class WindowStatement(AST):
    """window(<duration_literal>)"""
    def __init__(self, duration: str):
        self.duration = duration


class AggregateStatement(AST):
    """aggregate <agg_list>"""
    def __init__(self, aggregations: List['Aggregation']):
        self.aggregations = aggregations


class Aggregation:
    """<fn>(<col>|*) as <alias>"""
    def __init__(self, fn: str, col: Optional[str], alias: str):
        self.fn = fn
        self.col = col  # None for count(*)
        self.alias = alias


class TransformExpr:
    """Base class for transform expressions."""
    pass


class LenTransform(TransformExpr):
    """len(<string_col>) -> int"""
    def __init__(self, col: str):
        self.col = col


class DateTransform(TransformExpr):
    """day|month|year(<timestamp_col>) -> int"""
    def __init__(self, func: str, col: str):
        self.func = func  # 'day', 'month', or 'year'
        self.col = col


class RoundTransform(TransformExpr):
    """round(<number_col> [, <digits:int>]) -> number"""
    def __init__(self, col: str, digits: Optional[int] = None):
        self.col = col
        self.digits = digits


# Boolean expression AST nodes
class BoolExpr(AST):
    """Base class for boolean expressions."""
    pass


class BinOpBool(BoolExpr):
    def __init__(self, left: BoolExpr, op: str, right: BoolExpr):
        self.left = left
        self.op = op  # '&' or '|'
        self.right = right


class NotBool(BoolExpr):
    def __init__(self, expr: BoolExpr):
        self.expr = expr


class CompBool(BoolExpr):
    def __init__(self, left: 'ScalarExpr', op: str, right: 'ScalarExpr'):
        self.left = left
        self.op = op
        self.right = right


class InBool(BoolExpr):
    """string_literal in string_col"""
    def __init__(self, literal: str, col: str):
        self.literal = literal
        self.col = col


class ParenBool(BoolExpr):
    def __init__(self, expr: BoolExpr):
        self.expr = expr


# Scalar expression AST nodes
class ScalarExpr(AST):
    """Base class for scalar expressions (used in comparisons and transform args)."""
    pass


class ColumnRef(ScalarExpr):
    def __init__(self, name: str):
        self.name = name


class StringLiteral(ScalarExpr):
    def __init__(self, value: str):
        self.value = value


class NumberLiteral(ScalarExpr):
    def __init__(self, value: Union[int, float]):
        self.value = value


class BoolLiteral(ScalarExpr):
    def __init__(self, value: bool):
        self.value = value


# =============================================================================
# DSL Parser
# =============================================================================

class DSLParser:
    """Parser for MTL DSL."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.statements: List[AST] = []
        self.seen_aggregate = False
        self.seen_group_by = False
        self.seen_window = False

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def consume(self, expected_type: Optional[str] = None, expected_value: Optional[str] = None) -> Token:
        token = self.peek()
        if expected_type and token.type != expected_type:
            raise DLSError(f"Expected {TokenType.name(expected_type)}, got {TokenType.name(token.type)} at position {token.pos}")
        if expected_value is not None and token.value != expected_value:
            raise DLSError(f"Expected '{expected_value}', got '{token.value}' at position {token.pos}")
        self.pos += 1
        return token

    def parse(self) -> List[AST]:
        """Parse all statements."""
        while self.pos < len(self.tokens) and self.peek().type != TokenType.EOF:
            # Skip empty lines and comments (already handled by tokenizer)
            if self.peek().type == TokenType.EOF:
                break

            stmt = self.parse_statement()
            self.statements.append(stmt)

            # Expect newline or EOF
            if self.pos < len(self.tokens) and self.peek().type != TokenType.EOF:
                # Check if we have a newline
                if self.peek().type == TokenType.EOF:
                    pass
                # In our tokenizer, newlines are skipped, so we just continue
        return self.statements

    def parse_statement(self) -> AST:
        """Parse a single statement."""
        token = self.peek()

        if token.type != TokenType.IDENTIFIER:
            raise DLSError(f"Expected statement keyword, got '{token.value}' at position {token.pos}")

        keyword = token.value
        self.consume(TokenType.IDENTIFIER)

        if keyword == 'filter':
            return self.parse_filter()
        elif keyword == 'apply':
            return self.parse_apply()
        elif keyword == 'group_by':
            return self.parse_group_by()
        elif keyword == 'window':
            return self.parse_window()
        elif keyword == 'aggregate':
            return self.parse_aggregate()
        else:
            raise DLSError(f"Unknown statement '{keyword}' at position {token.pos}")

    def parse_filter(self) -> FilterStatement:
        """Parse filter(<bool_expr>)"""
        self.consume(TokenType.LPAREN, '(')
        expr = self.parse_bool_expr()
        self.consume(TokenType.RPAREN, ')')
        return FilterStatement(expr)

    def parse_apply(self) -> ApplyStatement:
        """Parse apply(<transform_expr>, <new_col_name>)"""
        self.consume(TokenType.LPAREN, '(')

        # Parse transform expression
        transform = self.parse_transform_expr()

        self.consume(TokenType.COMMA, ',')

        # Parse new column name
        col_token = self.consume(TokenType.IDENTIFIER)
        new_col = col_token.value

        self.consume(TokenType.RPAREN, ')')
        return ApplyStatement(transform, new_col)

    def parse_transform_expr(self) -> TransformExpr:
        """Parse a transform expression."""
        token = self.peek()

        if token.type != TokenType.IDENTIFIER:
            raise DLSError(f"Expected transform function name, got '{token.value}' at position {token.pos}")

        func_name = token.value
        self.consume(TokenType.IDENTIFIER)

        self.consume(TokenType.LPAREN, '(')

        if func_name == 'len':
            col_token = self.consume(TokenType.IDENTIFIER)
            col = col_token.value
            self.consume(TokenType.RPAREN, ')')
            return LenTransform(col)

        elif func_name in ('day', 'month', 'year'):
            col_token = self.consume(TokenType.IDENTIFIER)
            col = col_token.value
            self.consume(TokenType.RPAREN, ')')
            return DateTransform(func_name, col)

        elif func_name == 'round':
            col_token = self.consume(TokenType.IDENTIFIER)
            col = col_token.value

            digits = None
            if self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA, ',')
                digits_token = self.consume(TokenType.NUMBER)
                digits = int(digits_token.value)

            self.consume(TokenType.RPAREN, ')')
            return RoundTransform(col, digits)

        else:
            raise DLSError(f"Unknown transform function '{func_name}' at position {token.pos}")

    def parse_group_by(self) -> GroupByStatement:
        """Parse group_by(<col_name> | [<col_name>, ...])"""
        if self.seen_group_by:
            raise DLSError("Multiple group_by statements not allowed")
        self.seen_group_by = True

        self.consume(TokenType.LPAREN, '(')

        # Check for bracketed list or single column
        if self.peek().type == TokenType.LBRACKET:
            self.consume(TokenType.LBRACKET, '[')
            columns = []
            while True:
                col_token = self.consume(TokenType.IDENTIFIER)
                columns.append(col_token.value)
                if self.peek().type == TokenType.RBRACKET:
                    break
                self.consume(TokenType.COMMA, ',')
            self.consume(TokenType.RBRACKET, ']')
        else:
            col_token = self.consume(TokenType.IDENTIFIER)
            columns = [col_token.value]

        self.consume(TokenType.RPAREN, ')')
        return GroupByStatement(columns)

    def parse_window(self) -> WindowStatement:
        """Parse window(<duration_literal>)"""
        if self.seen_window:
            raise DLSError("Multiple window statements not allowed")
        self.seen_window = True

        self.consume(TokenType.LPAREN, '(')

        # Parse duration literal (e.g., "1h", "30m", "7d")
        if self.peek().type != TokenType.IDENTIFIER:
            raise DLSError(f"Expected duration literal, got '{self.peek().value}' at position {self.peek().pos}")

        duration_token = self.consume(TokenType.IDENTIFIER)
        duration = duration_token.value

        # Validate duration format
        if not re.match(r'^\d+(m|h|d)$', duration):
            raise DLSError(f"Invalid duration literal '{duration}' at position {duration_token.pos}")

        self.consume(TokenType.RPAREN, ')')
        return WindowStatement(duration)

    def parse_aggregate(self) -> AggregateStatement:
        """Parse aggregate <agg_list>"""
        if self.seen_aggregate:
            raise DLSError("Multiple aggregate statements not allowed")
        self.seen_aggregate = True

        # Parse aggregation list (comma-separated list)
        aggregations = []

        while True:
            agg = self.parse_aggregation()
            aggregations.append(agg)

            if self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA, ',')
            else:
                break

        return AggregateStatement(aggregations)

    def parse_aggregation(self) -> Aggregation:
        """Parse <fn>(<col>|*) as <alias>"""
        # Parse function name
        fn_token = self.consume(TokenType.IDENTIFIER)
        fn = fn_token.value

        self.consume(TokenType.LPAREN, '(')

        # Parse column or * for count
        if self.peek().type == TokenType.IDENTIFIER and self.peek().value == '*':
            col = None  # Special case for count(*)
            self.consume(TokenType.IDENTIFIER, '*')
        else:
            col_token = self.consume(TokenType.IDENTIFIER)
            col = col_token.value

        self.consume(TokenType.RPAREN, ')')

        self.consume(TokenType.EQUALS, 'as')

        alias_token = self.consume(TokenType.IDENTIFIER)
        alias = alias_token.value

        return Aggregation(fn, col, alias)

    def parse_bool_expr(self) -> BoolExpr:
        """Parse boolean expression with proper precedence: ! > & > |"""
        return self.parse_bool_expr_or()

    def parse_bool_expr_or(self) -> BoolExpr:
        """Parse OR expressions (lowest precedence)."""
        left = self.parse_bool_expr_and()

        while self.peek().type == TokenType.OR:
            op_token = self.consume(TokenType.OR)
            right = self.parse_bool_expr_and()
            left = BinOpBool(left, '|', right)

        return left

    def parse_bool_expr_and(self) -> BoolExpr:
        """Parse AND expressions."""
        left = self.parse_bool_expr_not()

        while self.peek().type == TokenType.AND:
            op_token = self.consume(TokenType.AND)
            right = self.parse_bool_expr_not()
            left = BinOpBool(left, '&', right)

        return left

    def parse_bool_expr_not(self) -> BoolExpr:
        """Parse NOT expressions (highest precedence)."""
        if self.peek().type == TokenType.NOT:
            self.consume(TokenType.NOT)
            expr = self.parse_bool_expr_not()
            return NotBool(expr)

        return self.parse_bool_expr_atom()

    def parse_bool_expr_atom(self) -> BoolExpr:
        """Parse atomic boolean expressions."""
        token = self.peek()

        # Parenthesized expression
        if token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN, '(')
            expr = self.parse_bool_expr()
            self.consume(TokenType.RPAREN, ')')
            return ParenBool(expr)

        # Comparison: scalar COMP scalar
        left = self.parse_scalar_expr()

        # Check for comparison operator
        if self.peek().type == TokenType.COMP:
            op_token = self.consume(TokenType.COMP)
            op = op_token.value
            right = self.parse_scalar_expr()
            return CompBool(left, op, right)

        # Check for 'in' operator: string_literal in string_col
        if self.peek().type == TokenType.IN:
            self.consume(TokenType.IN)
            # Right side must be a string column (identifier)
            col_token = self.consume(TokenType.IDENTIFIER)
            if left.type != TokenType.STRING:
                raise DLSError(f"Left side of 'in' must be a string literal, got '{left.value}' at position {left.pos}")
            return InBool(left.value, col_token.value)

        # Single boolean literal (for testing)
        if left.type == TokenType.BOOL:
            return BoolLiteral(left.value == 'true')

        raise DLSError(f"Expected comparison or 'in' operator, got '{self.peek().value}' at position {self.peek().pos}")

    def parse_scalar_expr(self) -> ScalarExpr:
        """Parse a scalar expression (column ref, literal)."""
        token = self.peek()

        if token.type == TokenType.IDENTIFIER:
            self.consume()
            # Check if it's a boolean literal
            if token.value in ('true', 'false'):
                return BoolLiteral(token.value == 'true')
            return ColumnRef(token.value)

        elif token.type == TokenType.STRING:
            self.consume()
            return StringLiteral(token.value)

        elif token.type == TokenType.NUMBER:
            self.consume()
            # Determine if integer or float
            if '.' in token.value:
                return NumberLiteral(float(token.value))
            else:
                return NumberLiteral(int(token.value))

        elif token.type == TokenType.BOOL:
            self.consume()
            return BoolLiteral(token.value == 'true')

        elif token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN, '(')
            expr = self.parse_scalar_expr()
            self.consume(TokenType.RPAREN, ')')
            return expr

        raise DLSError(f"Unexpected token '{token.value}' at position {token.pos}")


def parse_dsl(source: str) -> List[AST]:
    """Parse DSL source into AST."""
    # Preprocess: handle empty lines and comments
    lines = source.split('\n')
    processed_lines = []
    for line in lines:
        # Remove inline comments
        if '#' in line:
            line = line[:line.index('#')]
        processed_lines.append(line)

    processed_source = '\n'.join(processed_lines)

    # Tokenize
    tokenizer = DSLTokenizer(processed_source)
    tokens = tokenizer.tokenize()

    # Parse
    parser = DSLParser(tokens)
    return parser.parse()


# =============================================================================
# Type System
# =============================================================================

class DataType:
    """Represents a data type."""
    STRING = 'string'
    NUMERIC = 'numeric'  # Includes int and float
    TIMESTAMP = 'timestamp'
    BOOLEAN = 'boolean'


def infer_dtype(value) -> str:
    """Infer data type from a Python value."""
    if isinstance(value, bool):
        return DataType.BOOLEAN
    elif isinstance(value, (int, float)):
        return DataType.NUMERIC
    elif isinstance(value, str):
        return DataType.STRING
    elif isinstance(value, (datetime, pd.Timestamp)):
        return DataType.TIMESTAMP
    else:
        return DataType.STRING  # Default fallback


class TypeChecker:
    """Type checker for MTL expressions."""

    def __init__(self, data: pd.DataFrame, known_cols: Dict[str, str]):
        self.data = data
        # known_cols maps column name to data type
        self.cols = dict(known_cols)  # Copy

    def get_col_type(self, col: str) -> str:
        """Get the type of a column."""
        if col not in self.cols:
            raise TypeError(f"Unknown column '{col}'")
        return self.cols[col]

    def add_col(self, name: str, dtype: str):
        """Add a new column with its type."""
        self.cols[name] = dtype

    def check_transform(self, transform: TransformExpr) -> str:
        """Check transform expression and return its output type."""
        if isinstance(transform, LenTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.STRING:
                raise TypeError(f"len() requires string column, got '{col_type}' for '{transform.col}'")
            return DataType.NUMERIC

        elif isinstance(transform, DateTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.TIMESTAMP:
                raise TypeError(f"{transform.func}() requires timestamp column, got '{col_type}' for '{transform.col}'")
            return DataType.NUMERIC

        elif isinstance(transform, RoundTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.NUMERIC:
                raise TypeError(f"round() requires numeric column, got '{col_type}' for '{transform.col}'")
            if transform.digits is not None and not isinstance(transform.digits, int):
                raise TypeError(f"round() digits must be an integer")
            return DataType.NUMERIC

        raise TypeError(f"Unknown transform type")

    def check_bool_expr(self, expr: BoolExpr):
        """Check boolean expression for type correctness."""
        if isinstance(expr, BinOpBool):
            self.check_bool_expr(expr.left)
            self.check_bool_expr(expr.right)

        elif isinstance(expr, NotBool):
            self.check_bool_expr(expr.expr)

        elif isinstance(expr, CompBool):
            # Check types are compatible
            left_type = self._eval_scalar_type(expr.left)
            right_type = self._eval_scalar_type(expr.right)

            # Numeric allows int/float mixing
            if left_type == DataType.NUMERIC and right_type == DataType.NUMERIC:
                return
            if left_type == right_type:
                return
            raise TypeError(f"Incompatible types for comparison: '{left_type}' and '{right_type}'")

        elif isinstance(expr, InBool):
            # left must be string literal, right must be string column
            # Type checker already validated the literal
            col_type = self.get_col_type(expr.col)
            if col_type != DataType.STRING:
                raise TypeError(f"'in' operator requires string column, got '{col_type}' for '{expr.col}'")

        elif isinstance(expr, ParenBool):
            self.check_bool_expr(expr.expr)

        elif isinstance(expr, BoolLiteral):
            pass  # Always valid

    def _eval_scalar_type(self, expr: ScalarExpr) -> str:
        """Evaluate the type of a scalar expression."""
        if isinstance(expr, ColumnRef):
            return self.get_col_type(expr.name)
        elif isinstance(expr, StringLiteral):
            return DataType.STRING
        elif isinstance(expr, NumberLiteral):
            return DataType.NUMERIC
        elif isinstance(expr, BoolLiteral):
            return DataType.BOOLEAN
        raise TypeError(f"Unknown scalar expression type")

    def check_aggregation(self, agg: Aggregation) -> str:
        """Check aggregation and return its output type."""
        valid_fns = {'average', 'median', 'sum', 'count', 'std', 'var', 'min', 'max'}

        if agg.fn not in valid_fns:
            raise TypeError(f"Unknown aggregation function '{agg.fn}'")

        if agg.fn == 'count':
            if agg.col is None:
                return DataType.NUMERIC  # count(*) always numeric
            # count(column) is also numeric
            col_type = self.get_col_type(agg.col)
            if col_type not in (DataType.NUMERIC, DataType.STRING, DataType.TIMESTAMP, DataType.BOOLEAN):
                raise TypeError(f"count() column must have a valid type, got '{col_type}'")
            return DataType.NUMERIC

        # All other aggregations require numeric input
        if agg.col is None:
            raise TypeError(f"{agg.fn}() requires a column argument")

        col_type = self.get_col_type(agg.col)
        if col_type != DataType.NUMERIC:
            raise TypeError(f"{agg.fn}() requires numeric column, got '{col_type}'")

        return DataType.NUMERIC


# =============================================================================
# Data Processor
# =============================================================================

class DataProcessor:
    """Executes the transformation pipeline on data."""

    def __init__(self, data: pd.DataFrame, tz: str = 'UTC'):
        self.data = data.copy()
        self.tz = tz
        self.epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        # Infer types from data
        self.col_types: Dict[str, str] = {}
        for col in data.columns:
            # Try to infer type from first non-null value
            sample = data[col].dropna().iloc[0] if len(data[col].dropna()) > 0 else None
            if sample is not None:
                self.col_types[col] = infer_dtype(sample)
            else:
                # Default to string for all-null columns
                self.col_types[col] = DataType.STRING

    def get_col_type(self, col: str) -> str:
        if col not in self.col_types:
            raise TypeError(f"Unknown column '{col}'")
        return self.col_types[col]

    def add_col(self, name: str, values: pd.Series, dtype: str):
        """Add a new column."""
        self.data[name] = values
        self.col_types[name] = dtype

    def execute_filter(self, stmt: FilterStatement) -> 'DataProcessor':
        """Execute filter statement."""
        mask = self._eval_bool_expr(stmt.expr)
        new_proc = DataProcessor(self.data[mask], self.tz)
        new_proc.col_types = self.col_types.copy()
        return new_proc

    def _eval_bool_expr(self, expr: BoolExpr) -> pd.Series:
        """Evaluate boolean expression to a boolean mask."""
        if isinstance(expr, BinOpBool):
            left = self._eval_bool_expr(expr.left)
            right = self._eval_bool_expr(expr.right)
            if expr.op == '&':
                return left & right
            else:  # '|'
                return left | right

        elif isinstance(expr, NotBool):
            return ~self._eval_bool_expr(expr.expr)

        elif isinstance(expr, CompBool):
            left_vals = self._eval_scalar(expr.left)
            right_vals = self._eval_scalar(expr.right)

            # Handle numeric comparisons (allow int/float mixing)
            left_type = self._infer_series_type(left_vals)
            right_type = self._infer_series_type(right_vals)

            if left_type == DataType.NUMERIC and right_type == DataType.NUMERIC:
                # Convert both to float for comparison
                left_f = pd.to_numeric(left_vals, errors='coerce')
                right_f = pd.to_numeric(right_vals, errors='coerce')

                if expr.op == '<':
                    return left_f < right_f
                elif expr.op == '<=':
                    return left_f <= right_f
                elif expr.op == '>':
                    return left_f > right_f
                elif expr.op == '>=':
                    return left_f >= right_f
                elif expr.op == '==':
                    return left_f == right_f
                elif expr.op == '!=':
                    return left_f != right_f

            elif left_type == right_type:
                if expr.op == '<':
                    return left_vals < right_vals
                elif expr.op == '<=':
                    return left_vals <= right_vals
                elif expr.op == '>':
                    return left_vals > right_vals
                elif expr.op == '>=':
                    return left_vals >= right_vals
                elif expr.op == '==':
                    return left_vals == right_vals
                elif expr.op == '!=':
                    return left_vals != right_vals

            raise TypeError(f"Incompatible types for comparison: '{left_type}' and '{right_type}'")

        elif isinstance(expr, InBool):
            col_series = self.data[expr.col]
            # Check if substring exists in each value
            return col_series.astype(str).str.contains(expr.literal, regex=False, na=False)

        elif isinstance(expr, ParenBool):
            return self._eval_bool_expr(expr.expr)

        elif isinstance(expr, BoolLiteral):
            return pd.Series([expr.value] * len(self.data), index=self.data.index)

        raise TypeError(f"Unknown boolean expression type")

    def _eval_scalar(self, expr: ScalarExpr) -> pd.Series:
        """Evaluate scalar expression to a Series."""
        if isinstance(expr, ColumnRef):
            return self.data[expr.name]
        elif isinstance(expr, StringLiteral):
            return pd.Series([expr.value] * len(self.data), index=self.data.index)
        elif isinstance(expr, NumberLiteral):
            return pd.Series([expr.value] * len(self.data), index=self.data.index)
        elif isinstance(expr, BoolLiteral):
            return pd.Series([expr.value] * len(self.data), index=self.data.index)

        raise TypeError(f"Unknown scalar expression")

    def _infer_series_type(self, series: pd.Series) -> str:
        """Infer the type of a pandas Series."""
        # Try numeric first
        if pd.api.types.is_numeric_dtype(series):
            return DataType.NUMERIC
        # Then boolean
        elif pd.api.types.is_bool_dtype(series):
            return DataType.BOOLEAN
        # Default to string
        return DataType.STRING

    def execute_apply(self, stmt: ApplyStatement) -> 'DataProcessor':
        """Execute apply statement."""
        transform = stmt.transform_expr
        result_type = self._check_transform_type(transform)
        values = self._compute_transform(transform)

        new_proc = DataProcessor(self.data, self.tz)
        new_proc.col_types = self.col_types.copy()
        new_proc.add_col(stmt.new_col, values, result_type)
        return new_proc

    def _check_transform_type(self, transform: TransformExpr) -> str:
        """Check transform type compatibility."""
        if isinstance(transform, LenTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.STRING:
                raise TypeError(f"len() requires string column, got '{col_type}'")
            return DataType.NUMERIC

        elif isinstance(transform, DateTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.TIMESTAMP:
                raise TypeError(f"{transform.func}() requires timestamp column, got '{col_type}'")
            return DataType.NUMERIC

        elif isinstance(transform, RoundTransform):
            col_type = self.get_col_type(transform.col)
            if col_type != DataType.NUMERIC:
                raise TypeError(f"round() requires numeric column, got '{col_type}'")
            if transform.digits is not None and not isinstance(transform.digits, int):
                raise TypeError(f"round() digits must be an integer")
            return DataType.NUMERIC

        raise TypeError(f"Unknown transform type")

    def _compute_transform(self, transform: TransformExpr) -> pd.Series:
        """Compute transform values."""
        if isinstance(transform, LenTransform):
            col = self.data[transform.col]
            return col.astype(str).str.len()

        elif isinstance(transform, DateTransform):
            col = self.data[transform.col]
            # Ensure datetime
            dt_col = pd.to_datetime(col, utc=True)

            if transform.func == 'day':
                return dt_col.dt.day
            elif transform.func == 'month':
                return dt_col.dt.month
            elif transform.func == 'year':
                return dt_col.dt.year

        elif isinstance(transform, RoundTransform):
            col = pd.to_numeric(self.data[transform.col], errors='coerce')
            digits = transform.digits if transform.digits is not None else 0

            def round_half_away(x):
                if pd.isna(x):
                    return x
                if digits == 0:
                    # Round half away from zero
                    return int(math.copysign(math.floor(abs(x) + 0.5), x))
                else:
                    # For other digits, use decimal
                    scale = 10 ** digits
                    return round(x * scale) / scale

            return col.apply(round_half_away)

        raise TypeError(f"Unknown transform type")

    def execute_window(self, stmt: WindowStatement) -> 'DataProcessor':
        """Execute window statement - partitions data into tumbling windows."""
        # Parse duration
        match = re.match(r'^(\d+)(m|h|d)$', stmt.duration)
        if not match:
            raise TypeError(f"Invalid duration literal '{stmt.duration}'")

        amount = int(match.group(1))
        unit = match.group(2)

        # Convert to minutes
        if unit == 'm':
            window_minutes = amount
        elif unit == 'h':
            window_minutes = amount * 60
        elif unit == 'd':
            window_minutes = amount * 24 * 60
        else:
            raise TypeError(f"Unknown unit '{unit}'")

        window_delta = timedelta(minutes=window_minutes)

        # Get timestamp column and ensure it's datetime
        if 'timestamp' not in self.data.columns:
            raise TypeError("No timestamp column found")

        timestamps = pd.to_datetime(self.data['timestamp'], utc=True)

        # Calculate window start for each row (aligned to Unix epoch)
        # window_start = epoch + floor((ts - epoch) / window_size) * window_size
        epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        # Convert to total minutes since epoch
        minutes_since_epoch = (timestamps - epoch).total_seconds() / 60
        window_index = np.floor(minutes_since_epoch / window_minutes).astype(int)

        # Calculate window start times
        window_starts = epoch + pd.to_timedelta(window_index * window_minutes, unit='m')
        window_ends = window_starts + pd.to_timedelta(window_minutes, unit='m')

        # Store window info in the processor
        self.window_starts = window_starts
        self.window_ends = window_ends
        self.window_indices = window_index
        self.window_duration = stmt.duration

        return self

    def execute_group_by(self, stmt: GroupByStatement) -> 'DataProcessor':
        """Execute group_by statement."""
        # Validate columns exist
        for col in stmt.columns:
            if col not in self.data.columns:
                raise TypeError(f"Group-by column '{col}' not found")

        self.group_cols = stmt.columns
        return self

    def execute_aggregate(self, stmt: AggregateStatement) -> List[Dict[str, Any]]:
        """Execute aggregate statement and return results."""
        results = []

        # Determine grouping keys
        group_keys = getattr(self, 'group_cols', None)
        has_window = hasattr(self, 'window_indices')

        # Get numeric columns for aggregation validation
        numeric_cols = {col for col, dtype in self.col_types.items()
                       if dtype == DataType.NUMERIC}

        # Validate aggregations
        for agg in stmt.aggregations:
            if agg.fn != 'count' and agg.col and agg.col not in numeric_cols:
                raise TypeError(f"Aggregate {agg.fn}({agg.col}) requires numeric column")

        # Prepare aggregation functions
        agg_funcs = []
        agg_names = []
        agg_cols = []

        for agg in stmt.aggregations:
            agg_names.append(agg.alias)
            agg_cols.append(agg.col)

            if agg.fn == 'sum':
                agg_funcs.append('sum')
            elif agg.fn == 'average':
                agg_funcs.append('mean')
            elif agg.fn == 'median':
                agg_funcs.append('median')
            elif agg.fn == 'min':
                agg_funcs.append('min')
            elif agg.fn == 'max':
                agg_funcs.append('max')
            elif agg.fn == 'std':
                agg_funcs.append(lambda x: x.std(ddof=0))  # Population std
            elif agg.fn == 'var':
                agg_funcs.append(lambda x: x.var(ddof=0))  # Population var
            elif agg.fn == 'count':
                if agg.col is None:
                    agg_funcs.append('count')
                else:
                    agg_funcs.append(lambda x: x.count())
            else:
                raise TypeError(f"Unknown aggregate function '{agg.fn}'")

        # Perform grouping and aggregation
        if group_keys or has_window:
            # Create group keys
            if has_window:
                # Add window columns to dataframe for grouping
                self.data['_window_start'] = self.window_starts
                self.data['_window_end'] = self.window_ends
                group_cols_for_groupby = ['_window_start', '_window_end']
                if group_keys:
                    group_cols_for_groupby.extend(group_keys)
            else:
                group_cols_for_groupby = group_keys if group_keys else []

            # Group and aggregate
            if not group_cols_for_groupby:
                # Single group (no group_by, but might have window)
                grouped = self.data.apply(
                    lambda col: pd.Series({
                        name: func(col) if callable(func) else col.agg(func)
                        for name, func in zip(agg_names, agg_funcs)
                    }), axis=0
                ).T

                # Convert to dict
                if len(grouped) > 0:
                    row = grouped.iloc[0].to_dict()
                    if has_window:
                        row['window_start'] = self.window_starts.iloc[0]
                        row['window_end'] = self.window_ends.iloc[0]
                    results.append(row)
            else:
                grouped = self.data.groupby(group_cols_for_groupby, sort=False)

                for name, group in grouped:
                    agg_result = {}

                    # Add group keys
                    if has_window:
                        if group_keys:
                            ws, we, *gk = name
                            agg_result['window_start'] = ws
                            agg_result['window_end'] = we
                            for i, key in enumerate(gk):
                                agg_result[key] = key
                        else:
                            ws, we = name
                            agg_result['window_start'] = ws
                            agg_result['window_end'] = we
                    if group_keys and not has_window:
                        for i, key in enumerate(name):
                            agg_result[group_keys[i]] = key

                    # Compute aggregations
                    for agg_name, agg_col, agg_func in zip(agg_names, agg_cols, agg_funcs):
                        if agg_col is None:  # count(*)
                            value = len(group)
                        else:
                            series = pd.to_numeric(group[agg_col], errors='coerce')
                            if callable(agg_func):
                                try:
                                    value = agg_func(series)
                                except Exception as e:
                                    raise RuntimeError(f"Error computing {agg_name}: {str(e)}")
                            else:
                                try:
                                    value = series.agg(agg_func)
                                except Exception as e:
                                    raise RuntimeError(f"Error computing {agg_name}: {str(e)}")

                            # Check for NaN/Inf
                            if pd.isna(value):
                                value = None
                            elif math.isinf(value):
                                raise RuntimeError(f"Aggregate {agg_name} produced infinity")

                        agg_result[agg_name] = value

                    results.append(agg_result)
        else:
            # No grouping, single result
            result = {}
            for agg_name, agg_col, agg_func in zip(agg_names, agg_cols, agg_funcs):
                if agg_col is None:  # count(*)
                    value = len(self.data)
                else:
                    series = pd.to_numeric(self.data[agg_col], errors='coerce')
                    if callable(agg_func):
                        try:
                            value = agg_func(series)
                        except Exception as e:
                            raise RuntimeError(f"Error computing {agg_name}: {str(e)}")
                    else:
                        try:
                            value = series.agg(agg_func)
                        except Exception as e:
                            raise RuntimeError(f"Error computing {agg_name}: {str(e)}")

                    # Check for NaN/Inf
                    if pd.isna(value):
                        value = None
                    elif math.isinf(value):
                        raise RuntimeError(f"Aggregate {agg_name} produced infinity")

                result[agg_name] = value
            results.append(result)

        return results


# =============================================================================
# Main CLI
# =============================================================================

def load_csv(path: str) -> pd.DataFrame:
    """Load CSV file."""
    try:
        return pd.read_csv(path)
    except FileNotFoundError:
        raise IOError(f"CSV file not found: {path}")
    except Exception as e:
        raise IOError(f"Error reading CSV file {path}: {str(e)}")


def load_dsl(path: str) -> str:
    """Load DSL file or stdin."""
    if path == '-':
        return sys.stdin.read()
    try:
        with open(path, 'r') as f:
            return f.read()
    except FileNotFoundError:
        raise IOError(f"DSL file not found: {path}")
    except Exception as e:
        raise IOError(f"Error reading DSL file {path}: {str(e)}")


def parse_args():
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(description='MTL - Mini Transformation Language')
    parser.add_argument('--csv', required=True, help='Path to input CSV file')
    parser.add_argument('--dsl', required=True, help='Path to DSL file (use - for stdin)')
    parser.add_argument('--out', help='Output file (default: stdout)')
    parser.add_argument('--tz', default='UTC', help='Default timezone (default: UTC)')
    parser.add_argument('--timestamp-format', help='Timestamp format string')

    return parser.parse_args()


def normalize_output(results: List[Dict], group_cols: List[str],
                     has_window: bool, agg_aliases: List[str]) -> List[Dict]:
    """Normalize output according to specification."""

    def sort_key(item):
        keys = []
        # Primary: window_start
        if has_window:
            ws = item.get('window_start')
            keys.append(ws if ws is not None else datetime.min)
        # Secondary: group_by values
        for col in group_cols:
            val = item.get(col)
            keys.append(val if val is not None else '')
        return tuple(keys)

    # Sort results
    results.sort(key=sort_key)

    # Reorder keys in each result
    normalized = []
    for result in results:
        ordered = {}

        # Window keys first
        if has_window:
            ordered['window_start'] = result.get('window_start')
            ordered['window_end'] = result.get('window_end')

        # Group by keys in order
        for col in group_cols:
            ordered[col] = result.get(col)

        # Aggregate aliases in lexicographic order
        for alias in sorted(agg_aliases):
            ordered[alias] = result.get(alias)

        normalized.append(ordered)

    return normalized


def format_timestamp(dt) -> str:
    """Format datetime as ISO 8601 with Z suffix."""
    if dt is None:
        return None
    if isinstance(dt, str):
        # Already a string, ensure it has Z
        if 'Z' not in dt and '+' not in dt:
            dt += 'Z'
        return dt
    if pd.isna(dt):
        return None
    # Ensure UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # Format as ISO with Z
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def main():
    """Main entry point."""
    args = parse_args()

    try:
        # Load data
        data = load_csv(args.csv)

        # Parse timestamps
        if args.timestamp_format:
            # Parse with custom format
            data['timestamp'] = pd.to_datetime(data['timestamp'], format=args.timestamp_format, utc=True)
        else:
            # Parse ISO 8601
            data['timestamp'] = pd.to_datetime(data['timestamp'], utc=True)

        # Load and parse DSL
        dsl_source = load_dsl(args.dsl)
        statements = parse_dsl(dsl_source)

        # Validate DSL structure
        has_aggregate = False
        has_group_by = False
        has_window = False

        for stmt in statements:
            if isinstance(stmt, AggregateStatement):
                if has_aggregate:
                    raise DLSError("Multiple aggregate statements not allowed")
                has_aggregate = True
            elif isinstance(stmt, GroupByStatement):
                if has_group_by:
                    raise DLSError("Multiple group_by statements not allowed")
                has_group_by = True
            elif isinstance(stmt, WindowStatement):
                if has_window:
                    raise DLSError("Multiple window statements not allowed")
                has_window = True

        if not has_aggregate:
            raise DLSError("Exactly one aggregate statement is required")

        # Initialize processor
        processor = DataProcessor(data, args.tz)

        # Re-infer types after timestamp parsing
        processor.col_types['timestamp'] = DataType.TIMESTAMP

        # Execute pipeline
        group_cols = []
        duration = None
        aggregate_stmt = None

        for stmt in statements:
            if isinstance(stmt, FilterStatement):
                # Type check
                type_checker = TypeChecker(processor.data, processor.col_types)
                type_checker.check_bool_expr(stmt.expr)
                processor = processor.execute_filter(stmt)

            elif isinstance(stmt, ApplyStatement):
                # Type check
                result_type = processor._check_transform_type(stmt.transform_expr)
                processor = processor.execute_apply(stmt)

            elif isinstance(stmt, GroupByStatement):
                group_cols = stmt.columns
                processor = processor.execute_group_by(stmt)

            elif isinstance(stmt, WindowStatement):
                duration = stmt.duration
                processor = processor.execute_window(stmt)

            elif isinstance(stmt, AggregateStatement):
                aggregate_stmt = stmt

        # Execute aggregation
        results = processor.execute_aggregate(aggregate_stmt)

        # Collect aggregate aliases
        agg_aliases = [agg.alias for agg in aggregate_stmt.aggregations]

        # Normalize output
        normalized = normalize_output(results, group_cols, has_window, agg_aliases)

        # Format timestamps in output
        for result in normalized:
            if 'window_start' in result:
                result['window_start'] = format_timestamp(result['window_start'])
            if 'window_end' in result:
                result['window_end'] = format_timestamp(result['window_end'])

        # Write output
        output_json = json.dumps(normalized, indent=2) if len(normalized) > 1 else json.dumps(normalized[0] if normalized else {})

        if args.out:
            with open(args.out, 'w') as f:
                f.write(output_json + '\n')
        else:
            print(output_json)

        return 0

    except DLSError as e:
        print(e, file=sys.stderr)
        return 1
    except TypeError as e:
        print(e, file=sys.stderr)
        return 2
    except IOError as e:
        print(e, file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 4
    except Exception as e:
        # General error
        print(f"ERROR:unknown:{str(e)}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
