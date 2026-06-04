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
                        # 0b... literal
                        self.pos += 2
                        self.col += 2
                        # Consume binary digits
                        while self.pos < len(self.text) and self.text[self.pos] in ('0', '1', '_'):
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


def parse_file(filepath: str) -> dict:
    """Parse a .circ file and return the parsed result."""
    try:
        with open(filepath, 'r') as f:
            text = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {filepath}")

    tokenizer = Tokenizer(text, filepath)
    tokens = tokenizer.tokenize()

    parser = Parser(tokens, filepath)
    return parser.parse()


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
    radix: str = "bin"
) -> int:
    """Handle the eval command."""
    try:
        parsed = parse_file(filepath)
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


def handle_check(filepath: str, json_flag: bool, filename: str) -> int:
    """Handle the check command."""
    try:
        parsed = parse_file(filepath)
        circuit = validate_circuit(parsed, filepath)
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
        "format": "circ",
        "inputs": inputs,
        "outputs": outputs
    }

    if json_flag:
        output_json("check", data)
    else:
        output_plain("check", data)

    return 0


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
        print("Commands: check, eval")
        return 1

    command = args[0]

    if command == 'check':
        json_flag = '--json' in args
        # Find the filename
        filename = None
        for arg in args[1:]:
            if arg != '--json':
                filename = arg
                break

        if filename is None:
            print("Error: Missing required argument: <file.circ>")
            return 1

        return handle_check(filename, json_flag, "check")

    elif command == 'eval':
        json_flag = '--json' in args

        # Parse --radix option
        radix = "bin"  # default
        radix_idx = -1
        for i, arg in enumerate(args):
            if arg == '--radix':
                radix_idx = i
                break
        if radix_idx >= 1 and radix_idx + 1 < len(args):
            radix_val = args[radix_idx + 1]
            if radix_val in ('bin', 'hex', 'dec'):
                radix = radix_val
            else:
                print(f"Error: Invalid radix '{radix_val}' (must be bin, hex, or dec)")
                return 1

        # Parse other options
        set_inputs: list[str] = []
        default = 0
        allow_extra = False
        filepath = None

        i = 1
        while i < len(args):
            arg = args[i]
            if arg == '--radix':
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
                filepath = arg
                break

        if filepath is None:
            print("Error: Missing required argument: <file.circ>")
            return 1

        return handle_eval(filepath, set_inputs, default, allow_extra, json_flag, "eval", radix)

    else:
        print(f"Error: Unknown command '{command}'")
        return 1


if __name__ == '__main__':
    sys.exit(main())
