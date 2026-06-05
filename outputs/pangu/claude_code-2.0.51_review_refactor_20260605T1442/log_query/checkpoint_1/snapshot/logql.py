#!/usr/bin/env python3
"""
LogQL - Part 1: Parsing, Filtering, Projection
A command-line program that reads NDJSON logs and executes SQL-like queries.
"""

import sys
import json
import re
import argparse
from enum import Enum
from typing import Any, Optional


class TokenType(Enum):
    SELECT = "SELECT"
    FROM = "FROM"
    WHERE = "WHERE"
    AND = "AND"
    OR = "OR"
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
    __slots__ = ('type', 'value', 'line', 'column')

    def __init__(self, type: TokenType, value: str, line: int, column: int):
        self.type = type
        self.value = value
        self.line = line
        self.column = column

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
        'true': TokenType.TRUE,
        'false': TokenType.FALSE,
        'null': TokenType.NULL,
    }

    def __init__(self, query: str):
        self.query = query
        self.pos = 0
        self.line = 1
        self.column = 1
        self.tokens = []

    def _peek(self) -> Optional[str]:
        if self.pos < len(self.query):
            return self.query[self.pos]
        return None

    def _advance(self) -> str:
        char = self.query[self.pos]
        self.pos += 1
        if char == '\n':
            self.line += 1
            self.column = 1
        else:
            self.column += 1
        return char

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.query):
            char = self._peek()

            # Skip whitespace
            if char in ' \t':
                self._advance()
                continue
            if char == '\n':
                self._advance()
                continue
            if char == '\r':
                self._advance()
                continue

            # Check for two-character operators
            if self.pos + 1 < len(self.query):
                two_char = self.query[self.pos:self.pos + 2]
                if two_char == '<=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.LTE, '<=', self.line, self.column))
                    continue
                if two_char == '>=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.GTE, '>=', self.line, self.column))
                    continue
                if two_char == '!=':
                    self._advance()
                    self._advance()
                    self.tokens.append(Token(TokenType.NE, '!=', self.line, self.column))
                    continue

            # Check for single-character tokens
            if char == '*':
                self._advance()
                self.tokens.append(Token(TokenType.STAR, '*', self.line, self.column))
                continue
            if char == ',':
                self._advance()
                self.tokens.append(Token(TokenType.COMMA, ',', self.line, self.column))
                continue
            if char == '(':
                self._advance()
                self.tokens.append(Token(TokenType.LPAREN, '(', self.line, self.column))
                continue
            if char == ')':
                self._advance()
                self.tokens.append(Token(TokenType.RPAREN, ')', self.line, self.column))
                continue
            if char == '<':
                self._advance()
                self.tokens.append(Token(TokenType.LT, '<', self.line, self.column))
                continue
            if char == '>':
                self._advance()
                self.tokens.append(Token(TokenType.GT, '>', self.line, self.column))
                continue
            if char == '=':
                self._advance()
                self.tokens.append(Token(TokenType.EQ, '=', self.line, self.column))
                continue
            if char == '.':
                self._advance()
                self.tokens.append(Token(TokenType.DOT, '.', self.line, self.column))
                continue
            if char == '!':
                self._advance()
                raise SyntaxError(f"Unexpected character '!' at line {self.line}, column {self.column}")

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
                self.tokens.append(Token(TokenType.STRING, ''.join(value_chars), self.line, self.column))
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
                self.tokens.append(Token(TokenType.NUMBER, num_str, self.line, self.column))
                continue

            # Check for identifier/keyword
            if char.isalpha() or char == '_':
                start = self.pos
                while self.pos < len(self.query) and (self.query[self.pos].isalnum() or self.query[self.pos] == '_'):
                    self._advance()
                ident = self.query[start:self.pos]
                ident_upper = ident.upper()
                token_type = self.KEYWORDS.get(ident_upper, TokenType.IDENT)
                self.tokens.append(Token(token_type, ident, self.line, self.column))
                continue

            # Invalid character
            raise SyntaxError(f"Unexpected character '{char}' at line {self.line}, column {self.column}")

        self.tokens.append(Token(TokenType.EOF, '', self.line, self.column))
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


class ColumnList(ASTNode):
    __slots__ = ('columns', 'is_star')

    def __init__(self, columns: list, is_star: bool = False):
        self.columns = columns
        self.is_star = is_star


class Query(ASTNode):
    __slots__ = ('columns', 'table', 'where')

    def __init__(self, columns, table: str, where=None):
        self.columns = columns
        self.table = table
        self.where = where


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
        # SELECT <select_list> FROM logs [WHERE <boolean_expr>]
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

        self._eat(TokenType.EOF, '')
        return Query(columns, 'logs', where)

    def _parse_select_list(self):
        """Parse SELECT * or SELECT field, field, ..."""
        if self._peek_type() == TokenType.STAR:
            self._eat(TokenType.STAR)
            return ColumnList([], is_star=True)

        columns = []
        seen = set()
        while True:
            field_ref = self._parse_field_ref()
            key = '.'.join(field_ref.parts)
            if key in seen:
                raise SyntaxError(f"Duplicate field reference: {key}")
            seen.add(key)
            columns.append(field_ref)

            if self._peek_type() == TokenType.COMMA:
                self._eat(TokenType.COMMA)
                if self._peek_type() in (TokenType.EOF, TokenType.WHERE):
                    raise SyntaxError("Unexpected comma at end of select list")
                continue
            break

        return ColumnList(columns)

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
        results = []
        for record in self.records:
            if query.where is None or Evaluator.evaluate(query.where, record):
                projected = Projection.project(record, query.columns)
                results.append(projected)

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
