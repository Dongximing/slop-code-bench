#!/usr/bin/env python3
"""
LogQL Part 1 - A command-line program that reads NDJSON logs,
parses a tiny SQL-like query, filters rows with boolean logic,
and returns only the selected fields as JSON.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from typing import Any, List, Optional, Union


# =============================================================================
# Token Types
# =============================================================================

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


# =============================================================================
# Lexer
# =============================================================================

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


# =============================================================================
# AST Nodes
# =============================================================================

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
class SelectClause:
    star: bool
    fields: List[FieldRef]


@dataclass
class Query:
    select: SelectClause
    where: Optional[BooleanExpr]


# =============================================================================
# Parser
# =============================================================================

class ParseError(Exception):
    pass


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

        self.expect(TokenType.EOF)

        return Query(select, where)

    def parse_select_list(self) -> SelectClause:
        if self.match(TokenType.STAR):
            self.advance()
            return SelectClause(star=True, fields=[])

        fields = []
        field = self.parse_field_ref()
        fields.append(field)

        while self.match(TokenType.COMMA):
            self.advance()
            field = self.parse_field_ref()

            # Check for duplicates
            field_str = field.to_string()
            for existing in fields:
                if existing.to_string() == field_str:
                    raise ParseError(f"Duplicate field '{field_str}' in select list")

            fields.append(field)

        return SelectClause(star=False, fields=fields)

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
        # Parse OR expressions (lowest precedence)
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

        # Parse operator
        if self.match(TokenType.EQ, TokenType.NE, TokenType.LT,
                      TokenType.LE, TokenType.GT, TokenType.GE):
            op_token = self.advance()
            op = op_token.value
        else:
            raise ParseError(f"Expected comparison operator at position {self.current().pos}")

        # Parse literal
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


# =============================================================================
# Evaluator
# =============================================================================

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
    for field in select.fields:
        key = field.to_string()
        value = get_field_value(record, field)
        result[key] = value

    return result


# =============================================================================
# Error Output
# =============================================================================

def output_error(error_type: str, message: str):
    """Output a structured error to stderr and exit."""
    error_obj = {
        "error": error_type,
        "message": message
    }
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


# =============================================================================
# Main
# =============================================================================

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
    results = []

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

                # Project
                projected = project_record(record, query.select)
                results.append(projected)

    except FileNotFoundError:
        output_error("file_error", f"Log file not found: {log_file}")
    except IOError as e:
        output_error("file_error", f"Error reading log file: {e}")

    return results


def main():
    parser = argparse.ArgumentParser(description='LogQL Part 1 - Query NDJSON logs')
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
