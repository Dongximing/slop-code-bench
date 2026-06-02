#!/usr/bin/env python3
"""
circopt - Circuit optimizer and validator
"""

import argparse
import functools
import json
import sys
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum, auto


# Error Classes
class CircoptError(Exception):
    exit_code: int = 1
    error_type = "CircoptError"

    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self, command: str) -> dict:
        r = {"ok": False, "command": command, "exit_code": self.exit_code, "error": {"type": self.error_type, "message": self.message}}
        if self.file is not None: r["error"]["file"] = self.file
        if self.line is not None: r["error"]["line"] = self.line
        if self.col is not None: r["error"]["col"] = self.col
        return r

class CliUsageError(CircoptError):
    exit_code = 1; error_type = "CliUsageError"
    def __init__(self, message: str): super().__init__(message)

class CircParseError(CircoptError):
    exit_code = 2; error_type = "CircParseError"
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None): super().__init__(message, file, line, col)

class ValidationError(CircoptError):
    exit_code = 3; error_type = "ValidationError"
    def __init__(self, message: str, **kwargs): super().__init__(message, **kwargs)

class DeclarationAfterAssignmentError(ValidationError):
    def __init__(self, name: str = ""): super().__init__(f"Declaration after assignment: '{name}'" if name else "Declaration after assignment")

class DuplicateNameError(ValidationError):
    def __init__(self, name: str): super().__init__(f"Duplicate name: '{name}'")

class UndefinedNameError(ValidationError):
    def __init__(self, name: str): super().__init__(f"Undefined name: '{name}'")

class UnassignedSignalError(ValidationError):
    def __init__(self, name: str): super().__init__(f"Unassigned signal: '{name}'")

class InputAssignmentError(ValidationError):
    def __init__(self, name: str): super().__init__(f"Cannot assign to input: '{name}'")

class MultipleAssignmentError(ValidationError):
    def __init__(self, name: str): super().__init__(f"Multiple assignment to: '{name}'")

class ArityError(ValidationError):
    def __init__(self, op: str, expected: int, got: int): super().__init__(f"Arity error: {op} expects {expected}, got {got}")

class CycleError(ValidationError):
    def __init__(self, path: list): super().__init__(f"Cycle detected: {' -> '.join(path)}")


# Evaluation Errors
class MissingInputError(CircoptError):
    exit_code = 1; error_type = "MissingInputError"
    def __init__(self, name: str): super().__init__(f"Missing input: '{name}'")

class UnknownInputError(CircoptError):
    exit_code = 1; error_type = "UnknownInputError"
    def __init__(self, name: str): super().__init__(f"Unknown input: '{name}'")

class InputValueParseError(CircoptError):
    exit_code = 2; error_type = "InputValueParseError"
    def __init__(self, value: str): super().__init__(f"Invalid input value: '{value}'. Must be 0 or 1.")


# New Error Types for Part 3
class WidthMismatchError(CircoptError):
    exit_code = 3; error_type = "WidthMismatchError"
    def __init__(self, message: str): super().__init__(message)

class IndexOutOfBoundsError(CircoptError):
    exit_code = 3; error_type = "IndexOutOfBoundsError"
    def __init__(self, message: str): super().__init__(message)

class InputWidthMismatchError(CircoptError):
    exit_code = 3; error_type = "InputWidthMismatchError"
    def __init__(self, message: str): super().__init__(message)


# AST Nodes

class NodeType(Enum):
    INPUT = auto()
    OUTPUT = auto()
    WIRE = auto()
    ASSIGNMENT = auto()
    IDENTIFIER = auto()
    LITERAL = auto()
    CALL = auto()
    BIT_INDEX = auto()     # v[i]
    BIT_SLICE = auto()     # v[hi:lo]
    CONCAT = auto()        # {e1, e2, ...}


@dataclass
class Node:
    node_type: NodeType
    line: Optional[int] = None
    col: Optional[int] = None


@dataclass
class DeclarationNode(Node):
    names: list = field(default_factory=list)


@dataclass
class AssignmentNode(Node):
    lhs: str = ""
    rhs: 'ExprNode' = None  # type: ignore


