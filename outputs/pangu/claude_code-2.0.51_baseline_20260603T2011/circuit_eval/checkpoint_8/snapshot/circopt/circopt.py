#!/usr/bin/env python3
"""
Circuit optimizer and validator for .circ files.
Supports vectors, slicing, concatenation, rich literals, and width checking.
"""

import argparse
import json
import sys
import re
from enum import Enum
from typing import Optional
from dataclasses import dataclass


# =============================================================================
# Error classes
# =============================================================================

class CircoptError(Exception):
    """Base exception for all circopt errors."""
    exit_code: int

    def to_json(self, command: str, file: Optional[str] = None,
                line: Optional[int] = None, col: Optional[int] = None) -> dict:
        return {
            "ok": False,
            "command": command,
            "exit_code": self.exit_code,
            "error": {
                "type": self.__class__.__name__,
                "message": str(self),
                "file": getattr(self, 'file', None) if getattr(self, 'file', None) is not None else file,
                "line": getattr(self, 'line', None) if getattr(self, 'line', None) is not None else line,
                "col": getattr(self, 'col', None) if getattr(self, 'col', None) is not None else col
            }
        }


class CliUsageError(CircoptError):
    exit_code = 1


class CircParseError(CircoptError):
    exit_code = 2

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.file = file
        self.line = line
        self.col = col


class DeclarationAfterAssignmentError(CircoptError):
    exit_code = 3


class DuplicateNameError(CircoptError):
    exit_code = 3


class UndefinedNameError(CircoptError):
    exit_code = 3


class UnassignedSignalError(CircoptError):
    exit_code = 3


class InputAssignmentError(CircoptError):
    exit_code = 3


class MissingInputError(CircoptError):
    exit_code = 1


class UnknownInputError(CircoptError):
    exit_code = 1


class InputValueParseError(CircoptError):
    exit_code = 2


class MultipleAssignmentError(CircoptError):
    exit_code = 3


class ArityError(CircoptError):
    exit_code = 3


class CycleError(CircoptError):
    exit_code = 3

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        path = " -> ".join(cycle)
        super().__init__(f"Cycle detected: {path}")


class WidthMismatchError(CircoptError):
    exit_code = 3

    def __init__(self, op: str, width1: int, width2: int):
        super().__init__(f"Width mismatch for {op}: operand widths {width1} and {width2} do not match")


class IndexOutOfBoundsError(CircoptError):
    exit_code = 3

    def __init__(self, signal: str, index: int, width: int):
        super().__init__(f"Index {index} out of bounds for signal '{signal}' (width {width})")


class InputWidthMismatchError(CircoptError):
    exit_code = 3

    def __init__(self, name: str, declared: int, provided: int):
        super().__init__(f"Width mismatch for input '{name}': declared width {declared}, provided width {provided}")


class RadixNotAllowedIn3ValError(CircoptError):
    exit_code = 1

    def __init__(self):
        super().__init__("--radix hex or --radix dec not allowed with --mode 3val")


class UnknownOutputFormatError(CircoptError):
    exit_code = 1

    def __init__(self, format: str):
        super().__init__(f"Unknown output format: '{format}'")


class UnsupportedFeatureError(CircoptError):
    exit_code = 1

    def __init__(self, feature: str):
        super().__init__(f"Unsupported feature: '{feature}'")


class InputLimitExceeded(CircoptError):
    exit_code = 1

    def __init__(self, total_bits: int, limit: int):
        super().__init__(f"Input bit limit exceeded: {total_bits} bits > {limit}")


class PortMismatchError(CircoptError):
    exit_code = 3

    def __init__(self, message: str):
        super().__init__(message)


class UnknownInputFormatError(CircoptError):
    exit_code = 1

    def __init__(self, format: str):
        super().__init__(f"Unknown input format: '{format}'")


class JsonParseError(CircoptError):
    exit_code = 2

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.file = file
        self.line = line
        self.col = col


class JsonSchemaError(CircoptError):
    exit_code = 3

    def __init__(self, message: str, file: Optional[str] = None):
        super().__init__(message)
        self.file = file


class BenchParseError(CircoptError):
    exit_code = 2

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.file = file
        self.line = line
        self.col = col


class RedefinitionError(CircoptError):
    exit_code = 3

    def __init__(self, name: str):
        super().__init__(f"Signal '{name}' redefined")


class EquivalenceFailed(CircoptError):
    exit_code = 10

    def __init__(self, counterexample: dict):
        self.counterexample = counterexample
        super().__init__("Circuits are not equivalent")


# =============================================================================
# Tokenizer - handles new token types for vectors and literals
# =============================================================================