@dataclass
class ExprNode(Node):
    value = ""
    args: list = field(default_factory=list)


# ============================================================================
# Tokenizer
# ============================================================================

class TokenType(Enum):
    IDENTIFIER = auto()
    NUMBER = auto()
    OP = auto()  # NOT, AND, OR, etc.
    LITERAL = auto()  # Literal values (0, 1, 0b..., 0x..., sized)
    EQUALS = auto()
    COMMA = auto()
    LPAREN = auto()
    RPAREN = auto()
    LBRACKET = auto()   # [
    RBRACKET = auto()   # ]
    COLON = auto()      # :
    LBRACE = auto()     # {
    RBRACE = auto()     # }
    WHITESPACE = auto()
    COMMENT = auto()
    EOF = auto()


class Token:
    __slots__ = ('type', 'value', 'line', 'col')

    def __init__(self, type: TokenType, value: str, line: int, col: int):
        self.type = type
        self.value = value
        self.line = line
        self.col = col


class Tokenizer:
    """Tokenizer for .circ files."""

    OPS = {'INPUT', 'OUTPUT', 'WIRE', 'NOT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'BUF'}

    def __init__(self, text: str, filename: Optional[str] = None):
        self.text = text
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1

    def peek(self) -> Optional[str]:
        if self.pos < len(self.text):
            return self.text[self.pos]
        return None

    def advance(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        if ch == '\n':
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def skip_whitespace_and_comments(self):
        while self.pos < len(self.text):
            ch = self.peek()
            if ch.isspace():
                self.advance()
            elif ch == '#':
                # Skip comment until end of line
                while self.pos < len(self.text) and self.peek() != '\n':
                    self.advance()
            else:
                break

    def tokenize_identifier(self, first_char: str):
        start_line = self.line
        start_col = self.col - len(first_char) - 1 if self.col > 1 else self.line
        value = first_char
        while self.pos < len(self.text):
            ch = self.peek()
            if ch.isalnum() or ch == '_':
                value += self.advance()
            else:
                break
        return Token(TokenType.OP if value.upper() in self.OPS else TokenType.IDENTIFIER, value, start_line, start_col)

    def tokenize_number(self, first_char: str):
        start_line = self.line
        start_col = self.col - 1
        value = first_char
        while self.pos < len(self.text):
            ch = self.peek()
            if ch.isdigit():
                value += self.advance()
            else:
                break
        return Token(TokenType.NUMBER, value, start_line, start_col)

    def tokenize_binary_hex_literal(self, prefix_char: str):
        """Tokenize a binary (0b) or hex (0x) literal."""
        start_line = self.line
        start_col = self.col - 1
        value = '0' + prefix_char
        # Advance past the prefix
        self.advance()  # consume 'b' or 'x'

        # Collect digits (allowing underscore separator)
        has_digit = False
        while self.pos < len(self.text):
            ch = self.peek()
            if ch.isalnum() or ch == '_':
                if ch.isalnum():
                    has_digit = True
                value += self.advance()
            else:
                break

        if not has_digit:
            raise CircParseError(
                f"Unsized literal must have at least one digit after prefix '{value[:2]}'",
                self.filename, start_line, start_col
            )

        return Token(TokenType.LITERAL, value, start_line, start_col)

    def next_token(self) -> Token:
        self.skip_whitespace_and_comments()

        if self.pos >= len(self.text):
            return Token(TokenType.EOF, '', self.line, self.col)

        ch = self.peek()
        start_line = self.line
        start_col = self.col

        if ch.isalpha() or ch == '_':
            return self.tokenize_identifier(self.advance())
        elif ch.isdigit():
            # Check for binary (0b) or hex (0x) literals
            if ch == '0' and self.pos + 1 < len(self.text):
                next_ch = self.text[self.pos + 1]
                if next_ch in ('b', 'x', 'B', 'X'):
                    return self.tokenize_binary_hex_literal(next_ch.lower())
            return self.tokenize_number(self.advance())
        elif ch == '=':
            self.advance()
            return Token(TokenType.EQUALS, '=', start_line, start_col)
        elif ch == ',':
            self.advance()
            return Token(TokenType.COMMA, ',', start_line, start_col)
        elif ch == '(':
            self.advance()
            return Token(TokenType.LPAREN, '(', start_line, start_col)
        elif ch == ')':
            self.advance()
            return Token(TokenType.RPAREN, ')', start_line, start_col)
        elif ch == '[':
            self.advance()
            return Token(TokenType.LBRACKET, '[', start_line, start_col)
        elif ch == ']':
            self.advance()
            return Token(TokenType.RBRACKET, ']', start_line, start_col)
        elif ch == ':':
            self.advance()
            return Token(TokenType.COLON, ':', start_line, start_col)
        elif ch == '{':
            self.advance()
            return Token(TokenType.LBRACE, '{', start_line, start_col)
        elif ch == '}':
            self.advance()
            return Token(TokenType.RBRACE, '}', start_line, start_col)
        else:
            raise CircParseError(
                f"Unexpected character: '{ch}'",
                self.filename, start_line, start_col
            )

    def tokenize(self):
        tokens = []
        while True:
            token = self.next_token()
            tokens.append(token)
            if token.type == TokenType.EOF:
                break
        return tokens


# ============================================================================
# Parser
# ============================================================================

class Parser:

    def __init__(self, tokens: list, filename: Optional[str] = None):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0
        self.current_line = 1

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def advance(self) -> Token:
        token = self.peek()
        if token is not None:
            self.pos += 1
            self.current_line = token.line
        return token

    def expect(self, expected_type: TokenType, error_msg: str) -> Token:
        token = self.peek()
        if token is None:
            raise CircParseError(
                error_msg,
                self.filename, self.current_line, 1
            )
        if token.type != expected_type:
            raise CircParseError(
                f"{error_msg}, got {token.type.name}: '{token.value}'",
                self.filename, token.line, token.col
            )
        return self.advance()

    def parse_expr(self) -> ExprNode:
        """Parse an expression: identifier, literal, function call, bit index/slice, or concat."""
        token = self.peek()
        if token is None:
            raise CircParseError(
                "Unexpected end of file in expression",
                self.filename, self.current_line, 1
            )

        if token.type == TokenType.IDENTIFIER:
            self.advance()
            # Check for bit index or slice
            if self.peek() and self.peek().type == TokenType.LBRACKET:
                return self.parse_bit_index_or_slice(token.line, token.col, token.value)
            return ExprNode(NodeType.IDENTIFIER, token.line, token.col, token.value)
        elif token.type == TokenType.NUMBER:
            self.advance()
            # Scalar literals (0 or 1)
            if token.value in ('0', '1'):
                return ExprNode(NodeType.LITERAL, token.line, token.col, token.value)
            else:
                raise CircParseError(
                    f"Invalid literal: {token.value}. Only 0 and 1 are allowed.",
                    self.filename, token.line, token.col
                )
        elif token.type == TokenType.LITERAL:
            self.advance()
            return ExprNode(NodeType.LITERAL, token.line, token.col, token.value)
        elif token.type == TokenType.OP:
            # Could be a binary/hex literal or a function call
            op_name = token.value.upper()
            if op_name in self.OPS or op_name in {'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}:
                return self.parse_call()
            else:
                # This is a sized literal (e.g., 8'd200, 8'b00001111, 8'hff)
                if self.is_sized_literal(token.value):
                    return ExprNode(NodeType.LITERAL, token.line, token.col, token.value)
                raise CircParseError(
                    f"Unknown operator: {op_name}",
                    self.filename, token.line, token.col
                )
        elif token.type == TokenType.LBRACE:
            return self.parse_concat()
        else:
            raise CircParseError(
                f"Unexpected token in expression: {token.type.name}: '{token.value}'",
                self.filename, token.line, token.col
            )

    def is_sized_literal(self, value: str) -> bool:
        """Check if a token value is a sized literal (e.g., 8'd200, 8'b00001111, 8'hff)."""
        import re
        # Match patterns like 8'd200, 8'b00001111, 8'hff
        pattern = r'^\d+[b-h](.+)$'
        return bool(re.match(pattern, value))

    def parse_bit_index_or_slice(self, line: int, col: int, name: str) -> ExprNode:
        """Parse bit index v[i] or slice v[hi:lo]."""
        self.expect(TokenType.LBRACKET, f"Expected '[' after identifier '{name}'")

        # Parse the index or slice bounds
        index_token = self.expect(TokenType.NUMBER, f"Expected index number")
        index = int(index_token.value)

        # Check if it's a slice or single index
        if self.peek() and self.peek().type == TokenType.COLON:
            self.advance()  # consume ':'
            lo_token = self.expect(TokenType.NUMBER, f"Expected low bound")
            lo = int(lo_token.value)
            self.expect(TokenType.RBRACKET, f"Expected ']' after slice")
            return ExprNode(NodeType.BIT_SLICE, line, col, (name, index, lo))
        else:
            self.expect(TokenType.RBRACKET, f"Expected ']' after index")
            return ExprNode(NodeType.BIT_INDEX, line, col, (name, index))

    def parse_concat(self) -> ExprNode:
        """Parse concatenation expression {e1, e2, ..., ek}."""
        start_line = self.current_line
        start_col = self.peek().col if self.peek() else 1

        self.expect(TokenType.LBRACE, f"Expected '{{' for concatenation")

        args = []
        while True:
            arg = self.parse_expr()
            args.append(arg)

            next_token = self.peek()
            if next_token is None:
                raise CircParseError(
                    "Unexpected end of file, expected '}'",
                    self.filename, start_line, start_col
                )
            if next_token.type == TokenType.RBRACE:
                self.advance()
                break
            elif next_token.type == TokenType.COMMA:
                self.advance()
                continue
            else:
                raise CircParseError(
                    f"Expected ',' or '}}', got {next_token.type.name}: '{next_token.value}'",
                    self.filename, next_token.line, next_token.col
                )

        return ExprNode(NodeType.CONCAT, start_line, start_col, args)

    def parse_call(self) -> ExprNode:
        """Parse a function call like AND(a, b)."""
        op_token = self.expect(TokenType.OP, f"Expected operator name")
        op_name = op_token.value.upper()

        self.expect(TokenType.LPAREN, f"Expected '(' after operator '{op_name}'")

        args = []
        while True:
            arg = self.parse_expr()
            args.append(arg)

            next_token = self.peek()
            if next_token is None:
                raise CircParseError(
                    f"Unexpected end of file, expected ')'",
                    self.filename, op_token.line, op_token.col
                )
            if next_token.type == TokenType.RPAREN:
                self.advance()
                break
            elif next_token.type == TokenType.COMMA:
                self.advance()
                continue
            else:
                raise CircParseError(
                    f"Expected ',' or ')', got {next_token.type.name}: '{next_token.value}'",
                    self.filename, next_token.line, next_token.col
                )

        return ExprNode(NodeType.CALL, op_token.line, op_token.col, op_name, args)

    def parse_declaration(self) -> DeclarationNode:
        """Parse a declaration line: input a b c, output y, wire t1 t2."""
        type_token = self.expect(TokenType.OP, "Expected declaration type (input, output, wire)")
        type_upper = type_token.value.upper()

        if type_upper not in {'INPUT', 'OUTPUT', 'WIRE'}:
            raise CircParseError(
                f"Expected declaration type (input, output, wire), got '{type_token.value}'",
                self.filename, type_token.line, type_token.col
            )

        names = []
        while True:
            token = self.peek()
            if token is None or token.type == TokenType.EOF:
                break
            # Only consume identifiers on the same line
            if token.type == TokenType.IDENTIFIER and token.line == type_token.line:
                self.advance()
                name = token.value

                # Check for optional vector declaration [msb:lsb]
                msb = lsb = 0  # Default to scalar
                if self.peek() and self.peek().type == TokenType.LBRACKET:
                    self.expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
                    msb_token = self.expect(TokenType.NUMBER, f"Expected MSB for '{name}'")
                    msb = int(msb_token.value)
                    self.expect(TokenType.COLON, f"Expected ':' after MSB for '{name}'")
                    lsb_token = self.expect(TokenType.NUMBER, f"Expected LSB for '{name}'")
                    lsb = int(lsb_token.value)
                    self.expect(TokenType.RBRACKET, f"Expected ']' after LSB for '{name}'")

                    if msb < lsb:
                        raise CircParseError(
                            f"MSB must be >= LSB for '{name}'",
                            self.filename, token.line, token.col
                        )
                    if lsb < 0:
                        raise CircParseError(
                            f"LSB must be >= 0 for '{name}'",
                            self.filename, token.line, token.col
                        )

                names.append((name, msb, lsb))
            else:
                break

        if not names:
            raise CircParseError(
                f"Expected names after declaration type",
                self.filename, type_token.line, type_token.col
            )

        node_type = (NodeType.INPUT if type_upper == "INPUT" else
                     NodeType.OUTPUT if type_upper == "OUTPUT" else NodeType.WIRE)
        return DeclarationNode(node_type, type_token.line, type_token.col, names)

    def parse_assignment(self) -> AssignmentNode:
        """Parse an assignment line: lhs = rhs."""
        lhs_token = self.expect(TokenType.IDENTIFIER, "Expected LHS identifier")
        lhs = lhs_token.value

        self.expect(TokenType.EQUALS, f"Expected '=' after LHS '{lhs}'")

        rhs = self.parse_expr()

        return AssignmentNode(NodeType.ASSIGNMENT, lhs_token.line, lhs_token.col, lhs, rhs)

    def parse_line(self) -> Optional[Node]:
        """Parse a single statement."""
        token = self.peek()
        if token is None or token.type == TokenType.EOF:
            return None

        # Check if this is a declaration
        if token.type == TokenType.OP:
            op_upper = token.value.upper()
            if op_upper in {'INPUT', 'OUTPUT', 'WIRE'}:
                return self.parse_declaration()

        # Default to assignment
        return self.parse_assignment()

    def parse(self) -> list:
        """Parse the entire file."""
        statements = []
        while self.pos < len(self.tokens):
            stmt = self.parse_line()
            if stmt is not None:
                statements.append(stmt)
        return statements


# ============================================================================
# Validator
# ============================================================================

@dataclass
class SignalInfo:
    """Information about a signal, including its width."""
    name: str
    msb: int
    lsb: int
    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass
class Circuit:
    """Represents a parsed and validated circuit."""
    inputs: list = field(default_factory=list)   # list of SignalInfo
    outputs: list = field(default_factory=list)  # list of SignalInfo
    wires: list = field(default_factory=list)    # list of SignalInfo
    assignments: dict = field(default_factory=dict)  # name -> ExprNode
    all_signals: dict = field(default_factory=dict)  # name -> SignalInfo


class Validator:
    """Validates parsed circuit statements."""

    SUPPORTED_OPS = {
        'NOT': 1,
        'BUF': 1,
        'AND': 'multiple',
        'OR': 'multiple',
        'XOR': 'multiple',
        'NAND': 'multiple',
        'NOR': 'multiple',
        'XNOR': 'multiple',
        'MUX': 3,
        'ITE': 3,
        'REDUCE_AND': 1,
        'REDUCE_OR': 1,
        'REDUCE_XOR': 1,
        'EQ': 2,
    }

    def __init__(self, filename: Optional[str] = None):
        self.filename = filename
        self.circuit = Circuit()
        self.assignments_seen = False  # Have we seen any assignment yet?

    def validate(self, statements: list) -> Circuit:
        """Validate all statements and return the circuit."""
        # First pass: collect declarations
        for stmt in statements:
            if isinstance(stmt, DeclarationNode):
                if self.assignments_seen:
                    raise DeclarationAfterAssignmentError(stmt.names[0] if stmt.names else "")
                self._process_declaration(stmt)

        # Second pass: process assignments
        for stmt in statements:
            if isinstance(stmt, AssignmentNode):
                self.assignments_seen = True
                self._process_assignment(stmt)

        # Check that all signals are assigned
        self._check_all_assigned()

        # Check for cycles
        self._check_cycles()

        return self.circuit

    def _process_declaration(self, decl: DeclarationNode):
        """Process a declaration statement."""
        for name, msb, lsb in decl.names:
            if name in self.circuit.all_signals:
                raise DuplicateNameError(name)

            signal_info = SignalInfo(name, msb, lsb)
            self.circuit.all_signals[name] = signal_info

            if decl.node_type == NodeType.INPUT:
                self.circuit.inputs.append(signal_info)
            elif decl.node_type == NodeType.OUTPUT:
                self.circuit.outputs.append(signal_info)
            else:  # WIRE
                self.circuit.wires.append(signal_info)

    def _process_assignment(self, assign: AssignmentNode):
        """Process an assignment statement."""
        lhs = assign.lhs

        # Check LHS is declared
        if lhs not in self.circuit.all_signals:
            raise UndefinedNameError(lhs)

        # Check LHS is not an input
        if lhs in self.circuit.inputs:
            raise InputAssignmentError(lhs)

        # Check not already assigned
        if lhs in self.circuit.assignments:
            raise MultipleAssignmentError(lhs)

        # Validate the RHS expression
        self._validate_expr(assign.rhs)

        self.circuit.assignments[lhs] = assign.rhs

    def _validate_expr(self, expr: ExprNode):
        """Validate an expression."""
        if expr.node_type == NodeType.IDENTIFIER:
            if expr.value not in self.circuit.all_signals:
                raise UndefinedNameError(expr.value)
        elif expr.node_type == NodeType.LITERAL:
            pass  # Literal already validated during parsing
        elif expr.node_type == NodeType.CALL:
            op = expr.value.upper()
            if op not in self.SUPPORTED_OPS:
                raise CircParseError(f"Unknown operator: {op}", self.filename, expr.line, expr.col)

            expected = self.SUPPORTED_OPS[op]
            got = len(expr.args)
            if got < expected if op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR') else got != expected:
                raise ArityError(op, f"≥ {expected}" if op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR') else expected, got)

            for arg in expr.args:
                self._validate_expr(arg)

    def _check_all_assigned(self):
        """Check that all wires and outputs are assigned."""
        for name in self.circuit.wires + self.circuit.outputs:
            if name not in self.circuit.assignments:
                raise UnassignedSignalError(name)

    def _check_cycles(self):
        """Check for cycles in the assignment graph."""
        visited = set()
        rec_stack = set()
        path = []

        def dfs(node: str):
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            if node in self.circuit.assignments:
                expr = self.circuit.assignments[node]
                deps = _get_identifiers(expr)
                for dep in deps:
                    if dep not in self.circuit.all_signals:
                        continue
                    if dep not in visited:
                        if dfs(dep):
                            return True
                    elif dep in rec_stack:
                        # Found a cycle
                        cycle_start = path.index(dep)
                        cycle_path = path[cycle_start:] + [dep]
                        raise CycleError(cycle_path)

            path.pop()
            rec_stack.remove(node)
            return False

        # Start DFS from each unvisited node that's an output or wire
        for name in self.circuit.outputs + self.circuit.wires:
            if name not in visited:
                if dfs(name):
                    return

def _get_identifiers(expr: ExprNode) -> list:
    """Get all identifier names in an expression."""
    if expr.node_type == NodeType.IDENTIFIER:
        return [expr.value]
    if expr.node_type == NodeType.LITERAL:
        return []
    if expr.node_type == NodeType.CALL:
        return [name for arg in expr.args for name in _get_identifiers(arg)]
    return []


# ============================================================================
# 2-Valued Evaluator
# ============================================================================

def evaluate_circuit(circuit: Circuit, inputs: dict, default: int = 0) -> dict:
    """
    Evaluate a circuit with given input values using 2-valued Boolean logic.

    Args:
        circuit: The validated circuit
        inputs: Dict mapping input names to values (0 or 1)
        default: Default value for unspecified inputs (0 or 1)

    Returns:
        Dict mapping output names to evaluated values (0 or 1)
    """
    # Validate all specified inputs are valid
    for name in inputs:
        if name not in circuit.inputs:
            raise UnknownInputError(name)

    # Check for missing inputs
    for name in circuit.inputs:
        if name not in inputs:
            raise MissingInputError(name)

    # Validate input values are 0 or 1
    for name, value in inputs.items():
        if value not in (0, 1):
            raise InputValueParseError(str(value))

    # Build signal values map
    # Start with inputs
    signal_values = {}
    for name in circuit.inputs:
        signal_values[name] = inputs.get(name, default)

    # Evaluate in topological order (by repeatedly evaluating until stable)
    # Since the circuit is validated and acyclic, we can keep evaluating
    # until all outputs are computed
    changed = True
    max_iterations = len(circuit.all_signals) + 1
    iteration = 0

    while changed and iteration < max_iterations:
        changed = False
        iteration += 1

        # Evaluate each assignment
        for name, expr in circuit.assignments.items():
            if name in signal_values:
                continue  # Already computed

            # Check if all dependencies are available
            deps = _get_identifiers(expr)
            can_evaluate = True
            for dep in deps:
                if dep not in signal_values:
                    can_evaluate = False
                    break

            if can_evaluate:
                result = _eval_expr(expr, signal_values)
                signal_values[name] = result
                changed = True

    # Get output values
    outputs = {}
    for name in circuit.outputs:
        if name not in signal_values:
            # This shouldn't happen with a valid circuit
            raise UnassignedSignalError(name)
        outputs[name] = signal_values[name]

    return outputs


_OP_FUNCS = {'NOT': lambda x: 1 - x,
             'BUF': lambda x: x,
             'AND': lambda args: functools.reduce(int.__and__, args, 1),
             'OR': lambda args: functools.reduce(int.__or__, args, 0),
             'XOR': lambda args: functools.reduce(int.__xor__, args, 0),
             'NAND': lambda args: 1 - functools.reduce(int.__and__, args, 1),
             'NOR': lambda args: 1 - functools.reduce(int.__or__, args, 0),
             'XNOR': lambda args: 1 - functools.reduce(int.__xor__, args, 0)}


def _eval_expr(expr: ExprNode, values: dict) -> int:
    if expr.node_type == NodeType.IDENTIFIER:
        return values[expr.value]
    if expr.node_type == NodeType.LITERAL:
        return int(expr.value)
    if expr.node_type == NodeType.CALL:
        args = [_eval_expr(arg, values) for arg in expr.args]
        return _OP_FUNCS[expr.value](args)
    raise ValueError(f"Unknown node type: {expr.node_type}")


def _process_circuit(filename, args, output_handler):
    """Shared pipeline: read file, tokenize, parse, validate."""
    try:
        with open(filename, 'r') as f:
            text = f.read()
    except FileNotFoundError:
        error = CliUsageError(f"File not found: {filename}")
        print(json.dumps(error.to_dict(args.command)), file=sys.stderr)
        return 1

    tokenizer = Tokenizer(text, filename)
    try:
        tokens = tokenizer.tokenize()
    except CircParseError as e:
        if e.file is None:
            e.file = filename
        print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
        return e.exit_code

    parser = Parser(tokens, filename)
    try:
        statements = parser.parse()
    except CircParseError as e:
        if e.file is None:
            e.file = filename
        print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
        return e.exit_code

    validator = Validator(filename)
    try:
        circuit = validator.validate(statements)
    except CircoptError as e:
        print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
        return e.exit_code

    return output_handler(circuit, args)


def eval_command(args) -> int:
    """Implement the 'eval' command."""
    def handler(circuit, args):
        # Parse --set arguments
        inputs = {}
        if args.set:
            for s in args.set:
                if '=' not in s:
                    error = CliUsageError(f"Invalid --set format: '{s}'. Expected name=value")
                    print(json.dumps(error.to_dict("eval")), file=sys.stderr)
                    return 1
                name, value_str = s.split('=', 1)
                name = name.strip()
                value_str = value_str.strip()
                try:
                    value = int(value_str)
                except ValueError:
                    error = InputValueParseError(value_str)
                    print(json.dumps(error.to_dict("eval")), file=sys.stderr)
                    return error.exit_code
                inputs[name] = value

        # Evaluate
        try:
            default = 1 if args.default is True else 0
            outputs = evaluate_circuit(circuit, inputs, default=default)
        except CircoptError as e:
            print(json.dumps(e.to_dict("eval")), file=sys.stderr)
            return e.exit_code

        # Output results
        if args.json:
            result = {
                "ok": True,
                "command": "eval",
                "mode": "2val",
                "radix": "bin",
                "outputs": [
                    {"name": name, "msb": 0, "lsb": 0, "value": str(value)}
                    for name, value in sorted(outputs.items())
                ]
            }
            print(json.dumps(result))
        else:
            for name in sorted(outputs.keys()):
                print(f"{name}={outputs[name]}")

        return 0

    return _process_circuit(args.file, args, handler)


# ============================================================================
# Main Application
# ============================================================================

VERSION = "0.1.0"


def check_command(args) -> int:
    """Implement the 'check' command."""

    def handler(circuit, _):
        inputs = sorted([{"name": name, "msb": 0, "lsb": 0} for name in circuit.inputs],
                        key=lambda x: x["name"])
        outputs = sorted([{"name": name, "msb": 0, "lsb": 0} for name in circuit.outputs],
                         key=lambda x: x["name"])
        result = {"ok": True, "command": "check", "format": "circ", "inputs": inputs, "outputs": outputs}
        print(json.dumps(result))
        return 0

    return _process_circuit(args.file, args, handler)


def version_command(args) -> int:
    """Handle --version flag."""
    if args.json:
        print(json.dumps({"ok": True, "command": "__version__", "version": VERSION}))
    else:
        print(VERSION)
    return 0


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog='circopt',
        description='Circuit optimizer and validator',
        add_help=False,
    )

    # Global flags
    parser.add_argument('--help', action='store_true', help='Show help message')
    parser.add_argument('--version', action='store_true', help='Show version')
    parser.add_argument('--json', action='store_true', help='Output in JSON format')

    # Parse known args to check for command
    args, remaining = parser.parse_known_args(sys.argv[1:])

    # Handle --help
    if args.help:
        if args.json:
            # JSON mode - still print plain text help to stdout
            parser.print_help()
        else:
            parser.print_help()
        return 0

    # Handle --version
    if args.version:
        return version_command(args)

    # Check if there's a command
    if not remaining:
        error = CliUsageError("No command specified. Use 'check <file.circ>' to validate a circuit file.")
        print(json.dumps(error.to_dict("__cli__")), file=sys.stderr)
        return 1

    command = remaining[0]

    if command == 'check':
        # Parse check-specific args
        check_parser = argparse.ArgumentParser(prog='circopt check', add_help=False)
        check_parser.add_argument('file', help='Circuit file to check')
        check_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        check_args, _ = check_parser.parse_known_args(remaining[1:])
        return check_command(check_args)

    elif command == 'eval':
        # Parse eval-specific args
        eval_parser = argparse.ArgumentParser(prog='circopt eval', add_help=False)
        eval_parser.add_argument('file', help='Circuit file to evaluate')
        eval_parser.add_argument('--set', dest='set_val', action='append', default=[],
                                 help='Set input value (name=value)')
        eval_parser.add_argument('--default', type=str, choices=['0', '1'],
                                 help='Default value for unspecified inputs')
        eval_parser.add_argument('--allow-extra', action='store_true',
                                 help='Allow extra inputs (not used in this implementation)')
        eval_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        eval_args, _ = eval_parser.parse_known_args(remaining[1:])

        # Convert --default string to boolean for internal use
        if eval_args.default is not None:
            eval_args.default = (eval_args.default == '1')
        else:
            eval_args.default = False  # Will be treated as 0

        return eval_command(eval_args)

    else:
        error = CliUsageError(f"Unknown command: {command}")
        print(json.dumps(error.to_dict("__cli__")), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