class TokenType(Enum):
    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["      # For [msb:lsb] in declarations and [index] in expressions
    RBRACKET = "]"
    COLON = ":"         # For slice [hi:lo]
    LCURLY = "{"        # Start of concatenation
    RCURLY = "}"        # End of concatenation
    COMMA = ","
    EQUALS = "="
    NEWLINE = "NEWLINE"
    EOF = "EOF"


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int


class Tokenizer:
    """Tokenizer for .circ files."""

    def __init__(self, text: str, filename: str):
        self.text = text
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: list[Token] = []

    def tokenize(self) -> list[Token]:
        while self.pos < len(self.text):
            ch = self.text[self.pos]

            if ch.isspace():
                if ch == '\n':
                    self._add_token(TokenType.NEWLINE, '\n', self.line, self.col)
                    self.line += 1
                    self.col = 1
                    self.pos += 1
                else:
                    self.col += 1
                    self.pos += 1
            elif ch == '#':
                # Skip comment until end of line
                while self.pos < len(self.text) and self.text[self.pos] != '\n':
                    self.pos += 1
                    self.col += 1
            elif ch.isalpha() or ch == '_':
                start_pos = self.pos
                while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == '_'):
                    self.pos += 1
                    self.col += 1
                value = self.text[start_pos:self.pos]
                self._add_token(TokenType.IDENTIFIER, value, self.line, self.col)
            elif ch.isdigit() or (ch == '0' and self.pos + 1 < len(self.text) and self.text[self.pos + 1] in ('b', 'x', 'B', 'X')):
                # Number or sized literal (0b, 0x) or base literal (N'b, N'h, N'd)
                start_pos = self.pos

                # Check for 0b or 0x prefix (unsized binary/hex)
                if self.text[self.pos] == '0' and self.pos + 1 < len(self.text):
                    next_ch = self.text[self.pos + 1]
                    if next_ch.lower() == 'b':
                        # 0b... literal (can contain X/x for 3-valued)
                        self.pos += 2
                        self.col += 2
                        # Consume binary digits (including X/x for 3-valued)
                        while self.pos < len(self.text) and self.text[self.pos] in ('0', '1', 'X', 'x', '_'):
                            if self.text[self.pos] != '_':
                                self.col += 1
                            self.pos += 1
                        value = self.text[start_pos:self.pos]
                        self._add_token(TokenType.IDENTIFIER, value, self.line, start_pos + 1)
                        continue
                    elif next_ch.lower() == 'x':
                        # 0x... literal
                        self.pos += 2
                        self.col += 2
                        while self.pos < len(self.text) and self.text[self.pos] in ('0', '1', '2', '3', '4', '5', '6', '7', '8', '9', 'a', 'b', 'c', 'd', 'e', 'f', 'A', 'B', 'C', 'D', 'E', 'F', '_'):
                            if self.text[self.pos] != '_':
                                self.col += 1
                            self.pos += 1
                        value = self.text[start_pos:self.pos]
                        self._add_token(TokenType.IDENTIFIER, value, self.line, start_pos + 1)
                        continue

                # Check for sized literal format: N'b..., N'h..., N'd...
                sized_match = re.match(r'^(\d+)\'([bh])(.*)', self.text[self.pos:])
                if sized_match:
                    suffix = sized_match.group(2).lower()
                    # For binary, allow X/x for 3-valued logic
                    if suffix == 'b':
                        # Consume the literal (will be validated later during evaluation)
                        self.pos += len(sized_match.group(0))
                        value = self.text[start_pos:self.pos]
                        self._add_token(TokenType.IDENTIFIER, value, self.line, start_pos + 1)
                        continue
                    else:
                        # For hex and decimal, only accept valid digits
                        digits_str = sized_match.group(3)
                        # Count valid digits (including underscores)
                        self.pos += len(sized_match.group(0))
                        value = self.text[start_pos:self.pos]
                        self._add_token(TokenType.IDENTIFIER, value, self.line, start_pos + 1)
                        continue

                # Regular decimal number
                while self.pos < len(self.text) and (self.text[self.pos].isdigit() or self.text[self.pos] == '_'):
                    if self.text[self.pos] != '_':
                        self.col += 1
                    self.pos += 1
                value = self.text[start_pos:self.pos]
                self._add_token(TokenType.NUMBER, value, self.line, start_pos + 1)

            elif ch == '(':
                self._add_token(TokenType.LPAREN, '(', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == ')':
                self._add_token(TokenType.RPAREN, ')', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == '[':
                self._add_token(TokenType.LBRACKET, '[', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == ']':
                self._add_token(TokenType.RBRACKET, ']', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == ':':
                self._add_token(TokenType.COLON, ':', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == '{':
                self._add_token(TokenType.LCURLY, '{', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == '}':
                self._add_token(TokenType.RCURLY, '}', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == ',':
                self._add_token(TokenType.COMMA, ',', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == '=':
                self._add_token(TokenType.EQUALS, '=', self.line, self.col)
                self.pos += 1
                self.col += 1
            else:
                raise CircParseError(
                    f"Unexpected character '{ch}'",
                    self.filename, self.line, self.col
                )

        self._add_token(TokenType.EOF, '', self.line, self.col)
        return self.tokens

    def _add_token(self, token_type: TokenType, value: str, line: int = None, col: int = None):
        if line is None:
            line = self.line
        if col is None:
            col = self.col
        self.tokens.append(Token(token_type, value, line, col))


# =============================================================================
# Parser - handles all expression forms
# =============================================================================

class Parser:
    """Parser for .circ files."""

    # Supported operators and their arities
    # -1 means >= 2 (variadic)
    # Specific numbers mean exact arity
    OPERATORS = {
        'NOT': 1,
        'BUF': 1,
        'AND': -1,    # >= 2
        'OR': -1,
        'XOR': -1,
        'NAND': -1,
        'NOR': -1,
        'XNOR': -1,
        'MUX': 3,
        'ITE': 3,
        'REDUCE_AND': 1,
        'REDUCE_OR': 1,
        'REDUCE_XOR': 1,
        'EQ': 2,
    }

    def __init__(self, tokens: list[Token], filename: str):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0
        self.assignments_started = False

    def parse(self) -> dict:
        """Parse the circuit file and return declarations and assignments."""
        result = {
            'inputs': {},       # name -> (msb, lsb)
            'outputs': {},      # name -> (msb, lsb)
            'wires': {},        # name -> (msb, lsb)
            'assignments': [],  # list of (lhs, rhs)
        }

        # First pass: parse declarations
        while self.pos < len(self.tokens):
            token = self.peek()
            if token.type == TokenType.EOF:
                break
            elif token.type == TokenType.NEWLINE:
                self.consume()
                continue

            # Check if this is a declaration
            if token.type == TokenType.IDENTIFIER and token.value.upper() in ('INPUT', 'OUTPUT', 'WIRE'):
                decl_type = token.value.upper()
                if self.assignments_started:
                    raise CircParseError(
                        f"Declaration after assignment: {decl_type}",
                        self.filename, token.line, token.col
                    )
                self._parse_declaration(decl_type, result)
            elif token.type == TokenType.NEWLINE:
                self.consume()
            else:
                # Assignment - this starts the assignment phase
                self.assignments_started = True
                self._parse_assignment(result)

        # Validate that all outputs and wires are assigned
        assigned_names = set()
        for lhs, _ in result['assignments']:
            assigned_names.add(lhs)

        all_signals = set(result['inputs'].keys()) | set(result['outputs'].keys()) | set(result['wires'].keys())

        # Check for unassigned outputs and wires
        for name in result['outputs'] | result['wires']:
            if name not in assigned_names:
                raise UnassignedSignalError(f"Signal '{name}' is not assigned")

        # Check for duplicate assignments
        seen_assignments = set()
        for lhs, _ in result['assignments']:
            if lhs in seen_assignments:
                raise MultipleAssignmentError(f"Signal '{lhs}' is assigned multiple times")
            seen_assignments.add(lhs)

        return result

    def _parse_declaration(self, decl_type: str, result: dict):
        """Parse a declaration line."""
        # Consume the keyword
        self.consume()

        # Parse names with optional width
        while self.pos < len(self.tokens):
            token = self.peek()
            if token.type in (TokenType.NEWLINE, TokenType.EOF):
                break
            if token.type != TokenType.IDENTIFIER:
                raise CircParseError(
                    f"Expected identifier, got '{token.value}'",
                    self.filename, token.line, token.col
                )

            name = token.value
            msb, lsb = 0, 0  # Default scalar

            # Check for optional width specification [msb:lsb]
            next_token = self.peek(1) if self.pos + 1 < len(self.tokens) else None
            if next_token and next_token.type == TokenType.LBRACKET:
                # Parse [msb:lsb]
                self.consume()  # consume identifier
                self.consume()  # consume '['
                msb_token = self.consume()
                if msb_token.type != TokenType.NUMBER:
                    raise CircParseError(
                        f"Expected number for msb, got '{msb_token.value}'",
                        self.filename, msb_token.line, msb_token.col
                    )
                msb = int(msb_token.value)

                colon_token = self.consume()
                if colon_token.type != TokenType.COLON:
                    raise CircParseError(
                        f"Expected ':', got '{colon_token.value}'",
                        self.filename, colon_token.line, colon_token.col
                    )

                lsb_token = self.consume()
                if lsb_token.type != TokenType.NUMBER:
                    raise CircParseError(
                        f"Expected number for lsb, got '{lsb_token.value}'",
                        self.filename, lsb_token.line, lsb_token.col
                    )
                lsb = int(lsb_token.value)

                rbracket_token = self.consume()
                if rbracket_token.type != TokenType.RBRACKET:
                    raise CircParseError(
                        f"Expected ']', got '{rbracket_token.value}'",
                        self.filename, rbracket_token.line, rbracket_token.col
                    )
            else:
                # Scalar signal - consume only the identifier
                self.consume()

            # Add to appropriate set
            target_map = {'INPUT': result['inputs'], 'OUTPUT': result['outputs'], 'WIRE': result['wires']}[decl_type]

            # Check for duplicate
            if name in target_map:
                raise DuplicateNameError(f"Duplicate name '{name}'")
            target_map[name] = (msb, lsb)

            # Check for newline to stop
            if self.pos < len(self.tokens) and self.peek().type == TokenType.NEWLINE:
                break

        # Consume newline if present
        if self.pos < len(self.tokens) and self.peek().type == TokenType.NEWLINE:
            self.consume()

    def _parse_assignment(self, result: dict):
        """Parse an assignment line."""
        # Parse LHS
        lhs_token = self.consume()
        if lhs_token.type != TokenType.IDENTIFIER:
            raise CircParseError(
                f"Expected identifier for LHS, got '{lhs_token.value}'",
                self.filename, lhs_token.line, lhs_token.col
            )

        lhs = lhs_token.value

        # Check = sign
        eq_token = self.consume()
        if eq_token.type != TokenType.EQUALS:
            raise CircParseError(
                f"Expected '=', got '{eq_token.value}'",
                self.filename, eq_token.line, eq_token.col
            )

        # Parse RHS (expression)
        rhs = self._parse_expression()

        result['assignments'].append((lhs, rhs))

        # Consume newline
        if self.pos < len(self.tokens) and self.peek().type == TokenType.NEWLINE:
            self.consume()

        return lhs, rhs

    def _parse_expression(self):
        """Parse an expression: identifier, literal, vector access, slice, concatenation, or OP(...)"""
        token = self.consume()

        if token.type == TokenType.LCURLY:
            # Concatenation: {e1, e2, ..., ek}
            args = []
            while True:
                if self.peek().type == TokenType.RCURLY:
                    self.consume()
                    break
                arg = self._parse_expression()
                args.append(arg)
                if self.peek().type == TokenType.COMMA:
                    self.consume()
                elif self.peek().type != TokenType.RCURLY:
                    raise CircParseError(
                        f"Expected ',' or '}}', got '{self.peek().value}'",
                        self.filename, self.peek().line, self.peek().col
                    )
            return ('concatenation', args)

        if token.type == TokenType.IDENTIFIER:
            # Check for forbidden literal X
            if token.value == 'X':
                raise CircParseError(
                    "Forbidden literal 'X' value",
                    self.filename, token.line, token.col
                )

            # Check for base literals (0b, 0x) or sized literals (N'b, N'h, N'd)
            if token.value.startswith('0b') or token.value.startswith('0x'):
                # Parse as literal - will be handled in evaluate step
                return ('literal', token.value)

            if re.match(r'^\d+\'[bh]', token.value, re.IGNORECASE):
                # Sized literal
                return ('literal', token.value)

            # Could be an identifier or numeric literal (0, 1)
            if token.value in ('0', '1'):
                return ('literal', token.value)

            # Check for operator call (identifier followed by parentheses)
            next_token = self.peek() if self.pos < len(self.tokens) else None
            if next_token and next_token.type == TokenType.LPAREN:
                return self._parse_operator_call(token)

            # Check for vector access: v[index]
            next_token = self.peek() if self.pos < len(self.tokens) else None
            if next_token and next_token.type == TokenType.LBRACKET:
                # Parse vector access/slice
                return self._parse_vector_access(token, token.value)

            return ('identifier', token.value)

        elif token.type == TokenType.LPAREN:
            # Parenthesized expression
            expr = self._parse_expression()
            if self.peek().type != TokenType.RPAREN:
                raise CircParseError(
                    f"Expected ')', got '{self.peek().value}'",
                    self.filename, self.peek().line, self.peek().col
                )
            self.consume()
            return expr

        else:
            raise CircParseError(
                f"Expected expression, got '{token.value}'",
                self.filename, token.line, token.col
            )

    def _parse_vector_access(self, name_token: Token, name: str):
        """Parse vector access v[i] or slice v[hi:lo]"""
        self.consume()  # consume '['

        # Parse the index/slice
        index_token = self.consume()
        if index_token.type != TokenType.NUMBER:
            raise CircParseError(
                f"Expected number, got '{index_token.value}'",
                self.filename, index_token.line, index_token.col
            )
        index = int(index_token.value)

        # Check if this is a slice or single bit
        next_token = self.peek() if self.pos < len(self.tokens) else None

        if next_token and next_token.type == TokenType.COLON:
            # This is a slice: v[hi:lo]
            self.consume()  # consume ':'

            lo_token = self.consume()
            if lo_token.type != TokenType.NUMBER:
                raise CircParseError(
                    f"Expected number, got '{lo_token.value}'",
                    self.filename, lo_token.line, lo_token.col
                )
            lo = int(lo_token.value)

            rbracket = self.consume()
            if rbracket.type != TokenType.RBRACKET:
                raise CircParseError(
                    f"Expected ']', got '{rbracket.value}'",
                    self.filename, rbracket.line, rbracket.col
                )

            return ('slice', name, index, lo)
        else:
            # Single bit access: v[i]
            rbracket = self.consume()
            if rbracket.type != TokenType.RBRACKET:
                raise CircParseError(
                    f"Expected ']', got '{rbracket.value}'",
                    self.filename, rbracket.line, rbracket.col
                )

            return ('bit_index', name, index)

    def _parse_operator_call(self, op_token: Token = None):
        """Parse an operator call: OP(arg1, arg2, ...)"""
        if op_token is None:
            op_token = self.consume()

        op_name = op_token.value.upper()

        if op_name not in self.OPERATORS:
            raise CircParseError(
                f"Unknown operator '{op_name}'",
                self.filename, op_token.line, op_token.col
            )

        # Check for opening paren
        paren_token = self.consume()
        if paren_token.type != TokenType.LPAREN:
            raise CircParseError(
                f"Expected '(', got '{paren_token.value}'",
                self.filename, paren_token.line, paren_token.col
            )

        # Parse arguments
        args = []
        while True:
            # Check for closing paren
            if self.peek().type == TokenType.RPAREN:
                self.consume()
                break

            arg = self._parse_expression()
            args.append(arg)

            # Check for comma or closing paren
            next_tok = self.peek()
            if next_tok.type == TokenType.COMMA:
                self.consume()
            elif next_tok.type != TokenType.RPAREN:
                raise CircParseError(
                    f"Expected ',' or ')', got '{next_tok.value}'",
                    self.filename, next_tok.line, next_tok.col
                )

        # Check arity
        expected = self.OPERATORS[op_name]
        if expected == 1 and len(args) != 1:
            raise ArityError(
                f"Operator '{op_name}' requires exactly 1 argument, got {len(args)}"
            )
        if expected == -1 and len(args) < 2:
            raise ArityError(
                f"Operator '{op_name}' requires at least 2 arguments, got {len(args)}"
            )
        if expected != -1 and expected != 1 and len(args) != expected:
            raise ArityError(
                f"Operator '{op_name}' requires exactly {expected} arguments, got {len(args)}"
            )

        return ('call', op_name, args)

    def peek(self, offset: int = 0) -> Token:
        if self.pos + offset >= len(self.tokens):
            # Return EOF token
            return self.tokens[-1]  # Last token is always EOF
        return self.tokens[self.pos + offset]

    def consume(self) -> Token:
        token = self.tokens[self.pos]
        self.pos += 1
        return token


def parse_file(filepath: str, fmt: str = "auto") -> dict:
    """Parse a circuit file and return the parsed result.

    Args:
        filepath: Path to the circuit file
        fmt: Format to use - "auto", "circ", "json", or "bench"

    Returns:
        Parsed circuit as a dictionary
    """
    # Determine format from file extension if not specified
    if fmt == "auto":
        if filepath.endswith('.circ'):
            fmt = "circ"
        elif filepath.endswith('.json'):
            fmt = "json"
        elif filepath.endswith('.bench'):
            fmt = "bench"
        else:
            raise UnknownInputFormatError(filepath)

    if fmt == "circ":
        try:
            with open(filepath, 'r') as f:
                text = f.read()
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {filepath}")

        tokenizer = Tokenizer(text, filepath)
        tokens = tokenizer.tokenize()

        parser = Parser(tokens, filepath)
        return parser.parse()

    elif fmt == "json":
        return parse_json_file(filepath)

    elif fmt == "bench":
        return parse_bench_file(filepath)

    else:
        raise UnknownInputFormatError(fmt)


# =============================================================================
# JSON circuit format parser
# =============================================================================

def parse_json_file(filepath: str) -> dict:
    """Parse a JSON circuit file and return the parsed result."""
    try:
        with open(filepath, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {filepath}")

    # Parse JSON
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise JsonParseError(
            f"Invalid JSON syntax: {e.msg}",
            file=filepath,
            line=e.lineno,
            col=e.colno
        )

    # Validate schema
    if not isinstance(data, dict):
        raise JsonSchemaError("JSON root must be an object", filepath)

    # Check required fields
    required_fields = ['format_version', 'inputs', 'outputs', 'wires', 'assignments']
    for field in required_fields:
        if field not in data:
            raise JsonSchemaError(f"Missing required field: '{field}'", filepath)

    # Validate format_version
    if data['format_version'] != 1:
        raise JsonSchemaError(f"Unsupported format_version: {data['format_version']} (expected 1)", filepath)

    # Helper to validate port objects
    def validate_port(port, idx, context):
        if not isinstance(port, dict):
            raise JsonSchemaError(f"{context} at index {idx} must be an object", filepath)
        if 'name' not in port:
            raise JsonSchemaError(f"{context} at index {idx} missing 'name' field", filepath)
        if 'msb' not in port or 'lsb' not in port:
            raise JsonSchemaError(f"{context} at index {idx} missing 'msb' or 'lsb' field", filepath)
        if not isinstance(port['name'], str):
            raise JsonSchemaError(f"{context} at index {idx} 'name' must be a string", filepath)
        if not isinstance(port['msb'], int) or not isinstance(port['lsb'], int):
            raise JsonSchemaError(f"{context} at index {idx} 'msb' and 'lsb' must be integers", filepath)
        if port['msb'] < port['lsb'] or port['lsb'] < 0:
            raise JsonSchemaError(
                f"{context} at index {idx} has invalid width: msb={port['msb']}, lsb={port['lsb']} (must have msb >= lsb >= 0)",
                filepath
            )
        return port['name'], port['msb'], port['lsb']

    # Parse inputs
    inputs = {}
    if not isinstance(data['inputs'], list):
        raise JsonSchemaError("'inputs' must be a list", filepath)
    for i, port in enumerate(data['inputs']):
        name, msb, lsb = validate_port(port, i, "Input")
        if name in inputs:
            raise RedefinitionError(name)
        inputs[name] = (msb, lsb)

    # Parse outputs
    outputs = {}
    if not isinstance(data['outputs'], list):
        raise JsonSchemaError("'outputs' must be a list", filepath)
    for i, port in enumerate(data['outputs']):
        name, msb, lsb = validate_port(port, i, "Output")
        if name in outputs:
            raise RedefinitionError(name)
        outputs[name] = (msb, lsb)

    # Parse wires
    wires = {}
    if not isinstance(data['wires'], list):
        raise JsonSchemaError("'wires' must be a list", filepath)
    for i, port in enumerate(data['wires']):
        name, msb, lsb = validate_port(port, i, "Wire")
        if name in wires:
            raise RedefinitionError(name)
        wires[name] = (msb, lsb)

    # Check for name collisions across categories
    all_names = set(inputs.keys()) | set(outputs.keys()) | set(wires.keys())
    if len(all_names) < len(inputs) + len(outputs) + len(wires):
        raise JsonSchemaError("Signal name defined in multiple categories", filepath)

    # Parse assignments
    assignments = []
    if not isinstance(data['assignments'], list):
        raise JsonSchemaError("'assignments' must be a list", filepath)

    lhs_names_seen = set()
    for i, assignment in enumerate(data['assignments']):
        if not isinstance(assignment, dict):
            raise JsonSchemaError(f"Assignment at index {i} must be an object", filepath)
        if 'lhs' not in assignment:
            raise JsonSchemaError(f"Assignment at index {i} missing 'lhs' field", filepath)
        if 'rhs' not in assignment:
            raise JsonSchemaError(f"Assignment at index {i} missing 'rhs' field", filepath)
        if not isinstance(assignment['lhs'], str):
            raise JsonSchemaError(f"Assignment at index {i} 'lhs' must be a string", filepath)
        if not isinstance(assignment['rhs'], str):
            raise JsonSchemaError(f"Assignment at index {i} 'rhs' must be a string", filepath)

        lhs = assignment['lhs']
        rhs = assignment['rhs']

        # Check for duplicate LHS
        if lhs in lhs_names_seen:
            raise RedefinitionError(lhs)
        lhs_names_seen.add(lhs)

        assignments.append((lhs, rhs))

    # Validate that every declared wire and output appears exactly once as an assignment LHS
    for name in wires:
        if name not in lhs_names_seen:
            raise UnassignedSignalError(f"Signal '{name}' is not assigned")
    for name in outputs:
        if name not in lhs_names_seen:
            raise UnassignedSignalError(f"Signal '{name}' is not assigned")

    return {
        'inputs': inputs,
        'outputs': outputs,
        'wires': wires,
        'assignments': assignments,
    }


# =============================================================================
# BENCH circuit format parser
# =============================================================================

def parse_bench_file(filepath: str) -> dict:
    """Parse a BENCH circuit file and return the parsed result."""
    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {filepath}")

    inputs = {}
    outputs = {}
    wires = {}
    assignments = []

    # Track all defined signals for OUTPUT validation
    defined_signals = set()
    output_names = []

    # Supported operators (BENCH is scalar-only)
    bench_ops = {'NOT', 'BUF', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'}

    for line_num, line in enumerate(lines, 1):
        # Strip whitespace
        line = line.strip()

        # Skip empty lines
        if not line:
            continue

        # Remove comments
        if '#' in line:
            line = line[:line.index('#')]
            line = line.strip()

        # Skip if empty after removing comment
        if not line:
            continue

        # Check for INPUT declaration
        if line.startswith('INPUT(') and line.endswith(')'):
            match = re.match(r'^INPUT\(\s*(\w+)\s*\)$', line)
            if not match:
                raise BenchParseError(
                    f"Invalid INPUT syntax: {line}",
                    file=filepath,
                    line=line_num,
                    col=1
                )
            name = match.group(1)

            # Check for brackets in identifier (scalar-only constraint)
            if '[' in name or ']' in name:
                raise BenchParseError(
                    f"BENCH format does not support vector signals: {name}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            if name in inputs:
                raise RedefinitionError(name)
            inputs[name] = (0, 0)  # BENCH is always scalar
            defined_signals.add(name)
            continue

        # Check for OUTPUT declaration
        if line.startswith('OUTPUT(') and line.endswith(')'):
            match = re.match(r'^OUTPUT\(\s*(\w+)\s*\)$', line)
            if not match:
                raise BenchParseError(
                    f"Invalid OUTPUT syntax: {line}",
                    file=filepath,
                    line=line_num,
                    col=1
                )
            name = match.group(1)

            # Check for brackets in identifier (scalar-only constraint)
            if '[' in name or ']' in name:
                raise BenchParseError(
                    f"BENCH format does not support vector signals: {name}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            output_names.append(name)
            outputs[name] = (0, 0)  # BENCH is always scalar
            defined_signals.add(name)
            continue

        # Check for assignment (lhs = OP(...))
        if '=' in line:
            parts = line.split('=', 1)
            if len(parts) != 2:
                raise BenchParseError(
                    f"Invalid assignment syntax: {line}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            lhs = parts[0].strip()
            rhs = parts[1].strip()

            # Check for brackets in LHS (scalar-only constraint)
            if '[' in lhs or ']' in lhs:
                raise BenchParseError(
                    f"BENCH format does not support vector signals: {lhs}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            # Parse RHS - should be OP(...)
            op_match = re.match(r'^(\w+)\s*\((.*)\)$', rhs)
            if not op_match:
                raise BenchParseError(
                    f"Invalid operator call syntax: {rhs}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            op_name = op_match.group(1).upper()
            args_str = op_match.group(2)

            # Check for literals in RHS (BENCH doesn't allow literals)
            # Parse arguments and check for literals
            if args_str.strip():
                # Split by comma
                args = [arg.strip() for arg in args_str.split(',')]
                # Check for literals (0, 1, 0b, 0x, N'b, etc.)
                for arg in args:
                    if arg in ('0', '1'):
                        raise BenchParseError(
                            f"BENCH format does not allow literals: {arg}",
                            file=filepath,
                            line=line_num,
                            col=1
                        )
                    if arg.startswith('0b') or arg.startswith('0x'):
                        raise BenchParseError(
                            f"BENCH format does not allow literals: {arg}",
                            file=filepath,
                            line=line_num,
                            col=1
                        )
                    if re.match(r'^\d+\'', arg):
                        raise BenchParseError(
                            f"BENCH format does not allow literals: {arg}",
                            file=filepath,
                            line=line_num,
                            col=1
                        )
                    # Check for brackets in argument (scalar-only)
                    if '[' in arg or ']' in arg:
                        raise BenchParseError(
                            f"BENCH format does not support vector signals: {arg}",
                            file=filepath,
                            line=line_num,
                            col=1
                        )
            else:
                args = []

            # Validate operator
            if op_name not in bench_ops:
                raise BenchParseError(
                    f"Unknown operator '{op_name}' in BENCH format",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            # Check arity constraints
            if op_name in ('NOT', 'BUF') and len(args) != 1:
                raise ArityError(f"Operator '{op_name}' requires exactly 1 argument, got {len(args)}")
            if op_name in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR') and len(args) < 2:
                raise ArityError(f"Operator '{op_name}' requires at least 2 arguments, got {len(args)}")

            # Check for duplicate LHS
            for existing_lhs, _ in assignments:
                if existing_lhs == lhs:
                    raise RedefinitionError(lhs)

            # Check for brackets in LHS
            if '[' in lhs or ']' in lhs:
                raise BenchParseError(
                    f"BENCH format does not support vector signals: {lhs}",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            # Convert RHS to expression string (same grammar as .circ files)
            if op_name in ('NOT', 'BUF'):
                rhs_expr = f"{op_name}({args[0]})"
            else:
                rhs_expr = f"{op_name}({', '.join(args)})"

            assignments.append((lhs, rhs_expr))
            wires[lhs] = (0, 0)  # BENCH is always scalar
            defined_signals.add(lhs)
            continue

        # If we get here, the line is invalid
        raise BenchParseError(
            f"Invalid statement: {line}",
            file=filepath,
            line=line_num,
            col=1
        )

    # Validate OUTPUT references
    for name in output_names:
        if name not in defined_signals:
            raise UndefinedNameError(f"OUTPUT references undefined signal: {name}")

    return {
        'inputs': inputs,
        'outputs': outputs,
        'wires': wires,
        'assignments': assignments,
    }


# =============================================================================
# Validator - checks widths and other semantic errors
# =============================================================================

@dataclass
class Signal:
    """Represents a signal with its width."""
    name: str
    msb: int
    lsb: int

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass
class Circuit:
    inputs: dict[str, Signal]
    outputs: dict[str, Signal]
    wires: dict[str, Signal]
    assignments: list[tuple[str, tuple]]


def get_expr_width(expr: tuple, signals: dict[str, Signal]) -> int:
    """Get the width of an expression."""
    kind = expr[0]

    if kind == 'identifier':
        name = expr[1]
        if name in signals:
            return signals[name].width
        return 1

    elif kind == 'literal':
        return parse_literal_width(expr[1])

    elif kind == 'bit_index':
        return 1

    elif kind == 'slice':
        hi, lo = expr[2], expr[3]
        return hi - lo + 1

    elif kind == 'concatenation':
        total = 0
        for arg in expr[1]:
            total += get_expr_width(arg, signals)
        return total

    elif kind == 'call':
        op_name = expr[1]
        args = expr[2]

        if op_name in ('NOT', 'BUF'):
            return get_expr_width(args[0], signals)
        elif op_name in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            return 1
        elif op_name in ('MUX', 'ITE'):
            return get_expr_width(args[1], signals)
        elif op_name == 'EQ':
            return 1
        else:
            # AND, OR, XOR, NAND, NOR, XNOR - all have same width as operands
            if args:
                return get_expr_width(args[0], signals)
            return 1

    return 1


def validate_expr_width(expr: tuple, signals: dict[str, Signal], filename: str) -> int:
    """Validate and return the width of an expression. Raises errors on mismatch."""
    kind = expr[0]

    if kind == 'identifier':
        name = expr[1]
        if name not in signals:
            raise UndefinedNameError(f"Undefined signal '{name}'")
        return signals[name].width

    elif kind == 'literal':
        width = parse_literal_width(expr[1])
        return width

    elif kind == 'bit_index':
        name = expr[1]
        index = expr[2]
        if name not in signals:
            raise UndefinedNameError(f"Undefined signal '{name}'")
        width = signals[name].width
        if index < 0 or index > width - 1:
            raise IndexOutOfBoundsError(name, index, width)
        return 1

    elif kind == 'slice':
        name = expr[1]
        hi = expr[2]
        lo = expr[3]
        if name not in signals:
            raise UndefinedNameError(f"Undefined signal '{name}'")
        width = signals[name].width
        if hi < 0 or hi > width - 1 or lo < 0 or lo > width - 1:
            raise IndexOutOfBoundsError(name, hi, width)
        return hi - lo + 1

    elif kind == 'concatenation':
        total_width = 0
        for arg in expr[1]:
            total_width += validate_expr_width(arg, signals, filename)
        return total_width

    elif kind == 'call':
        op_name = expr[1]
        args = expr[2]

        if op_name in ('NOT', 'BUF', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            arg_width = validate_expr_width(args[0], signals, filename)
            if op_name in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR') and arg_width < 1:
                raise WidthMismatchError(op_name, arg_width, 1)
            return arg_width

        elif op_name in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            if not args:
                raise WidthMismatchError(op_name, 0, 0)
            first_width = validate_expr_width(args[0], signals, filename)
            for arg in args[1:]:
                width = validate_expr_width(arg, signals, filename)
                if width != first_width:
                    raise WidthMismatchError(op_name, first_width, width)
            return first_width

        elif op_name in ('MUX', 'ITE'):
            sel_width = validate_expr_width(args[0], signals, filename)
            if sel_width != 1:
                raise WidthMismatchError(op_name, 1, sel_width)
            a_width = validate_expr_width(args[1], signals, filename)
            b_width = validate_expr_width(args[2], signals, filename)
            if a_width != b_width:
                raise WidthMismatchError(op_name, a_width, b_width)
            return a_width

        elif op_name == 'EQ':
            a_width = validate_expr_width(args[0], signals, filename)
            b_width = validate_expr_width(args[1], signals, filename)
            if a_width != b_width:
                raise WidthMismatchError(op_name, a_width, b_width)
            return 1

        else:
            return 1

    return 1


def parse_literal_width(lit: str) -> int:
    """Parse the width of a literal."""
    if lit in ('0', '1'):
        return 1
    if lit.startswith('0b'):
        return len(lit) - 2
    if lit.startswith('0x'):
        return (len(lit) - 2) * 4
    if re.match(r'^\d+\'[\d]', lit):
        match = re.match(r'^(\d+)\'', lit)
        return int(match.group(1))
    return 1


def validate_circuit(circuit: dict, filename: str) -> Circuit:
    """Validate a parsed circuit and check for errors."""
    inputs = {}
    outputs = {}
    wires = {}

    # Convert to Signal objects
    for name, (msb, lsb) in circuit['inputs'].items():
        inputs[name] = Signal(name, msb, lsb)
    for name, (msb, lsb) in circuit['outputs'].items():
        outputs[name] = Signal(name, msb, lsb)
    for name, (msb, lsb) in circuit['wires'].items():
        wires[name] = Signal(name, msb, lsb)

    assignments = circuit['assignments']

    # Check that all LHS are valid and check width compatibility
    for lhs, rhs in assignments:
        if lhs in inputs:
            raise InputAssignmentError(f"Cannot assign to input '{lhs}'")
        if lhs not in outputs and lhs not in wires:
            raise UndefinedNameError(f"Undefined signal '{lhs}'")

        # Get LHS width
        if lhs in outputs:
            lhs_width = outputs[lhs].width
        else:
            lhs_width = wires[lhs].width

        # Get RHS width and validate expressions
        rhs_width = validate_expr_width(rhs, {**inputs, **outputs, **wires}, filename)

        # Width check: RHS must match LHS
        if lhs_width != rhs_width:
            raise WidthMismatchError(lhs, lhs_width, rhs_width)

    # Build dependency graph for cycle detection
    all_signals = {**inputs, **outputs, **wires}
    deps = {name: set() for name in {**wires, **outputs}}

    def collect_deps(expr, signal_name):
        kind = expr[0]

        if kind == 'identifier':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'bit_index':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'slice':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'concatenation':
            for arg in expr[1]:
                collect_deps(arg, signal_name)
        elif kind == 'call':
            for arg in expr[2]:
                collect_deps(arg, signal_name)

    for lhs, rhs in assignments:
        if lhs in deps:
            collect_deps(rhs, lhs)

    # Check for cycles using DFS
    visited = set()
    rec_stack = set()
    cycle_path = []

    def dfs(node):
        visited.add(node)
        rec_stack.add(node)
        cycle_path.append(node)

        for neighbor in deps.get(node, set()):
            if neighbor not in inputs and neighbor not in {**wires, **outputs}:
                continue
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                start_idx = cycle_path.index(neighbor)
                cycle = cycle_path[start_idx:] + [neighbor]
                raise CycleError(cycle)

        cycle_path.pop()
        rec_stack.remove(node)
        return False

    for node in {**wires, **outputs}:
        if node not in visited:
            dfs(node)

    return Circuit(inputs=inputs, outputs=outputs, wires=wires, assignments=assignments)


# =============================================================================
# Expression normalization for deterministic output
# =============================================================================

def normalize_expr(expr: tuple) -> tuple:
    """Normalize an expression for deterministic output.

    - Nested associative operators are flattened
    - Arguments are sorted lexicographically
    - Recursively applied to all subexpressions
    """
    kind = expr[0]

    if kind == 'concatenation':
        # Concatenation: recursively normalize and return as-is (order matters)
        return ('concatenation', [normalize_expr(arg) for arg in expr[1]])

    elif kind == 'call':
        op_name = expr[1]
        args = [normalize_expr(arg) for arg in expr[2]]

        # For associative operators (AND, OR, XOR), flatten nested calls
        if op_name in ('AND', 'OR', 'XOR'):
            flattened = []
            for arg in args:
                if arg[0] == 'call' and arg[1] == op_name:
                    # Flatten nested operator call
                    flattened.extend(arg[2])
                else:
                    flattened.append(arg)
            # Sort arguments lexicographically by their string representation
            # Use a stable sort by converting to string for comparison
            def arg_key(a):
                if a[0] == 'identifier':
                    return a[1]
                elif a[0] == 'literal':
                    return a[1]
                elif a[0] == 'bit_index':
                    return f"{a[1]}[{a[2]}]"
                elif a[0] == 'slice':
                    return f"{a[1]}[{a[2]}:{a[3]}]"
                elif a[0] == 'call':
                    return f"{a[1]}({','.join(arg_key(arg) for arg in a[2])})"
                else:
                    return str(a)

            flattened.sort(key=arg_key)
            return ('call', op_name, flattened)
        else:
            return ('call', op_name, args)

    else:
        # identifier, literal, bit_index, slice - return as-is
        return expr


def format_expr(expr: tuple, signals: dict[str, Signal]) -> str:
    """Format an expression to a string for .circ output.

    Operator names uppercase, proper formatting for concatenations, literals.
    """
    kind = expr[0]

    if kind == 'identifier':
        return expr[1]

    elif kind == 'literal':
        return expr[1]

    elif kind == 'bit_index':
        return f"{expr[1]}[{expr[2]}]"

    elif kind == 'slice':
        return f"{expr[1]}[{expr[2]}:{expr[3]}]"

    elif kind == 'concatenation':
        args = ', '.join(format_expr(arg, signals) for arg in expr[1])
        return f"{{{args}}}"

    elif kind == 'call':
        op_name = expr[1]
        args = ', '.join(format_expr(arg, signals) for arg in expr[2])
        return f"{op_name}({args})"

    else:
        raise ValueError(f"Unknown expression kind: {kind}")


def extract_subcircuit(
    circuit: Circuit,
    output_names: set[str]
) -> Circuit:
    """Extract a subcircuit containing only what's needed to compute the specified outputs.

    Uses backward cone extraction.
    """
    # Verify all output names exist
    all_output_names = set(circuit.outputs.keys())
    for name in output_names:
        if name not in all_output_names:
            raise UndefinedNameError(f"Unknown output: {name}")

    # Build backward cone: start from target outputs and include all dependencies
    include_signals: set[str] = set()
    queue = list(output_names)
    include_signals.update(queue)

    # Build reverse dependency map
    reverse_deps: dict[str, set[str]] = {name: set() for name in {**circuit.wires, **circuit.outputs}}
    for lhs, rhs in circuit.assignments:
        if lhs in reverse_deps:
            for dep in get_dependencies(rhs):
                if dep in {**circuit.inputs, **circuit.wires, **circuit.outputs}:
                    reverse_deps[lhs].add(dep)

    # BFS backwards
    idx = 0
    while idx < len(queue):
        node = queue[idx]
        idx += 1
        # Find what depends on this node (reverse deps)
        for lhs, deps in reverse_deps.items():
            if node in deps and lhs not in include_signals:
                include_signals.add(lhs)
                queue.append(lhs)

    # Build the new circuit
    new_inputs = {name: sig for name, sig in circuit.inputs.items() if name in include_signals}
    new_outputs = {name: sig for name, sig in circuit.outputs.items() if name in output_names}
    new_wires = {name: sig for name, sig in circuit.wires.items() if name in include_signals}

    # Include only assignments needed for included signals
    new_assignments = []
    for lhs, rhs in circuit.assignments:
        if lhs in include_signals:
            new_assignments.append((lhs, rhs))

    return Circuit(
        inputs=new_inputs,
        outputs=new_outputs,
        wires=new_wires,
        assignments=new_assignments
    )


def write_circuit(
    circuit: Circuit,
    fmt: str = "circ"
) -> str:
    """Write a circuit to a string in the specified format.

    Args:
        circuit: The circuit to write
        fmt: Output format - "circ" or "json"

    Returns:
        The circuit as a formatted string
    """
    if fmt == "circ":
        return write_circuit_circ(circuit)
    elif fmt == "json":
        return write_circuit_json(circuit)
    else:
        raise UnknownOutputFormatError(fmt)


def write_circuit_circ(circuit: Circuit) -> str:
    """Write a circuit to canonical .circ format.

    Rules:
    - Exactly one input line (inputs sorted by name)
    - Exactly one output line (outputs sorted by name)
    - Optional one wire line (wires sorted by name, omit if none)
    - One blank line
    - Assignments in dependency order; ties broken by LHS name
    """
    lines = []

    # Input line
    if circuit.inputs:
        sorted_inputs = sorted(circuit.inputs.keys())
        lines.append("input " + " ".join(sorted_inputs))
    else:
        lines.append("input")

    # Output line
    sorted_outputs = sorted(circuit.outputs.keys())
    lines.append("output " + " ".join(sorted_outputs))

    # Wire line (if any wires)
    if circuit.wires:
        sorted_wires = sorted(circuit.wires.keys())
        lines.append("wire " + " ".join(sorted_wires))

    # Blank line
    lines.append("")

    # Assignments in dependency order
    # Build dependency graph
    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}
    deps: dict[str, set[str]] = {name: set() for name in {**circuit.wires, **circuit.outputs}}

    def collect_deps(expr, signal_name):
        kind = expr[0]
        if kind == 'identifier':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'bit_index':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'slice':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'concatenation':
            for arg in expr[1]:
                collect_deps(arg, signal_name)
        elif kind == 'call':
            for arg in expr[2]:
                collect_deps(arg, signal_name)

    for lhs, rhs in circuit.assignments:
        if lhs in deps:
            collect_deps(rhs, lhs)

    # Topological sort
    visited: set[str] = set()
    temp_visited: set[str] = set()
    order: list[str] = []

    def topological_sort(node: str):
        if node in temp_visited:
            return
        if node in visited:
            return
        temp_visited.add(node)
        for neighbor in deps.get(node, set()):
            if neighbor in {**circuit.wires, **circuit.outputs}:
                topological_sort(neighbor)
        temp_visited.discard(node)
        visited.add(node)
        order.append(node)

    for node in {**circuit.wires, **circuit.outputs}:
        if node not in visited:
            topological_sort(node)

    # Sort assignments within each depth level by LHS name
    # Group by topological order index
    order_index = {name: i for i, name in enumerate(order)}

    # Sort assignments: first by dependency order, then by LHS name
    sorted_assignments = sorted(circuit.assignments, key=lambda x: (
        order_index.get(x[0], float('inf')),  # Dependency order first
        x[0]  # Then LHS name for ties
    ))

    # Create assignment map for quick lookup
    assignment_map = {lhs: rhs for lhs, rhs in circuit.assignments}

    # Format each assignment
    for lhs, rhs in sorted_assignments:
        # Normalize the expression before formatting
        normalized_rhs = normalize_expr(rhs)
        formatted_rhs = format_expr(normalized_rhs, all_signals)
        lines.append(f"{lhs} = {formatted_rhs}")

    return "\n".join(lines) + "\n"


def write_circuit_json(circuit: Circuit) -> str:
    """Write a circuit to canonical JSON format.

    Keys in order: format_version, inputs, outputs, wires, assignments
    - Arrays sorted by name
    - Assignments in dependency order; ties by LHS name
    """
    # Build dependency graph for ordering
    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}
    deps: dict[str, set[str]] = {name: set() for name in {**circuit.wires, **circuit.outputs}}

    def collect_deps(expr, signal_name):
        kind = expr[0]
        if kind == 'identifier':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'bit_index':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'slice':
            dep_name = expr[1]
            if dep_name in all_signals:
                deps[signal_name].add(dep_name)
        elif kind == 'concatenation':
            for arg in expr[1]:
                collect_deps(arg, signal_name)
        elif kind == 'call':
            for arg in expr[2]:
                collect_deps(arg, signal_name)

    for lhs, rhs in circuit.assignments:
        if lhs in deps:
            collect_deps(rhs, lhs)

    # Topological sort
    visited: set[str] = set()
    temp_visited: set[str] = set()
    order: list[str] = []

    def topological_sort(node: str):
        if node in temp_visited:
            return
        if node in visited:
            return
        temp_visited.add(node)
        for neighbor in deps.get(node, set()):
            if neighbor in {**circuit.wires, **circuit.outputs}:
                topological_sort(neighbor)
        temp_visited.discard(node)
        visited.add(node)
        order.append(node)

    for node in {**circuit.wires, **circuit.outputs}:
        if node not in visited:
            topological_sort(node)

    order_index = {name: i for i, name in enumerate(order)}

    # Build JSON structure
    data = {
        "format_version": 1,
        "inputs": sorted(
            [{"name": name, "msb": sig.msb, "lsb": sig.lsb} for name, sig in circuit.inputs.items()],
            key=lambda x: x["name"]
        ),
        "outputs": sorted(
            [{"name": name, "msb": sig.msb, "lsb": sig.lsb} for name, sig in circuit.outputs.items()],
            key=lambda x: x["name"]
        ),
        "wires": sorted(
            [{"name": name, "msb": sig.msb, "lsb": sig.lsb} for name, sig in circuit.wires.items()],
            key=lambda x: x["name"]
        ),
        "assignments": []
    }

    # Sort assignments
    sorted_assignments = sorted(circuit.assignments, key=lambda x: (
        order_index.get(x[0], float('inf')),
        x[0]
    ))

    for lhs, rhs in sorted_assignments:
        # Normalize expression before formatting
        normalized_rhs = normalize_expr(rhs)
        formatted_rhs = format_expr(normalized_rhs, all_signals)
        data["assignments"].append({
            "lhs": lhs,
            "rhs": formatted_rhs
        })

    return json.dumps(data, indent=2) + "\n"


# =============================================================================
# Cone command - subcircuit extraction
# =============================================================================

def handle_cone(
    filepath: str,
    output_names: set[str],
    out_path: str,
    out_format: Optional[str],
    json_flag: bool
) -> int:
    """Handle the cone command."""
    try:
        parsed = parse_file(filepath)
        circuit = validate_circuit(parsed, filepath)
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "cone",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("cone", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    # Extract subcircuit
    try:
        subcircuit = extract_subcircuit(circuit, output_names)
    except UndefinedNameError as e:
        if json_flag:
            print(json.dumps(e.to_json("cone", filepath)))
        else:
            print(f"Error: {e}")
        return 3

    # Determine output format
    fmt = out_format
    if fmt is None:
        if out_path.endswith('.circ'):
            fmt = 'circ'
        elif out_path.endswith('.json'):
            fmt = 'json'
        else:
            error = UnknownOutputFormatError(out_path)
            if json_flag:
                print(json.dumps(error.to_json("cone", filepath)))
            else:
                print(f"Error: {error}")
            return 1

    # Check for unsupported formats
    if fmt == 'bench':
        error = UnsupportedFeatureError('BENCH output in cone')
        if json_flag:
            print(json.dumps(error.to_json("cone", filepath)))
        else:
            print(f"Error: {error}")
        return 1

    # Write the circuit
    try:
        content = write_circuit(subcircuit, fmt)
        with open(out_path, 'w') as f:
            f.write(content)
    except IOError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "cone",
                "exit_code": 1,
                "error": {
                    "type": "IOError",
                    "message": str(e),
                    "file": out_path,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except UnknownOutputFormatError as e:
        if json_flag:
            print(json.dumps(e.to_json("cone", filepath)))
        else:
            print(f"Error: {e}")
        return 1

    # Success
    data = {
        "ok": True,
        "command": "cone",
        "out_path": out_path
    }

    if json_flag:
        output_json("cone", data)
    else:
        print(f"Wrote cone to: {out_path}")

    return 0


# =============================================================================
# Stats command - circuit metrics and statistics
# =============================================================================

def get_dependencies(expr: tuple) -> list[str]:
    """Extract all signal dependencies from an RHS expression recursively.

    For indexed/sliced references, the dependency is on the base signal.
    Duplicate references are included (for edge counting).
    """
    deps = []
    kind = expr[0]

    if kind == 'identifier':
        deps.append(expr[1])
    elif kind == 'bit_index':
        deps.append(expr[1])
    elif kind == 'slice':
        deps.append(expr[1])
    elif kind == 'concatenation':
        for arg in expr[1]:
            deps.extend(get_dependencies(arg))
    elif kind == 'call':
        for arg in expr[2]:
            deps.extend(get_dependencies(arg))
    # literal has no dependencies

    return deps


def get_op_category(rhs: tuple) -> str:
    """Classify an RHS into op_histogram category."""
    kind = rhs[0]

    if kind in ('identifier', 'bit_index', 'slice', 'literal'):
        return 'BUF'
    elif kind == 'concatenation':
        return 'CONCAT'
    elif kind == 'call':
        return rhs[1]  # Uppercase operator name
    else:
        return 'BUF'


def compute_depth(circuit: Circuit) -> int:
    """Compute maximum depth from any input to any output.

    Each assignment adds 1 to depth. A literal-only assignment has depth 1.
    """
    # Build dependency graph: signal -> set of signals it depends on
    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}
    deps: dict[str, set[str]] = {name: set() for name in all_signals}

    for lhs, rhs in circuit.assignments:
        if lhs in all_signals:
            for dep in get_dependencies(rhs):
                if dep in all_signals:
                    deps[lhs].add(dep)

    # Compute depth using DP/topological order
    # We need a topological order: dependencies before dependents
    # First, build reverse graph for topological sort
    reverse_deps: dict[str, set[str]] = {name: set() for name in all_signals}
    for node, d_set in deps.items():
        for d in d_set:
            if d in reverse_deps:
                reverse_deps[d].add(node)

    # Find inputs (no dependencies)
    in_degree: dict[str, int] = {name: 0 for name in all_signals}
    for node, d_set in deps.items():
        in_degree[node] = len(d_set)

    # Topological sort using Kahn's algorithm
    from collections import deque
    queue = deque([name for name, deg in in_degree.items() if deg == 0])
    topo_order = []

    while queue:
        node = queue.popleft()
        topo_order.append(node)
        for neighbor in reverse_deps.get(node, set()):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Compute depth for each signal
    depth: dict[str, int] = {}
    for node in topo_order:
        if node in circuit.inputs:
            depth[node] = 0
        else:
            max_dep_depth = 0
            for dep in deps.get(node, set()):
                if dep in depth:
                    max_dep_depth = max(max_dep_depth, depth[dep])
            # If no dependencies (literal-only), depth is 1, else max_dep_depth + 1
            if not deps.get(node, set()):
                depth[node] = 1
            else:
                depth[node] = max_dep_depth + 1

    # Max depth among outputs
    max_depth = 0
    for out_name in circuit.outputs:
        if out_name in depth:
            max_depth = max(max_depth, depth[out_name])

    return max_depth


def count_edges(circuit: Circuit) -> int:
    """Count total dependency references (with duplicates)."""
    count = 0
    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}

    for lhs, rhs in circuit.assignments:
        if lhs in all_signals:  # Only count assignments to wires/outputs
            count += len(get_dependencies(rhs))

    return count


def handle_stats(
    filepath: str,
    json_flag: bool,
    fmt: str = "auto"
) -> int:
    """Handle the stats command."""
    try:
        parsed = parse_file(filepath, fmt)
        circuit = validate_circuit(parsed, filepath)
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "stats",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("stats", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    # Compute metrics
    inputs_count = len(circuit.inputs)
    outputs_count = len(circuit.outputs)
    wires_count = len(circuit.wires)
    assignments_count = len(circuit.assignments)
    edges_count = count_edges(circuit)
    depth = compute_depth(circuit)

    # Compute op_histogram
    op_categories = ['BUF', 'CONCAT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR',
                     'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ', 'NOT']
    op_histogram = {cat: 0 for cat in op_categories}

    for lhs, rhs in circuit.assignments:
        if lhs in circuit.wires or lhs in circuit.outputs:
            cat = get_op_category(rhs)
            op_histogram[cat] += 1

    data = {
        "ok": True,
        "command": "stats",
        "inputs_count": inputs_count,
        "outputs_count": outputs_count,
        "wires_count": wires_count,
        "assignments_count": assignments_count,
        "edges_count": edges_count,
        "depth": depth,
        "op_histogram": op_histogram
    }

    if json_flag:
        output_json("stats", data)
    else:
        # Plain text output
        print(f"inputs: {inputs_count}")
        print(f"outputs: {outputs_count}")
        print(f"wires: {wires_count}")
        print(f"assignments: {assignments_count}")
        print(f"edges: {edges_count}")
        print(f"depth: {depth}")
        print("op_histogram:")
        for cat in op_categories:
            print(f"  {cat}: {op_histogram[cat]}")

    return 0


# =============================================================================
# Lint command - warnings about potential issues
# =============================================================================

class WarningType(Enum):
    UnusedInputWarning = "UnusedInputWarning"
    ConstantOutputWarning = "ConstantOutputWarning"
    ConstantOutputCheckSkippedWarning = "ConstantOutputCheckSkippedWarning"


@dataclass
class LintWarning:
    type: WarningType
    name: Optional[str]
    message: str
    value: Optional[str] = None

    def to_dict(self) -> dict:
        result = {
            "type": self.type.value,
            "name": self.name,
            "message": self.message
        }
        if self.value is not None:
            result["value"] = self.value
        return result


def get_all_dependents(circuit: Circuit) -> dict[str, set[str]]:
    """Build a map of which signals depend on each signal.

    Returns: signal_name -> set of signals that transitively depend on it
    """
    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}

    # Direct dependencies: signal -> set of signals it depends on
    direct_deps: dict[str, set[str]] = {name: set() for name in all_signals}

    for lhs, rhs in circuit.assignments:
        if lhs in all_signals:
            for dep in get_dependencies(rhs):
                if dep in all_signals:
                    direct_deps[lhs].add(dep)

    # Build reverse: signal -> set of signals that depend on it
    dependents: dict[str, set[str]] = {name: set() for name in all_signals}
    for node, d_set in direct_deps.items():
        for d in d_set:
            if d in dependents:
                dependents[d].add(node)

    # Compute transitive dependents using DFS
    transitive: dict[str, set[str]] = {name: set() for name in all_signals}

    def dfs(signal: str, visited: set[str]):
        for dep in dependents.get(signal, set()):
            if dep not in visited:
                visited.add(dep)
                transitive[signal].add(dep)
                dfs(dep, visited)

    for signal in all_signals:
        dfs(signal, set())

    return transitive


def format_constant_value(value: int, msb: int, lsb: int) -> str:
    """Format a constant value for output."""
    if msb == lsb:
        return str(value)
    width = msb - lsb + 1
    return f"0b{value:0{width}b}"


def evaluate_constant(circuit: Circuit, input_vals: dict[str, int]) -> dict[str, int]:
    """Evaluate circuit with given input values. Returns signal values."""
    values: dict[str, int] = {}
    for name in circuit.inputs:
        values[name] = input_vals.get(name, 0)
    for name in circuit.wires:
        values[name] = 0
    for name in circuit.outputs:
        values[name] = 0

    def eval_expr(expr: tuple) -> int:
        kind = expr[0]

        if kind == 'identifier':
            return values[expr[1]]
        elif kind == 'literal':
            val, _ = parse_literal_value(expr[1])
            return val
        elif kind == 'bit_index':
            name, index = expr[1], expr[2]
            return (values[name] >> index) & 1
        elif kind == 'slice':
            name, hi, lo = expr[1], expr[2], expr[3]
            val = values[name]
            width = hi - lo + 1
            mask = (1 << width) - 1
            return (val >> lo) & mask
        elif kind == 'concatenation':
            result = 0
            bit_pos = 0
            for arg in reversed(expr[1]):
                arg_val = eval_expr(arg)
                # Get width
                if arg[0] == 'literal':
                    _, width = parse_literal_value(arg[1])
                elif arg[0] == 'identifier':
                    width = next((s.width for s in [circuit.outputs.get(arg[1]), circuit.wires.get(arg[1]), circuit.inputs.get(arg[1])] if s), 1)
                elif arg[0] == 'bit_index':
                    width = 1
                elif arg[0] == 'slice':
                    width = arg[2] - arg[3] + 1
                else:
                    width = get_expr_width(arg, {**circuit.outputs, **circuit.wires, **circuit.inputs})
                result |= (arg_val << bit_pos)
                bit_pos += width
            return result
        elif kind == 'call':
            op_name = expr[1]
            args = [eval_expr(arg) for arg in expr[2]]

            if op_name == 'NOT':
                return ~args[0] & ((1 << args[0].bit_length()) - 1) if args[0] > 0 else 0
            elif op_name == 'BUF':
                return args[0]
            elif op_name == 'AND':
                result = args[0]
                for arg in args[1:]:
                    result &= arg
                return result
            elif op_name == 'OR':
                result = args[0]
                for arg in args[1:]:
                    result |= arg
                return result
            elif op_name == 'XOR':
                result = args[0]
                for arg in args[1:]:
                    result ^= arg
                return result
            elif op_name == 'NAND':
                result = args[0]
                for arg in args[1:]:
                    result &= arg
                return ~result
            elif op_name == 'NOR':
                result = args[0]
                for arg in args[1:]:
                    result |= arg
                return ~result
            elif op_name == 'XNOR':
                result = args[0]
                for arg in args[1:]:
                    result ^= arg
                return ~result
            elif op_name in ('MUX', 'ITE'):
                return args[1] if args[0] else args[2]
            elif op_name == 'REDUCE_AND':
                val = args[0]
                while val > 1:
                    if (val & 1) == 0:
                        return 0
                    val >>= 1
                return 1 if args[0] > 0 else 1
            elif op_name == 'REDUCE_OR':
                return 1 if args[0] != 0 else 0
            elif op_name == 'REDUCE_XOR':
                val = args[0]
                result = 0
                while val > 0:
                    result ^= (val & 1)
                    val >>= 1
                return result
            elif op_name == 'EQ':
                return 1 if args[0] == args[1] else 0

        raise ValueError(f"Unknown expression: {expr}")

    # Topologically evaluate
    topo_order = []
    visited: set[str] = set()
    temp_visited: set[str] = set()

    def visit(node: str):
        if node in temp_visited:
            return
        if node in visited:
            return
        temp_visited.add(node)
        for dep in get_dependencies(next((r for l, r in circuit.assignments if l == node), (None, ()))):
            if dep in circuit.wires or dep in circuit.outputs:
                visit(dep)
        temp_visited.discard(node)
        visited.add(node)
        topo_order.append(node)

    for lhs, _ in circuit.assignments:
        if lhs in circuit.wires or lhs in circuit.outputs:
            visit(lhs)

    for name in topo_order:
        for lhs, rhs in circuit.assignments:
            if lhs == name:
                values[name] = eval_expr(rhs)
                break

    return values


def handle_lint(
    filepath: str,
    json_flag: bool,
    max_input_bits: int = 16,
    fmt: str = "auto"
) -> int:
    """Handle the lint command."""
    try:
        parsed = parse_file(filepath, fmt)
        circuit = validate_circuit(parsed, filepath)
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "lint",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("lint", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    warnings: list[LintWarning] = []

    # Check for unused inputs
    dependents = get_all_dependents(circuit)
    for inp_name in circuit.inputs:
        # Check if any output depends on this input (transitively)
        used = False
        for out_name in circuit.outputs:
            if inp_name in dependents.get(out_name, set()):
                used = True
                break
        if not used:
            warnings.append(LintWarning(
                type=WarningType.UnusedInputWarning,
                name=inp_name,
                message=f"input '{inp_name}' is not used by any output"
            ))

    # Check for constant outputs
    total_input_bits = sum(sig.width for sig in circuit.inputs.values())

    if total_input_bits <= max_input_bits:
        # Exhaustive evaluation
        num_combos = 1 << total_input_bits

        # For each output, track if it's constant and what value
        output_const_values: dict[str, Optional[int]] = {name: None for name in circuit.outputs}
        output_msbs = {name: circuit.outputs[name].msb for name in circuit.outputs}
        output_lsbs = {name: circuit.outputs[name].lsb for name in circuit.outputs}

        # Enumerate all input combinations
        input_names = list(circuit.inputs.keys())
        input_widths = [circuit.inputs[name].width for name in input_names]

        for combo in range(num_combos):
            # Decode combo into input values
            input_vals = {}
            bit_pos = 0
            for i, name in enumerate(input_names):
                width = input_widths[i]
                mask = (1 << width) - 1
                val = (combo >> bit_pos) & mask
                input_vals[name] = val
                bit_pos += width

            # Evaluate
            results = evaluate_constant(circuit, input_vals)

            for out_name in circuit.outputs:
                val = results[out_name]
                if output_const_values[out_name] is None:
                    output_const_values[out_name] = val
                elif output_const_values[out_name] != val:
                    output_const_values[out_name] = None  # Not constant

        # Add warnings for constant outputs
        for out_name in circuit.outputs:
            if output_const_values[out_name] is not None:
                val = output_const_values[out_name]
                warnings.append(LintWarning(
                    type=WarningType.ConstantOutputWarning,
                    name=out_name,
                    message=f"output '{out_name}' is constant",
                    value=format_constant_value(val, output_msbs[out_name], output_lsbs[out_name])
                ))
    else:
        # Skip exhaustive check
        warnings.append(LintWarning(
            type=WarningType.ConstantOutputCheckSkippedWarning,
            name=None,
            message=f"constant output check skipped: {total_input_bits} input bits exceeds limit {max_input_bits}"
        ))

    # Sort warnings lexicographically by (type, name or "")
    warnings.sort(key=lambda w: (w.type.value, w.name if w.name is not None else ""))

    data = {
        "ok": True,
        "command": "lint",
        "warnings": [w.to_dict() for w in warnings]
    }

    if json_flag:
        output_json("lint", data)
    else:
        # Plain text output
        if not warnings:
            print("No warnings")
        else:
            for w in warnings:
                if w.value:
                    print(f"{w.type.value}: {w.name}: {w.message} ({w.value})")
                else:
                    print(f"{w.type.value}: {w.name}: {w.message}")

    return 0


# =============================================================================
# Dot command - Graphviz DOT export
# =============================================================================

def get_signal_node_id(signal_name: str, is_input: bool) -> str:
    """Get node ID for a signal.

    Inputs use 'in:' prefix, wires/outputs use 'sig:' prefix.
    """
    prefix = "in:" if is_input else "sig:"
    return f"{prefix}{signal_name}"


def get_op_node_id(lhs: str) -> str:
    """Get node ID for an operator node."""
    return f"op:{lhs}"


def get_signal_label(name: str, msb: int, lsb: int) -> str:
    """Get label for a signal node."""
    if msb == lsb:
        return name
    return f"{name}[{msb}:{lsb}]"


def get_op_label(rhs: tuple) -> str:
    """Get label for an operator node."""
    cat = get_op_category(rhs)
    return cat


def handle_dot(
    filepath: str,
    output_path: str,
    json_flag: bool,
    cone: Optional[str],
    fmt: str = "auto"
) -> int:
    """Handle the dot command."""
    try:
        parsed = parse_file(filepath, fmt)
        circuit = validate_circuit(parsed, filepath)
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "dot",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("dot", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    # Handle --cone filter
    target_outputs: set[str] = set()
    if cone:
        output_names = set(circuit.outputs.keys())
        requested = {name.strip() for name in cone.split(',')}
        for name in requested:
            if name not in output_names:
                if json_flag:
                    error = UndefinedNameError(f"unknown output: {name}")
                    print(json.dumps(error.to_json("dot", filepath)))
                else:
                    print(f"Error: unknown output: {name}")
                return 3
        target_outputs = requested

    # Build backward cone if --cone is specified
    include_signals: set[str] = set()
    if cone:
        # Start from target outputs and go backwards
        queue = list(target_outputs)
        include_signals.update(queue)

        # Build reverse dependency map
        reverse_deps: dict[str, set[str]] = {name: set() for name in {**circuit.wires, **circuit.outputs}}
        for lhs, rhs in circuit.assignments:
            if lhs in reverse_deps:
                for dep in get_dependencies(rhs):
                    if dep in {**circuit.inputs, **circuit.wires, **circuit.outputs}:
                        reverse_deps[lhs].add(dep)

        # BFS backwards
        idx = 0
        while idx < len(queue):
            node = queue[idx]
            idx += 1
            # Find what depends on this node (reverse deps)
            for lhs, deps in reverse_deps.items():
                if node in deps and lhs not in include_signals:
                    include_signals.add(lhs)
                    queue.append(lhs)

    # Collect nodes and edges
    nodes: set[tuple[str, str]] = set()  # (id, label)
    edges: set[tuple[str, str]] = set()  # (source, target)

    all_signals = {**circuit.inputs, **circuit.wires, **circuit.outputs}

    # Add input nodes
    for name, sig in circuit.inputs.items():
        if not cone or name in include_signals:
            node_id = get_signal_node_id(name, is_input=True)
            label = get_signal_label(name, sig.msb, sig.lsb)
            nodes.add((node_id, label))

    # Add wire/output nodes and op nodes + edges
    assignment_map = {lhs: rhs for lhs, rhs in circuit.assignments}

    for name, sig in circuit.wires.items():
        if not cone or name in include_signals:
            node_id = get_signal_node_id(name, is_input=False)
            label = get_signal_label(name, sig.msb, sig.lsb)
            nodes.add((node_id, label))

            # Add op node and edges from dependencies
            if name in assignment_map:
                rhs = assignment_map[name]
                op_id = get_op_node_id(name)
                op_label = get_op_label(rhs)
                nodes.add((op_id, op_label))

                # Edges from dependencies to op
                for dep in get_dependencies(rhs):
                    if dep in all_signals:
                        if dep in circuit.inputs:
                            src = get_signal_node_id(dep, is_input=True)
                        else:
                            src = get_signal_node_id(dep, is_input=False)
                        edges.add((src, op_id))

                # Edge from op to signal
                edges.add((op_id, node_id))

    for name, sig in circuit.outputs.items():
        if not cone or name in include_signals:
            node_id = get_signal_node_id(name, is_input=False)
            label = get_signal_label(name, sig.msb, sig.lsb)
            nodes.add((node_id, label))

            # Add op node and edges from dependencies
            if name in assignment_map:
                rhs = assignment_map[name]
                op_id = get_op_node_id(name)
                op_label = get_op_label(rhs)
                nodes.add((op_id, op_label))

                # Edges from dependencies to op
                for dep in get_dependencies(rhs):
                    if dep in all_signals:
                        if dep in circuit.inputs:
                            src = get_signal_node_id(dep, is_input=True)
                        else:
                            src = get_signal_node_id(dep, is_input=False)
                        edges.add((src, op_id))

                # Edge from op to signal
                edges.add((op_id, node_id))

    # Sort nodes and edges lexicographically
    sorted_nodes = sorted(nodes, key=lambda x: x[0])
    sorted_edges = sorted(edges, key=lambda x: (x[0], x[1]))

    # Write DOT file
    try:
        with open(output_path, 'w') as f:
            f.write("digraph circopt {\n")
            for node_id, label in sorted_nodes:
                f.write(f"  {node_id} [label=\"{label}\"];\n")
            for src, tgt in sorted_edges:
                f.write(f"  {src} -> {tgt};\n")
            f.write("}\n")
    except IOError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "dot",
                "exit_code": 1,
                "error": {
                    "type": "IOError",
                    "message": str(e),
                    "file": output_path,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1

    data = {
        "ok": True,
        "command": "dot",
        "dot_path": output_path
    }

    if json_flag:
        output_json("dot", data)
    else:
        print(f"Wrote DOT file: {output_path}")

    return 0


# =============================================================================
# 2-valued evaluation
# =============================================================================

def parse_literal_value(lit: str) -> tuple[int, int]:
    """Parse a literal and return (value, width)."""
    if lit in ('0', '1'):
        return int(lit), 1
    if lit.startswith('0b'):
        digits = lit[2:].replace('_', '')
        value = int(digits, 2) if digits else 0
        return value, len(digits)
    if lit.startswith('0x'):
        digits = lit[2:].replace('_', '')
        value = int(digits, 16) if digits else 0
        return value, len(digits) * 4
    if re.match(r'^\d+\'[\d]', lit):
        match = re.match(r'^(\d+)\'([bdh])(.+)$', lit, re.IGNORECASE)
        if match:
            width = int(match.group(1))
            base = match.group(2).lower()
            digits = match.group(3).replace('_', '')

            if base == 'b':
                if not all(c in '01' for c in digits):
                    raise ValueError(f"Invalid binary literal: {lit}")
                value = int(digits, 2) if digits else 0
            elif base == 'h':
                if not all(c in '0123456789abcdefABCDEF' for c in digits):
                    raise ValueError(f"Invalid hex literal: {lit}")
                value = int(digits, 16) if digits else 0
            elif base == 'd':
                if not digits.isdigit():
                    raise ValueError(f"Invalid decimal literal: {lit}")
                value = int(digits) if digits else 0
            else:
                raise ValueError(f"Unknown literal format: {lit}")

            # Validate width
            max_val = (1 << width) - 1
            if value > max_val:
                raise ValueError(f"Value {value} too large for width {width}")

            return value, width

    return int(lit), 1


def evaluate_circuit(
    circuit: Circuit,
    inputs: dict[str, int],
    default: int = 0,
    allow_extra: bool = False,
    radix: str = "bin"
) -> dict[str, int]:
    """Evaluate a circuit with given input values."""

    # Validate input values
    for name, value in inputs.items():
        if not isinstance(value, int) or value < 0:
            raise InputValueParseError(f"Input '{name}' value must be a non-negative integer")

    # Check for unknown inputs
    if not allow_extra:
        for name in inputs:
            if name not in circuit.inputs:
                raise UnknownInputError(f"Unknown input '{name}'")

    # Check for missing inputs
    for name in circuit.inputs:
        if name not in inputs:
            raise MissingInputError(f"Missing input '{name}'")

    # Initialize signal values
    values: dict[str, int] = {}
    for name in circuit.inputs:
        values[name] = inputs.get(name, default)
    for name in circuit.wires:
        values[name] = 0
    for name in circuit.outputs:
        values[name] = 0

    def eval_expr(expr: tuple) -> int:
        """Evaluate an expression and return its integer value."""
        kind = expr[0]

        if kind == 'identifier':
            name = expr[1]
            return values[name]

        elif kind == 'literal':
            value, _ = parse_literal_value(expr[1])
            return value

        elif kind == 'bit_index':
            name = expr[1]
            index = expr[2]
            val = values[name]
            return (val >> index) & 1

        elif kind == 'slice':
            name = expr[1]
            hi = expr[2]
            lo = expr[3]
            val = values[name]
            # Shift right by lo and mask
            width = hi - lo + 1
            mask = (1 << width) - 1
            return (val >> lo) & mask

        elif kind == 'concatenation':
            result = 0
            bit_pos = 0
            # Process in reverse: e1 is MSB, so last arg in list is LSB
            for arg in reversed(expr[1]):
                arg_val = eval_expr(arg)
                # Get width of this argument
                if arg[0] == 'literal':
                    _, width = parse_literal_value(arg[1])
                elif arg[0] == 'identifier':
                    width = next((s.width for s in [circuit.outputs.get(arg[1]), circuit.wires.get(arg[1]), circuit.inputs.get(arg[1])] if s), 1)
                elif arg[0] == 'bit_index':
                    width = 1
                elif arg[0] == 'slice':
                    width = arg[2] - arg[3] + 1
                elif arg[0] == 'concatenation':
                    width = get_expr_width(arg, {**circuit.outputs, **circuit.wires, **circuit.inputs})
                else:
                    width = get_expr_width(arg, {**circuit.outputs, **circuit.wires, **circuit.inputs})
                result |= (arg_val << bit_pos)
                bit_pos += width
            return result

        elif kind == 'call':
            op_name = expr[1]
            args = [eval_expr(arg) for arg in expr[2]]

            if op_name == 'NOT':
                return ~args[0] & ((1 << args[0].bit_length()) - 1) if args[0] > 0 else 0
            elif op_name == 'BUF':
                return args[0]
            elif op_name == 'AND':
                result = args[0]
                for arg in args[1:]:
                    result &= arg
                return result
            elif op_name == 'OR':
                result = args[0]
                for arg in args[1:]:
                    result |= arg
                return result
            elif op_name == 'XOR':
                result = args[0]
                for arg in args[1:]:
                    result ^= arg
                return result
            elif op_name == 'NAND':
                result = args[0]
                for arg in args[1:]:
                    result &= arg
                return ~result
            elif op_name == 'NOR':
                result = args[0]
                for arg in args[1:]:
                    result |= arg
                return ~result
            elif op_name == 'XNOR':
                result = args[0]
                for arg in args[1:]:
                    result ^= arg
                return ~result
            elif op_name in ('MUX', 'ITE'):
                sel, a, b = args[0], args[1], args[2]
                return a if sel else b
            elif op_name == 'REDUCE_AND':
                # AND all bits - for a vector, this is 1 if all bits are 1
                val = args[0]
                # We need to know width; for now, check if any bit is 0
                while val > 1:
                    if (val & 1) == 0:
                        return 0
                    val >>= 1
                return 1 if args[0] > 0 else 1  # If all bits are 1, return 1
            elif op_name == 'REDUCE_OR':
                # OR all bits
                return 1 if args[0] != 0 else 0
            elif op_name == 'REDUCE_XOR':
                # XOR all bits (parity)
                val = args[0]
                result = 0
                while val > 0:
                    result ^= (val & 1)
                    val >>= 1
                return result
            elif op_name == 'EQ':
                return 1 if args[0] == args[1] else 0

        raise ValueError(f"Unknown expression: {expr}")

    # Topologically sort for evaluation
    deps: dict[str, set[str]] = {name: set() for name in {**circuit.wires, **circuit.outputs}}

    def collect_expr_deps(expr: tuple, signal_name: str):
        kind = expr[0]
        if kind == 'identifier':
            dep_name = expr[1]
            if dep_name in {**circuit.inputs, **circuit.wires, **circuit.outputs}:
                deps[signal_name].add(dep_name)
        elif kind == 'bit_index':
            dep_name = expr[1]
            if dep_name in {**circuit.inputs, **circuit.wires, **circuit.outputs}:
                deps[signal_name].add(dep_name)
        elif kind == 'slice':
            dep_name = expr[1]
            if dep_name in {**circuit.inputs, **circuit.wires, **circuit.outputs}:
                deps[signal_name].add(dep_name)
        elif kind == 'concatenation':
            for arg in expr[1]:
                collect_expr_deps(arg, signal_name)
        elif kind == 'call':
            for arg in expr[2]:
                collect_expr_deps(arg, signal_name)

    for lhs, rhs in circuit.assignments:
        if lhs in deps:
            collect_expr_deps(rhs, lhs)

    # Topological sort
    visited: set[str] = set()
    temp_visited: set[str] = set()
    order: list[str] = []

    def topological_sort(node: str):
        if node in temp_visited:
            raise CycleError([node])
        if node in visited:
            return
        temp_visited.add(node)
        for neighbor in deps.get(node, set()):
            if neighbor in {**circuit.wires, **circuit.outputs}:
                topological_sort(neighbor)
        temp_visited.remove(node)
        visited.add(node)
        order.append(node)

    for node in {**circuit.wires, **circuit.outputs}:
        if node not in visited:
            topological_sort(node)

    # Evaluate in order
    for name in order:
        for lhs, rhs in circuit.assignments:
            if lhs == name:
                values[name] = eval_expr(rhs)
                break

    # Return output values
    result = {}
    for name in sorted(circuit.outputs):
        result[name] = values[name]

    return result


# =============================================================================
# CLI
# =============================================================================

def format_output(value: int, msb: int, lsb: int, radix: str, command: str) -> str:
    """Format a value for output."""
    if msb == lsb:
        # Scalar
        return str(value)

    width = msb - lsb + 1
    if radix == 'bin':
        return f"0b{value:0{width}b}"
    elif radix == 'hex':
        # Calculate needed hex digits
        hex_digits = (width + 3) // 4
        return f"0x{value:0{hex_digits}x}"
    elif radix == 'dec':
        return str(value)
    else:
        return str(value)


def output_json(command: str, data: dict):
    """Output JSON to stdout."""
    print(json.dumps(data))


def output_plain(command: str, data: dict, radix: str = "bin"):
    """Output plain text to stdout."""
    if command == '__version__':
        print(data['version'])
    elif command == 'check':
        inputs = sorted(data['inputs'], key=lambda x: x['name'])
        outputs = sorted(data['outputs'], key=lambda x: x['name'])
        print(f"Inputs: {', '.join(i['name'] for i in inputs)}")
        print(f"Outputs: {', '.join(o['name'] for o in outputs)}")
    elif command == 'eval':
        for out in data['outputs']:
            name = out['name']
            msb = out['msb']
            lsb = out['lsb']
            value = int(out['value'])  # Parse the string value
            formatted = format_output(value, msb, lsb, radix, command)
            print(f"{name}={formatted}")


def handle_version(json_flag: bool):
    """Handle --version flag."""
    data = {
        "ok": True,
        "command": "__version__",
        "version": __version__
    }
    if json_flag:
        output_json("__version__", data)
    else:
        output_plain("__version__", data)
    return 0


def handle_eval(
    filepath: str,
    set_inputs: list[str],
    default: int,
    allow_extra: bool,
    json_flag: bool,
    filename: str,
    radix: str = "bin",
    mode: str = "2val",
    fmt: str = "auto"
) -> int:
    """Handle the eval command."""
    try:
        parsed = parse_file(filepath, fmt)
        circuit = validate_circuit(parsed, filepath)
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "eval",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("eval", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    # Parse --set arguments
    inputs: dict[str, int] = {}
    for item in set_inputs:
        if '=' not in item:
            if json_flag:
                print(json.dumps({
                    "ok": False,
                    "command": "eval",
                    "exit_code": 2,
                    "error": {
                        "type": "InputValueParseError",
                        "message": f"Invalid input format: '{item}' (expected name=value)",
                        "file": None,
                        "line": None,
                        "col": None
                    }
                }))
            else:
                print(f"Error: Invalid input format: '{item}' (expected name=value)")
            return 2
        name, value_str = item.split('=', 1)
        name = name.strip()
        value_str = value_str.strip()

        try:
            # Parse the input literal
            value, provided_width = parse_literal_value(value_str)

            # Check width matching
            if name in circuit.inputs:
                declared_width = circuit.inputs[name].width
                if provided_width != declared_width:
                    if json_flag:
                        error = InputWidthMismatchError(name, declared_width, provided_width)
                        print(json.dumps(error.to_json("eval", filepath)))
                    else:
                        print(f"Error: Width mismatch for input '{name}': declared width {declared_width}, provided width {provided_width}")
                    return 3

            inputs[name] = value
        except ValueError as e:
            if json_flag:
                print(json.dumps({
                    "ok": False,
                    "command": "eval",
                    "exit_code": 2,
                    "error": {
                        "type": "InputValueParseError",
                        "message": str(e),
                        "file": None,
                        "line": None,
                        "col": None
                    }
                }))
            else:
                print(f"Error: {e}")
            return 2
        except InputWidthMismatchError as e:
            if json_flag:
                print(json.dumps(e.to_json("eval", filepath)))
            else:
                print(f"Error: {e}")
            return 3

    # Evaluate the circuit
    try:
        results = evaluate_circuit(circuit, inputs, default, allow_extra, radix)
    except MissingInputError as e:
        if json_flag:
            print(json.dumps(e.to_json("eval", filepath)))
        else:
            print(f"Error: {e}")
        return 1
    except UnknownInputError as e:
        if json_flag:
            print(json.dumps(e.to_json("eval", filepath)))
        else:
            print(f"Error: {e}")
        return 1
    except InputValueParseError as e:
        if json_flag:
            print(json.dumps(e.to_json("eval", filepath)))
        else:
            print(f"Error: {e}")
        return 2
    except WidthMismatchError as e:
        if json_flag:
            print(json.dumps(e.to_json("eval", filepath)))
        else:
            print(f"Error: {e}")
        return 3

    # Output results
    outputs_data = []
    for name, value in results.items():
        signal = circuit.outputs[name]
        outputs_data.append({
            "name": name,
            "msb": signal.msb,
            "lsb": signal.lsb,
            "value": str(value)
        })

    data = {
        "ok": True,
        "command": "eval",
        "mode": "2val",
        "radix": radix,
        "outputs": outputs_data
    }

    if json_flag:
        output_json("eval", data)
    else:
        output_plain("eval", data, radix)

    return 0


def handle_check(filepath: str, json_flag: bool, filename: str, fmt: str = "auto") -> int:
    """Handle the check command."""
    try:
        parsed = parse_file(filepath, fmt)
        circuit = validate_circuit(parsed, filepath)
        # Determine actual format for output
        if fmt != "auto":
            detected_fmt = fmt
        elif filepath.endswith('.circ'):
            detected_fmt = "circ"
        elif filepath.endswith('.json'):
            detected_fmt = "json"
        elif filepath.endswith('.bench'):
            detected_fmt = "bench"
        else:
            detected_fmt = "circ"
    except FileNotFoundError as e:
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "check",
                "exit_code": 1,
                "error": {
                    "type": "FileNotFoundError",
                    "message": str(e),
                    "file": filepath,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: {e}")
        return 1
    except CircoptError as e:
        if json_flag:
            print(json.dumps(e.to_json("check", filepath)))
        else:
            print(f"Error: {e}")
        return e.exit_code

    # Success
    inputs = sorted([{"name": name, "msb": sig.msb, "lsb": sig.lsb} for name, sig in circuit.inputs.items()],
                    key=lambda x: x['name'])
    outputs = sorted([{"name": name, "msb": sig.msb, "lsb": sig.lsb} for name, sig in circuit.outputs.items()],
                     key=lambda x: x['name'])

    data = {
        "ok": True,
        "command": "check",
        "format": detected_fmt,
        "inputs": inputs,
        "outputs": outputs
    }

    if json_flag:
        output_json("check", data)
    else:
        output_plain("check", data)

    return 0


def extract_filename(args: list[str], ignore_flags: list[str]) -> Optional[str]:
    """Extract the filename from arguments, ignoring specified flags.

    Args:
        args: List of command-line arguments (excluding command)
        ignore_flags: List of flag names to ignore (e.g., ['--json', '--format'])

    Returns:
        The filename if found, None otherwise
    """
    # First, build a set of all flag values that should be skipped
    skip = set()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ignore_flags:
            # Skip this flag and its value (if it has one)
            skip.add(i)
            # Check if the next arg is a value (not a flag)
            if i + 1 < len(args) and args[i + 1] not in ignore_flags:
                skip.add(i + 1)
                i += 1  # Skip the value too
        i += 1

    # Find the first non-skipped argument that's not a flag
    for i, arg in enumerate(args):
        if i not in skip and not arg.startswith('-'):
            return arg

    return None


def parse_seed(args: list[str]) -> Optional[int]:
    """Parse --seed from arguments.

    Returns:
        The seed value if present, None otherwise

    Raises:
        CliUsageError: If seed is invalid (negative or non-integer)
    """
    for i, arg in enumerate(args):
        if arg == '--seed':
            if i + 1 >= len(args):
                raise CliUsageError("Missing value for --seed")
            try:
                seed_val = int(args[i + 1])
                if seed_val < 0:
                    raise CliUsageError(f"Invalid seed: {seed_val} (must be non-negative)")
                return seed_val
            except ValueError:
                raise CliUsageError(f"Invalid seed: '{args[i + 1]}' (must be an integer)")
    return None


def main():
    """Main entry point for the circopt CLI."""
    # Check for --version
    if '--version' in sys.argv:
        json_flag = '--json' in sys.argv
        return handle_version(json_flag)

    # Parse command
    args = sys.argv[1:]

    if len(args) == 0:
        print("Error: No command specified")
        print("Usage: python circopt.py <command> [options]")
        print("Commands: check, eval, stats, lint, dot")
        return 1

    command = args[0]

    # Parse --format flag if present
    fmt = "auto"
    format_idx = -1
    for i, arg in enumerate(args):
        if arg == '--format':
            format_idx = i
            break

    if format_idx >= 1 and format_idx + 1 < len(args):
        fmt_val = args[format_idx + 1]
        if fmt_val in ('auto', 'circ', 'json', 'bench'):
            fmt = fmt_val
        else:
            print(f"Error: Invalid format '{fmt_val}' (must be auto, circ, json, or bench)")
            return 1

    if command == 'check':
        json_flag = '--json' in args

        # Find the filename (exclude all flags)
        filename = extract_filename(args, ['--json', '--format'])

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        return handle_check(filename, json_flag, "check", fmt)

    elif command == 'stats':
        json_flag = '--json' in args

        filename = extract_filename(args, ['--json', '--format'])

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        return handle_stats(filename, json_flag, fmt)

    elif command == 'lint':
        json_flag = '--json' in args
        max_input_bits = 16  # default

        # Parse --max-input-bits
        max_bits_idx = -1
        for i, arg in enumerate(args):
            if arg == '--max-input-bits':
                max_bits_idx = i
                break

        if max_bits_idx >= 1 and max_bits_idx + 1 < len(args):
            try:
                max_input_bits = int(args[max_bits_idx + 1])
                if max_input_bits < 0:
                    print(f"Error: --max-input-bits must be non-negative, got '{args[max_bits_idx + 1]}'")
                    return 1
            except ValueError:
                print(f"Error: Invalid max-input-bits value: '{args[max_bits_idx + 1]}'")
                return 1

        filename = extract_filename(args, ['--json', '--format', '--max-input-bits'])

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        return handle_lint(filename, json_flag, max_input_bits, fmt)

    elif command == 'dot':
        json_flag = '--json' in args
        output_path = None
        cone = None

        # Parse -o option
        o_idx = -1
        for i, arg in enumerate(args):
            if arg == '-o':
                o_idx = i
                break

        if o_idx >= 1 and o_idx + 1 < len(args):
            output_path = args[o_idx + 1]
        else:
            print("Error: Missing required argument for dot: -o <file>")
            return 1

        # Parse --cone option
        cone_idx = -1
        for i, arg in enumerate(args):
            if arg == '--cone':
                cone_idx = i
                break

        if cone_idx >= 1 and cone_idx + 1 < len(args):
            cone = args[cone_idx + 1]

        filename = extract_filename(args, ['--json', '--format', '-o', '--cone'])

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        return handle_dot(filename, output_path, json_flag, cone, fmt)

    elif command == 'eval':
        json_flag = '--json' in args
        # Find the filename (exclude all flags)
        filename = None
        for arg in args[1:]:
            if arg != '--json' and arg != '--format':
                filename = arg
                break
            # Skip the format value if it's next
            if arg == '--format':
                # Will be handled in next iteration
                continue

        # Also remove format value from consideration
        if format_idx >= 1 and filename == args[format_idx + 1] if format_idx + 1 < len(args) else False:
            # Find next filename
            for arg in args[1:]:
                if arg not in ('--json', '--format'):
                    # Check if it's the format value
                    is_format_value = False
                    for i, a in enumerate(args):
                        if a == arg and i > 0 and args[i - 1] == '--format':
                            is_format_value = True
                            break
                    if not is_format_value:
                        filename = arg
                        break

        # Alternative approach: just filter out flags
        potential_files = [arg for arg in args[1:] if arg != '--json']
        # Remove --format and its value
        filtered = []
        skip_next = False
        for i, arg in enumerate(args[1:], 1):
            if skip_next:
                skip_next = False
                continue
            if arg == '--format':
                skip_next = True
                continue
            if arg != '--json':
                filtered.append(arg)

        filename = filtered[0] if filtered else None

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        return handle_check(filename, json_flag, "check", fmt)

    elif command == 'eval':
        json_flag = '--json' in args

        # Parse --radix and --mode options
        radix = "bin"  # default
        mode = "2val"  # default
        radix_idx = -1
        mode_idx = -1
        for i, arg in enumerate(args):
            if arg == '--radix':
                radix_idx = i
            elif arg == '--mode':
                mode_idx = i
        if radix_idx >= 1 and radix_idx + 1 < len(args):
            radix_val = args[radix_idx + 1]
            if radix_val in ('bin', 'hex', 'dec'):
                radix = radix_val
            else:
                print(f"Error: Invalid radix '{radix_val}' (must be bin, hex, or dec)")
                return 1

        if mode_idx >= 1 and mode_idx + 1 < len(args):
            mode_val = args[mode_idx + 1]
            if mode_val in ('2val', '3val'):
                mode = mode_val
            else:
                print(f"Error: Invalid mode '{mode_val}' (must be 2val or 3val)")
                return 1

        # Parse --set, --default, --allow-extra
        set_inputs: list[str] = []
        default = 0
        allow_extra = False

        i = 1
        while i < len(args):
            arg = args[i]
            if arg in ('--radix', '--mode', '--format'):
                i += 2
                continue
            elif arg == '--set':
                if i + 1 < len(args):
                    set_inputs.append(args[i + 1])
                    i += 2
                else:
                    print("Error: Missing value for --set")
                    return 1
            elif arg == '--default':
                if i + 1 < len(args):
                    try:
                        default = int(args[i + 1])
                        if default not in (0, 1):
                            print(f"Error: --default must be 0 or 1, got '{args[i + 1]}'")
                            return 2
                    except ValueError:
                        print(f"Error: --default must be an integer, got '{args[i + 1]}'")
                        return 2
                    i += 2
                else:
                    print("Error: Missing value for --default")
                    return 1
            elif arg == '--allow-extra':
                allow_extra = True
                i += 1
            elif arg == '--json':
                i += 1
            else:
                break

        # Get remaining filepath
        remaining = args[i:]
        filename = extract_filename(remaining, ['--json', '--format', '--radix', '--mode', '--set', '--default', '--allow-extra'])

        if filename is None:
            print("Error: Missing required argument: <file>")
            return 1

        # Check for radix restriction in 3val mode
        if mode == "3val" and radix in ('hex', 'dec'):
            if json_flag:
                error = RadixNotAllowedIn3ValError()
                print(json.dumps(error.to_json("eval", filename)))
            else:
                print(f"Error: {RadixNotAllowedIn3ValError()}")
            return 1

        return handle_eval(filename, set_inputs, default, allow_extra, json_flag, "eval", radix, mode, fmt)

    elif command == 'opt':
        return handle_opt(args)

    else:
        print(f"Error: Unknown command '{command}'")
        return 1


if __name__ == '__main__':
    sys.exit(main())
