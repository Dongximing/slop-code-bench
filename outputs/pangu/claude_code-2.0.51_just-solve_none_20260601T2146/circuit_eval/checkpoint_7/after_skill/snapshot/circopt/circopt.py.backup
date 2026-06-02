#!/usr/bin/env python3
"""circopt - Circuit optimizer and validator."""

import argparse
import functools
import json
import re
import sys
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ==============================================================================
# Error Handling
# ==============================================================================

class CircoptError(Exception):
    """Base error with JSON serialization."""
    exit_code = 1
    error_type = "CircoptError"

    def __init__(self, message, file=None, line=None, col=None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self, command):
        d = {"ok": False, "command": command, "exit_code": self.exit_code,
             "error": {"type": self.error_type, "message": self.message}}
        if self.file: d["error"]["file"] = self.file
        if self.line: d["error"]["line"] = self.line
        if self.col: d["error"]["col"] = self.col
        return d


class CliUsageError(CircoptError):
    exit_code = 1; error_type = "CliUsageError"


class CircParseError(CircoptError):
    exit_code = 2; error_type = "CircParseError"


class MissingInputError(CircoptError):
    exit_code = 1; error_type = "MissingInputError"


class UnknownInputError(CircoptError):
    exit_code = 1; error_type = "UnknownInputError"


class InputValueParseError(CircoptError):
    exit_code = 2; error_type = "InputValueParseError"


class WidthMismatchError(CircoptError):
    exit_code = 3; error_type = "WidthMismatchError"


class IndexOutOfBoundsError(CircoptError):
    exit_code = 3; error_type = "IndexOutOfBoundsError"


class InputWidthMismatchError(CircoptError):
    exit_code = 3; error_type = "InputWidthMismatchError"


class RadixNotAllowedIn3ValError(CircoptError):
    exit_code = 1; error_type = "RadixNotAllowedIn3ValError"


class UnknownInputFormatError(CircoptError):
    exit_code = 1; error_type = "UnknownInputFormatError"


class JsonParseError(CircoptError):
    exit_code = 2; error_type = "JsonParseError"


class JsonSchemaError(CircoptError):
    exit_code = 3; error_type = "JsonSchemaError"


class BenchParseError(CircoptError):
    exit_code = 2; error_type = "BenchParseError"


class RedefinitionError(CircoptError):
    exit_code = 3; error_type = "RedefinitionError"


class ValidationError(CircoptError):
    exit_code = 3; error_type = "ValidationError"


class UnknownOutputFormatError(CircoptError):
    exit_code = 1; error_type = "UnknownOutputFormatError"


class UnsupportedFeatureError(CircoptError):
    exit_code = 1; error_type = "UnsupportedFeatureError"


class InputLimitExceeded(CircoptError):
    exit_code = 1; error_type = "InputLimitExceeded"


class PortMismatchError(CircoptError):
    exit_code = 3; error_type = "PortMismatchError"


class UndefinedNameError(CircoptError):
    exit_code = 3; error_type = "UndefinedNameError"


def decl_after_assign(n=""): return ValidationError("Declaration after assignment: '" + n + "'" if n else "Declaration after assignment")
def duplicate_name(n): return ValidationError("Duplicate name: '" + n + "'")
def undefined_name(n): return ValidationError("Undefined name: '" + n + "'")
def unassigned_signal(n): return ValidationError("Unassigned signal: '" + n + "'")
def input_assignment(n): return ValidationError("Cannot assign to input: '" + n + "'")
def multiple_assignment(n): return ValidationError("Multiple assignment to: '" + n + "'")
def arity_error(op, e, g):
    expect = '>=' if op in ('AND','OR','XOR','NAND','NOR','XNOR') else ''
    return ValidationError(f"Arity error: {op} expects {expect}{e}, got {g}")
def cycle_error(p): return ValidationError("Cycle detected: " + " -> ".join(p))


def decl_after_assign(n=""): return ValidationError("Declaration after assignment: '" + n + "'" if n else "Declaration after assignment")
def duplicate_name(n): return ValidationError("Duplicate name: '" + n + "'")
def undefined_name(n): return ValidationError("Undefined name: '" + n + "'")
def unassigned_signal(n): return ValidationError("Unassigned signal: '" + n + "'")
def input_assignment(n): return ValidationError("Cannot assign to input: '" + n + "'")
def multiple_assignment(n): return ValidationError("Multiple assignment to: '" + n + "'")
def arity_error(op, e, g):
    expect = '>=' if op in ('AND','OR','XOR','NAND','NOR','XNOR') else ''
    return ValidationError(f"Arity error: {op} expects {expect}{e}, got {g}")
def cycle_error(p): return ValidationError("Cycle detected: " + " -> ".join(p))


# ==============================================================================
# AST Nodes
# ==============================================================================

class NodeType(Enum):
    INPUT = auto(); OUTPUT = auto(); WIRE = auto(); ASSIGNMENT = auto()
    IDENTIFIER = auto(); LITERAL = auto(); CALL = auto()
    BIT_INDEX = auto(); BIT_SLICE = auto(); CONCAT = auto()


@dataclass
class Node:
    node_type: NodeType
    line: int = 0; col: int = 0


@dataclass
class DeclarationNode(Node):
    names: list = field(default_factory=list)


@dataclass
class AssignmentNode(Node):
    lhs: str = ""
    rhs: 'ExprNode' = None


@dataclass
class ExprNode(Node):
    value: str = ""
    args: list = field(default_factory=list)


@dataclass
class SignalInfo:
    name: str
    msb: int = 0
    lsb: int = 0

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: dict = field(default_factory=dict)  # name -> ExprNode
    all_signals: dict = field(default_factory=dict)  # name -> SignalInfo


# ==============================================================================
# Tokenizer
# ==============================================================================

class TokenType(Enum):
    IDENTIFIER = auto(); NUMBER = auto(); OP = auto(); LITERAL = auto()
    EQUALS = auto(); COMMA = auto(); LPAREN = auto(); RPAREN = auto()
    LBRACKET = auto(); RBRACKET = auto(); COLON = auto()
    LBRACE = auto(); RBRACE = auto(); WHITESPACE = auto(); COMMENT = auto(); EOF = auto()


class Token:
    __slots__ = ('type', 'value', 'line', 'col')
    def __init__(self, type_: TokenType, value: str, line: int, col: int):
        self.type = type_; self.value = value
        self.line = line; self.col = col


def tokenize(text: str, filename: Optional[str] = None) -> list:
    """Tokenizer for .circ files."""
    OPS = {'INPUT', 'OUTPUT', 'WIRE', 'NOT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'BUF'}
    single = {'=': TokenType.EQUALS, ',': TokenType.COMMA, '(': TokenType.LPAREN,
              ')': TokenType.RPAREN, '[': TokenType.LBRACKET, ']': TokenType.RBRACKET,
              ':': TokenType.COLON, '{': TokenType.LBRACE, '}': TokenType.RBRACE}

    pos = line = col = 0
    tokens = []

    def peek() -> Optional[str]:
        return text[pos] if pos < len(text) else None

    def advance() -> str:
        nonlocal pos, line, col
        ch = text[pos]
        pos += 1
        if ch == '\n': line += 1; col = 1
        else: col += 1
        return ch

    def skip_ws_comments():
        while pos < len(text):
            ch = peek()
            if ch.isspace(): advance()
            elif ch == '#':
                while pos < len(text) and peek() != '\n':
                    advance()
            else: break

    while pos < len(text):
        skip_ws_comments()
        if pos >= len(text): break

        ch = peek()
        sl, sc = line, col

        # Identifier or keyword
        if ch.isalpha() or ch == '_':
            v = advance()
            while pos < len(text):
                nxt = peek()
                if nxt.isalnum() or nxt == '_': v += advance()
                else: break
            tokens.append(Token(TokenType.OP if v.upper() in OPS else TokenType.IDENTIFIER, v, sl, sc))
            continue

        # Number or binary/hex literal
        if ch.isdigit():
            first = advance()
            if first == '0' and pos < len(text) and peek() in ('b', 'x', 'B', 'X'):
                pref = advance().lower()
                v = '0' + pref; has_digit = False
                while pos < len(text):
                    nxt = peek()
                    if nxt.isalnum() or nxt == '_':
                        if nxt.isalnum(): has_digit = True
                        v += advance()
                    else: break
                if not has_digit:
                    raise CircParseError(f"Unsized literal must have at least one digit after prefix '0{pref}'",
                                          filename, sl, sc)
                tokens.append(Token(TokenType.LITERAL, v, sl, sc))
                continue
            v = first
            while pos < len(text) and peek().isdigit():
                v += advance()
            tokens.append(Token(TokenType.NUMBER, v, sl, sc))
            continue

        # Single character tokens
        if ch in single:
            advance()
            tokens.append(Token(single[ch], ch, sl, sc))
            continue

        raise CircParseError(f"Unexpected character: '{ch}'", filename, sl, sc)

    tokens.append(Token(TokenType.EOF, '', line, col))
    return tokens


# ==============================================================================
# Parser
# ==============================================================================

def parse(tokens: list, filename: Optional[str] = None) -> list:
    OPS = {'NOT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'BUF', 'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}

    pos = 0; current_line = 1

    def peek() -> Optional[Token]:
        return tokens[pos] if pos < len(tokens) else None

    def advance() -> Token:
        nonlocal pos, current_line
        t = peek()
        if t is not None:
            pos += 1
            current_line = t.line
        return t

    def expect(expected: TokenType, msg: str) -> Token:
        t = peek()
        if t is None or t.type != expected:
            raise CircParseError(f"{msg}, got {t.type.name if t else 'EOF'}: '{t.value if t else ''}'",
                                  filename, current_line, 1)
        return advance()

    def parse_expr() -> ExprNode:
        t = peek()
        if t is None:
            raise CircParseError("Unexpected end of file in expression", filename, current_line, 1)

        if t.type == TokenType.IDENTIFIER:
            advance()
            if peek() and peek().type == TokenType.LBRACKET:
                return parse_bit_slice(t.line, t.col, t.value)
            return ExprNode(NodeType.IDENTIFIER, t.line, t.col, t.value)

        if t.type == TokenType.NUMBER:
            advance()
            if t.value not in ('0', '1'):
                raise CircParseError(f"Invalid literal: {t.value}. Only 0 and 1 are allowed.",
                                      filename, t.line, t.col)
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.LITERAL:
            advance()
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.OP:
            op = t.value.upper()
            if op in OPS or op in {'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}:
                return parse_call()
            if re.match(r'^\d+[b-h](.+)$', t.value):
                advance()
                return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)
            raise CircParseError(f"Unknown operator: {op}", filename, t.line, t.col)

        if t.type == TokenType.LBRACE:
            return parse_concat()

        raise CircParseError(f"Unexpected token in expression: {t.type.name}: '{t.value}'",
                              filename, t.line, t.col)

    def parse_bit_slice(line: int, col: int, name: str) -> ExprNode:
        expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
        idx = int(expect(TokenType.NUMBER, "Expected index number").value)
        if peek() and peek().type == TokenType.COLON:
            advance()
            lo = int(expect(TokenType.NUMBER, "Expected low bound").value)
            expect(TokenType.RBRACKET, "Expected ']' after slice")
            return ExprNode(NodeType.BIT_SLICE, line, col, (name, idx, lo))
        expect(TokenType.RBRACKET, "Expected ']' after index")
        return ExprNode(NodeType.BIT_INDEX, line, col, (name, idx))

    def parse_concat() -> ExprNode:
        sl = current_line
        sc = peek().col if peek() else 1
        expect(TokenType.LBRACE, "Expected '{' for concatenation")
        args = [parse_expr()]
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr())
        expect(TokenType.RBRACE, "Expected '}' after concatenation")
        return ExprNode(NodeType.CONCAT, sl, sc, args)

    def parse_call() -> ExprNode:
        op = expect(TokenType.OP, "Expected operator name")
        expect(TokenType.LPAREN, f"Expected '(' after '{op.value}'")
        args = [parse_expr()] if peek() and peek().type != TokenType.RPAREN else []
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr())
        expect(TokenType.RPAREN, "Expected ')' after call")
        return ExprNode(NodeType.CALL, op.line, op.col, op.value, args)

    def parse_declaration() -> DeclarationNode:
        t = expect(TokenType.OP, "Expected declaration type (input, output, wire)")
        t_upper = t.value.upper()
        if t_upper not in {'INPUT', 'OUTPUT', 'WIRE'}:
            raise CircParseError(f"Expected declaration type, got '{t.value}'", filename, t.line, t.col)

        names = []
        while True:
            tok = peek()
            if tok is None or tok.type == TokenType.EOF or tok.type != TokenType.IDENTIFIER or tok.line != t.line:
                break
            advance()
            name = tok.value
            msb = lsb = 0
            if peek() and peek().type == TokenType.LBRACKET:
                expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
                msb = int(expect(TokenType.NUMBER, f"Expected MSB for '{name}'").value)
                expect(TokenType.COLON, f"Expected ':' after MSB for '{name}'")
                lsb = int(expect(TokenType.NUMBER, f"Expected LSB for '{name}'").value)
                expect(TokenType.RBRACKET, f"Expected ']' after LSB for '{name}'")
                if msb < lsb:
                    raise CircParseError(f"MSB must be >= LSB for '{name}'", filename, tok.line, tok.col)
                if lsb < 0:
                    raise CircParseError(f"LSB must be >= 0 for '{name}'", filename, tok.line, tok.col)
            names.append((name, msb, lsb))

        if not names:
            raise CircParseError("Expected names after declaration type", filename, t.line, t.col)

        nt = (NodeType.INPUT if t_upper == "INPUT" else NodeType.OUTPUT if t_upper == "OUTPUT" else NodeType.WIRE)
        return DeclarationNode(nt, t.line, t.col, names)

    def parse_assignment() -> AssignmentNode:
        lt = expect(TokenType.IDENTIFIER, "Expected LHS identifier")
        lhs = lt.value
        expect(TokenType.EQUALS, f"Expected '=' after LHS '{lhs}'")
        rhs = parse_expr()
        return AssignmentNode(NodeType.ASSIGNMENT, line=lt.line, col=lt.col, lhs=lhs, rhs=rhs)

    def parse_line() -> Optional[Node]:
        t = peek()
        if t is None or t.type == TokenType.EOF:
            return None
        if t.type == TokenType.OP and t.value.upper() in {'INPUT', 'OUTPUT', 'WIRE'}:
            return parse_declaration()
        if t.type == TokenType.IDENTIFIER:
            return parse_assignment()
        raise CircParseError(f"Unexpected token: {t.type.name}: '{t.value}'", filename, t.line, t.col)

    stmts = []; p = pos
    while True:
        stmt = parse_line()
        if stmt is None:
            if p == pos: break
            p = pos
            continue
        stmts.append(stmt)
        p = pos
    return stmts


def validate(stmts: list, filename: Optional[str] = None) -> Circuit:
    """Convert parsed statements to a validated Circuit."""
    circuit = Circuit()
    seen_signals = set()

    # First pass: collect all declarations
    for stmt in stmts:
        if stmt.node_type in (NodeType.INPUT, NodeType.OUTPUT, NodeType.WIRE):
            for name, msb, lsb in stmt.names:
                if name in seen_signals:
                    raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
                si = SignalInfo(name, msb, lsb)
                if stmt.node_type == NodeType.INPUT:
                    circuit.inputs.append(si)
                elif stmt.node_type == NodeType.OUTPUT:
                    circuit.outputs.append(si)
                else:
                    circuit.wires.append(si)
                circuit.all_signals[name] = si
                seen_signals.add(name)

    # Second pass: process assignments
    for stmt in stmts:
        if stmt.node_type == NodeType.ASSIGNMENT:
            lhs = stmt.lhs
            rhs = stmt.rhs

            # Check LHS is valid signal
            if lhs not in circuit.all_signals:
                raise undefined_name(lhs)
            if lhs in circuit.inputs:
                raise input_assignment(lhs)
            if lhs in circuit.assignments:
                raise multiple_assignment(lhs)

            # Validate expression references
            def validate_refs(e: ExprNode):
                if e.node_type == NodeType.IDENTIFIER:
                    if e.value not in circuit.all_signals:
                        raise undefined_name(e.value)
                elif e.node_type == NodeType.CALL:
                    for arg in e.args:
                        validate_refs(arg)
                elif e.node_type == NodeType.CONCAT:
                    for arg in e.args:
                        validate_refs(arg)

            validate_refs(rhs)
            circuit.assignments[lhs] = rhs

    # Check all wires and outputs are assigned
    for sig in circuit.wires + circuit.outputs:
        if sig.name not in circuit.assignments:
            raise unassigned_signal(sig.name)

    _detect_cycles(circuit, filename)
    return circuit


def _parse_expr(tokens: list, filename: Optional[str] = None, extra_ops: Optional[set] = None) -> ExprNode:
    """
    Parse expression from tokens.
    extra_ops: additional operator names to allow as literals instead of call syntax.
    """
    pos = 0; current_line = 1
    default_ops = {'NOT', 'BUF'}
    concat_call_ops = {'MUX', 'ITE'}
    variadic_ops = {'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'}
    reduce_ops = {'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}
    allowed_ops = default_ops | concat_call_ops | variadic_ops | reduce_ops
    if extra_ops:
        allowed_ops = allowed_ops | extra_ops

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def advance():
        nonlocal pos, current_line
        t = peek()
        if t is not None:
            pos += 1
            current_line = t.line
        return t

    def expect(expected, msg):
        t = peek()
        if t is None or t.type != expected:
            raise CircParseError(f"{msg}, got {t.type.name if t else 'EOF'}: '{t.value if t else ''}'",
                                  filename, current_line, 1)
        return advance()

    def parse_bit_slice(line, col, name):
        expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
        idx = int(expect(TokenType.NUMBER, "Expected index number").value)
        if peek() and peek().type == TokenType.COLON:
            advance()
            lo = int(expect(TokenType.NUMBER, "Expected low bound").value)
            expect(TokenType.RBRACKET, "Expected ']' after slice")
            return ExprNode(NodeType.BIT_SLICE, line, col, (name, idx, lo))
        expect(TokenType.RBRACKET, "Expected ']' after index")
        return ExprNode(NodeType.BIT_INDEX, line, col, (name, idx))

    def parse_concat():
        sl = current_line; sc = peek().col if peek() else 1
        expect(TokenType.LBRACE, "Expected '{' for concatenation")
        args = [parse_expr_inner()]
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr_inner())
        expect(TokenType.RBRACE, "Expected '}' after concatenation")
        return ExprNode(NodeType.CONCAT, sl, sc, args)

    def parse_call():
        op = expect(TokenType.OP, "Expected operator name")
        expect(TokenType.LPAREN, f"Expected '(' after '{op.value}'")
        args = [parse_expr_inner()] if peek() and peek().type != TokenType.RPAREN else []
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr_inner())
        expect(TokenType.RPAREN, "Expected ')' after call")
        return ExprNode(NodeType.CALL, op.line, op.col, op.value.upper(), args)

    def parse_expr_inner():
        t = peek()
        if t is None:
            raise CircParseError("Unexpected end of file in expression", filename, current_line, 1)

        if t.type == TokenType.IDENTIFIER:
            advance()
            if peek() and peek().type == TokenType.LBRACKET:
                return parse_bit_slice(t.line, t.col, t.value)
            return ExprNode(NodeType.IDENTIFIER, t.line, t.col, t.value)

        if t.type == TokenType.NUMBER:
            advance()
            if t.value not in ('0', '1'):
                raise CircParseError(f"Invalid literal: {t.value}. Only 0 and 1 are allowed.", filename, t.line, t.col)
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.LITERAL:
            advance()
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.OP:
            op = t.value.upper()
            if op in allowed_ops:
                return parse_call()
            if re.match(r'^\d+[b-h](.+)$', t.value):
                advance()
                return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)
            raise CircParseError(f"Unknown operator: {op}", filename, t.line, t.col)

        if t.type == TokenType.LBRACE:
            return parse_concat()

        raise CircParseError(f"Unexpected token in expression: {t.type.name}: '{t.value}'", filename, t.line, t.col)

    return parse_expr_inner()


# ==============================================================================
# JSON Parser
# ==============================================================================

def parse_json(text: str, filename: Optional[str] = None) -> Circuit:
    """Parse circuit from JSON format."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise JsonParseError(f"Invalid JSON syntax: {e}", filename, e.lineno, e.colno)

    # Check format_version
    if 'format_version' not in data:
        raise JsonSchemaError("Missing required field: format_version", filename)
    if data['format_version'] != 1:
        raise JsonSchemaError(f"Unsupported format_version: {data['format_version']}. Expected 1.", filename)

    # Required fields
    for field in ('inputs', 'outputs', 'wires', 'assignments'):
        if field not in data:
            raise JsonSchemaError(f"Missing required field: {field}", filename)

    circuit = Circuit()
    seen_signals = set()

    def _parse_ports(key, target_list):
        nonlocal seen_signals, circuit
        ports = data[key]
        if not isinstance(ports, list):
            raise JsonSchemaError(f"'{key}' must be a list", filename)
        for i, port in enumerate(ports):
            if not isinstance(port, dict):
                raise JsonSchemaError(f"{key}[{i}] must be an object", filename)
            if 'name' not in port:
                raise JsonSchemaError(f"{key}[{i}] missing 'name' field", filename)
            name = port['name']
            msb = port.get('msb', 0)
            lsb = port.get('lsb', 0)
            if not isinstance(msb, int) or not isinstance(lsb, int):
                raise JsonSchemaError(f"{key}[{i}]: '{name}': msb and lsb must be integers", filename)
            if msb < lsb:
                raise JsonSchemaError(f"{key}[{i}]: '{name}': msb must be >= lsb", filename)
            if lsb < 0:
                raise JsonSchemaError(f"{key}[{i}]: '{name}': lsb must be >= 0", filename)
            if name in seen_signals:
                raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
            si = SignalInfo(name, msb, lsb)
            target_list.append(si)
            circuit.all_signals[name] = si
            seen_signals.add(name)

    for k, lst in (('inputs', circuit.inputs), ('outputs', circuit.outputs), ('wires', circuit.wires)):
        _parse_ports(k, lst)

    # Parse assignments
    if not isinstance(data['assignments'], list):
        raise JsonSchemaError("'assignments' must be a list", filename)
    for i, assign in enumerate(data['assignments']):
        if not isinstance(assign, dict):
            raise JsonSchemaError(f"assignments[{i}] must be an object", filename)
        if 'lhs' not in assign:
            raise JsonSchemaError(f"assignments[{i}] missing 'lhs' field", filename)
        if 'rhs' not in assign:
            raise JsonSchemaError(f"assignments[{i}] missing 'rhs' field", filename)
        lhs = assign['lhs']
        rhs = assign['rhs']
        if not isinstance(lhs, str):
            raise JsonSchemaError(f"assignments[{i}]: 'lhs' must be a string", filename)
        if not isinstance(rhs, str):
            raise JsonSchemaError(f"assignments[{i}]: 'rhs' must be a string", filename)

        # Check LHS is valid signal
        if lhs not in circuit.all_signals:
            raise undefined_name(lhs)
        if lhs in circuit.inputs:
            raise input_assignment(lhs)
        if lhs in circuit.assignments:
            raise multiple_assignment(lhs)

        # Parse RHS expression using the existing parser
        try:
            tokens = tokenize(rhs, filename)
            expr = parse_expr_from_tokens(tokens, filename)
        except CircoptError as e:
            raise
        except Exception as e:
            raise CircParseError(f"Expression parsing error: {e}", filename)

        # Validate expression references
        def validate_refs(e: ExprNode):
            if e.node_type == NodeType.IDENTIFIER:
                if e.value not in circuit.all_signals:
                    raise undefined_name(e.value)
            elif e.node_type == NodeType.CALL:
                op = e.value.upper()
                SUPPORTED_OPS_JSON = {
                    'NOT': 1, 'BUF': 1,
                    'AND': 'multiple', 'OR': 'multiple', 'XOR': 'multiple',
                    'NAND': 'multiple', 'NOR': 'multiple', 'XNOR': 'multiple',
                }
                if op not in SUPPORTED_OPS_JSON:
                    raise CircParseError(f"Unknown operator: {op}", filename, e.line, e.col)
                exp = SUPPORTED_OPS_JSON[op]; got = len(e.args)
                if isinstance(exp, str):
                    if exp == 'multiple' and got < 1:
                        raise arity_error(op, exp, got)
                elif got != exp:
                    raise arity_error(op, exp, got)
                for arg in e.args:
                    validate_refs(arg)

        validate_refs(expr)
        circuit.assignments[lhs] = expr

    # Check all wires and outputs are assigned
    for sig in circuit.wires + circuit.outputs:
        if sig.name not in circuit.assignments:
            raise unassigned_signal(sig.name)

    _detect_cycles(circuit, filename)
    return circuit


def _parse_expr_with_consumption(tokens: list, filename: Optional[str] = None, extra_ops: Optional[set] = None):
    """Parse expression, returning (expr, tokens_consumed)."""
    pos = 0; current_line = 1
    default_ops = {'NOT', 'BUF'}
    concat_call_ops = {'MUX', 'ITE'}
    variadic_ops = {'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'}
    reduce_ops = {'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}
    allowed_ops = default_ops | concat_call_ops | variadic_ops | reduce_ops
    if extra_ops:
        allowed_ops = allowed_ops | extra_ops

    def peek():
        return tokens[pos] if pos < len(tokens) else None

    def advance():
        nonlocal pos, current_line
        t = peek()
        if t is not None:
            pos += 1
            current_line = t.line
        return t

    def expect(expected, msg):
        t = peek()
        if t is None or t.type != expected:
            raise CircParseError(f"{msg}, got {t.type.name if t else 'EOF'}: '{t.value if t else ''}'",
                                  filename, current_line, 1)
        return advance()

    def parse_bit_slice(line, col, name):
        expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
        idx = int(expect(TokenType.NUMBER, "Expected index number").value)
        if peek() and peek().type == TokenType.COLON:
            advance()
            lo = int(expect(TokenType.NUMBER, "Expected low bound").value)
            expect(TokenType.RBRACKET, "Expected ']' after slice")
            return ExprNode(NodeType.BIT_SLICE, line, col, (name, idx, lo))
        expect(TokenType.RBRACKET, "Expected ']' after index")
        return ExprNode(NodeType.BIT_INDEX, line, col, (name, idx))

    def parse_concat():
        sl = current_line; sc = peek().col if peek() else 1
        expect(TokenType.LBRACE, "Expected '{' for concatenation")
        args = [parse_expr_inner()]
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr_inner())
        expect(TokenType.RBRACE, "Expected '}' after concatenation")
        return ExprNode(NodeType.CONCAT, sl, sc, args)

    def parse_call():
        op = expect(TokenType.OP, "Expected operator name")
        expect(TokenType.LPAREN, f"Expected '(' after '{op.value}'")
        args = [parse_expr_inner()] if peek() and peek().type != TokenType.RPAREN else []
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr_inner())
        expect(TokenType.RPAREN, "Expected ')' after call")
        return ExprNode(NodeType.CALL, op.line, op.col, op.value.upper(), args)

    def parse_expr_inner():
        t = peek()
        if t is None:
            raise CircParseError("Unexpected end of file in expression", filename, current_line, 1)

        if t.type == TokenType.IDENTIFIER:
            advance()
            if peek() and peek().type == TokenType.LBRACKET:
                return parse_bit_slice(t.line, t.col, t.value)
            return ExprNode(NodeType.IDENTIFIER, t.line, t.col, t.value)

        if t.type == TokenType.NUMBER:
            advance()
            if t.value not in ('0', '1'):
                raise CircParseError(f"Invalid literal: {t.value}. Only 0 and 1 are allowed.", filename, t.line, t.col)
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.LITERAL:
            advance()
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.OP:
            op = t.value.upper()
            if op in allowed_ops:
                return parse_call()
            if re.match(r'^\d+[b-h](.+)$', t.value):
                advance()
                return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)
            raise CircParseError(f"Unknown operator: {op}", filename, t.line, t.col)

        if t.type == TokenType.LBRACE:
            return parse_concat()

        raise CircParseError(f"Unexpected token in expression: {t.type.name}: '{t.value}'",
                              filename, t.line, t.col)

    expr = parse_expr_inner()
    return expr, pos


def parse_expr_from_tokens(tokens: list, filename: Optional[str] = None) -> ExprNode:
    """Parse expression from tokens (simplified parser for JSON RHS)."""
    expr, consumed = _parse_expr_with_consumption(tokens, filename,
                                                   extra_ops={'NOT', 'BUF', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'})
    # Check for trailing tokens
    if consumed < len(tokens):
        remaining = tokens[consumed]
        if remaining.type != TokenType.EOF:
            raise CircParseError(f"Unexpected trailing tokens in expression: '{remaining.value}'",
                                  filename, remaining.line, remaining.col)
    return expr


# ==============================================================================
# BENCH Parser
# ==============================================================================

def parse_bench(text: str, filename: Optional[str] = None) -> Circuit:
    """Parse circuit from BENCH format."""
    circuit = Circuit()
    seen_signals = set()
    lines = text.split('\n')

    # First pass: collect all INPUT and OUTPUT declarations
    for line_num, line in enumerate(lines, 1):
        line = line.strip()
        # Remove comments
        if '#' in line:
            line = line[:line.index('#')]
        line = line.strip()
        if not line:
            continue

        # Parse INPUT(name)
        if line.startswith('INPUT(') and line.endswith(')'):
            name = line[6:-1].strip()
            if not name:
                raise BenchParseError(f"Empty INPUT name at line {line_num}", filename, line_num, 1)
            # Check for brackets in identifier (scalar-only constraint)
            if '[' in name or ']' in name:
                raise BenchParseError(f"Invalid identifier '{name}': brackets not allowed in BENCH format", filename, line_num, 1)
            if name in seen_signals:
                raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
            si = SignalInfo(name, 0, 0)
            circuit.inputs.append(si)
            circuit.all_signals[name] = si
            seen_signals.add(name)
            continue

        # Parse OUTPUT(name)
        if line.startswith('OUTPUT(') and line.endswith(')'):
            name = line[7:-1].strip()
            if not name:
                raise BenchParseError(f"Empty OUTPUT name at line {line_num}", filename, line_num, 1)
            if '[' in name or ']' in name:
                raise BenchParseError(f"Invalid identifier '{name}': brackets not allowed in BENCH format", filename, line_num, 1)
            if name in seen_signals:
                raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
            si = SignalInfo(name, 0, 0)
            circuit.outputs.append(si)
            circuit.all_signals[name] = si
            seen_signals.add(name)
            continue

        # Parse assignment: lhs = OP(arg1, arg2, ...)
        if '=' in line:
            parts = line.split('=', 1)
            if len(parts) != 2:
                raise BenchParseError(f"Invalid assignment syntax at line {line_num}", filename, line_num, 1)
            lhs = parts[0].strip()
            rhs = parts[1].strip()

            if not lhs:
                raise BenchParseError(f"Empty LHS in assignment at line {line_num}", filename, line_num, 1)
            if '[' in lhs or ']' in lhs:
                raise BenchParseError(f"Invalid identifier '{lhs}': brackets not allowed in BENCH format", filename, line_num, 1)

            # Check for literals in RHS (BENCH constraint: no literals)
            # Look for 0, 1, 0b..., 0x..., etc. as standalone tokens
            # Simple check: if RHS contains digits not part of operator names
            rhs_stripped = rhs.strip()
            # Check for numeric literals (starting with digit or 0b/0x)
            if re.match(r'^\d', rhs_stripped) or rhs_stripped.startswith(('0b', '0B', '0x', '0X')):
                raise BenchParseError(f"Literals not allowed in BENCH format: '{rhs_stripped}'", filename, line_num, 1)

            # Parse operator call
            match = re.match(r'^([A-Z]+)\((.+)\)$', rhs_stripped)
            if not match:
                raise BenchParseError(f"Invalid operator syntax: '{rhs_stripped}' at line {line_num}", filename, line_num, 1)

            op = match.group(1)
            args_str = match.group(2)

            # Supported operators
            unary_ops = {'NOT', 'BUF'}
            variadic_ops = {'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'}

            if op not in unary_ops and op not in variadic_ops:
                raise BenchParseError(f"Unsupported operator '{op}' in BENCH format", filename, line_num, 1)

            # Parse arguments (comma-separated identifiers)
            args = [a.strip() for a in args_str.split(',')]
            args = [a for a in args if a]  # remove empty

            if not args:
                raise BenchParseError(f"Operator '{op}' requires at least one argument", filename, line_num, 1)

            # Check for brackets in arguments (scalar-only)
            for arg in args:
                if '[' in arg or ']' in arg:
                    raise BenchParseError(f"Invalid identifier '{arg}': brackets not allowed in BENCH format", filename, line_num, 1)

            # Check arity
            if op in unary_ops and len(args) != 1:
                raise arity_error(op, 1, len(args))
            # variadic ops require at least 2 args
            if op in variadic_ops and len(args) < 2:
                raise arity_error(op, '>=2', len(args))

            # Check LHS is valid
            if lhs not in seen_signals:
                raise undefined_name(lhs)
            if lhs in circuit.inputs:
                raise input_assignment(lhs)
            if lhs in circuit.assignments:
                raise multiple_assignment(lhs)

            # Check all arguments are defined (either inputs or previously assigned wires)
            for arg in args:
                if arg not in circuit.all_signals:
                    # In BENCH, wires are implicitly declared by assignment
                    # but OUTPUT references must reference defined signals
                    # For now, we'll track that args are used as identifiers
                    pass

            # Create expression node for the RHS
            expr_args = []
            for arg in args:
                # Check if argument refers to undefined signal
                if arg not in circuit.all_signals:
                    # Implicit wire declaration - add it now
                    if arg in seen_signals:
                        raise RedefinitionError(f"Signal '{arg}' defined multiple times", filename)
                    arg_si = SignalInfo(arg, 0, 0)
                    circuit.all_signals[arg] = arg_si
                    seen_signals.add(arg)
                expr_args.append(ExprNode(NodeType.IDENTIFIER, line_num, 1, arg))

            expr = ExprNode(NodeType.CALL, line_num, 1, op, expr_args)
            circuit.assignments[lhs] = expr

    # After parsing, validate OUTPUT references
    for out_sig in circuit.outputs:
        if out_sig.name not in circuit.assignments:
            raise undefined_name(out_sig.name)

    # Validate all assignments' arguments refer to existing signals
    for lhs, expr in list(circuit.assignments.items()):
        for arg_expr in _get_identifiers(expr):
            if arg_expr not in circuit.all_signals:
                # This shouldn't happen with our parsing above, but check anyway
                raise undefined_name(arg_expr)

    # Check for cycles (same as in validate function)
    visited = set(); rec = set(); path = []
    def dfs(n):
        visited.add(n); rec.add(n); path.append(n)
        if n in circuit.assignments:
            for dep in _get_identifiers(circuit.assignments[n]):
                if dep not in circuit.all_signals: continue
                if dep not in visited:
                    if dfs(dep): return True
                elif dep in rec:
                    raise cycle_error(path[path.index(dep):] + [dep])
        path.pop(); rec.remove(n); return False
    for sig in circuit.outputs + list(circuit.assignments.keys()):
        if sig not in visited and dfs(sig): return

    return circuit


def _get_identifiers(expr: ExprNode) -> list:
    if expr.node_type == NodeType.IDENTIFIER: return [expr.value]
    if expr.node_type == NodeType.LITERAL: return []
    if expr.node_type == NodeType.CALL: return [i for a in expr.args for i in _get_identifiers(a)]
    return []


# ==============================================================================
# 3-Valued Logic
# ==============================================================================

# Representation: 0=False, 1=True, 'X'=Unknown

def _is_x(v) -> bool:
    return v == 'X'

def _to_3val(val) -> str:
    """Convert 0/1 to '0'/'1', or return 'X' as is."""
    if val == 'X':
        return 'X'
    return '0' if val == 0 else '1'

def _parse_input_value(s: str, mode: str) -> list:
    """Parse input value string into list of 3-valued bits ['0'|'1'|'X'].

    Supports:
    - '0', '1', 'X' (scalar)
    - 0b[01Xx_]+ (unsized binary)
    - <N>'b[01Xx_]+ (sized binary)
    - 0x..., <N>'h... (hex)
    - <N>'d..., 0d... (decimal)
    """
    s = s.strip()

    # Scalar: single 0, 1, or X/x
    if s in ('0', '1'):
        return [s]
    if s.upper() == 'X':
        return ['X']

    # Binary format
    bin_match = re.match(r'^(\d+)\'[bB]([01Xx_]+)$', s)
    if bin_match:
        width = int(bin_match.group(1))
        bits_str = bin_match.group(2).replace('_', '')
        bits = []
        for ch in bits_str:
            ch_up = ch.upper()
            if ch_up == 'X':
                bits.append('X')
            elif ch_up in ('0', '1'):
                bits.append(ch_up)
            else:
                raise InputValueParseError(f"Invalid binary digit '{ch}' in '{s}'")
        if len(bits) != width:
            raise WidthMismatchError(f"Width mismatch: declared {width}, got {len(bits)}")
        return bits

    # Unsized binary (0b prefix)
    if s.startswith('0b') or s.startswith('0B'):
        bits_str = s[2:]
        bits = []
        for ch in bits_str:
            ch_up = ch.upper()
            if ch_up == 'X':
                bits.append('X')
            elif ch_up in ('0', '1'):
                bits.append(ch_up)
            else:
                raise InputValueParseError(f"Invalid binary digit '{ch}' in '{s}'")
        return bits

    # Hex format - X not allowed in bits in 3val mode, but parse it generally
    hex_match = re.match(r'^(\d+)\'[hH]([0-9a-fA-F]+)$', s)
    if hex_match:
        width = int(hex_match.group(1))
        hex_str = hex_match.group(2)
        # Check for X in hex digits (not allowed)
        if 'X' in hex_str.upper():
            raise InputValueParseError(f"Hex literal cannot contain X: '{s}'")
        # Convert hex to binary
        val = int(hex_str, 16)
        bin_str = bin(val)[2:].zfill(width)
        return [c for c in bin_str]

    # 0x prefix hex
    if s.startswith('0x') or s.startswith('0X'):
        hex_str = s[2:]
        if 'X' in hex_str.upper():
            raise InputValueParseError(f"Hex literal cannot contain X: '{s}'")
        val = int(hex_str, 16)
        bin_str = bin(val)[2:]
        return [c for c in bin_str]

    # Decimal format
    dec_match = re.match(r'^(\d+)\'[dD](\d+)$', s)
    if dec_match:
        width = int(dec_match.group(1))
        dec_str = dec_match.group(2)
        val = int(dec_str, 10)
        bin_str = bin(val)[2:].zfill(width)
        return [c for c in bin_str]

    # 0d prefix decimal
    if s.startswith('0d') or s.startswith('0D'):
        dec_str = s[2:]
        val = int(dec_str, 10)
        bin_str = bin(val)[2:]
        return [c for c in bin_str]

    raise InputValueParseError(f"Invalid value format: '{s}'")


def _format_3val_output(bits: list, mode: str, radix: str, width_info=None) -> str:
    """Format 3-valued output as string.

    Args:
        bits: List of '0', '1', 'X'
        mode: '2val' or '3val'
        radix: 'bin', 'hex', or 'dec'
        width_info: Optional (msb, lsb) tuple for vector signals

    Returns:
        Formatted string (scalar or vector format)
    """
    if mode == '2val':
        # 2-value output
        if width_info:
            msb, lsb = width_info
            width = msb - lsb + 1
            # For 2val, all bits must be 0 or 1
            val = int(''.join(bits), 2)
            if radix == 'bin':
                return bin(val)[2:].zfill(width)
            elif radix == 'hex':
                return hex(val)[2:].upper()
            else:  # dec
                return str(val)
        else:
            return str(int(''.join(bits), 2) if bits else 0)

    # 3-valued mode: only binary radix allowed
    if radix != 'bin':
        raise RadixNotAllowedIn3ValError("Radix 'hex' or 'dec' not allowed with --mode 3val")

    if len(bits) == 1:
        # Scalar format
        return bits[0]
    else:
        # Vector format: 0b + bits
        return '0b' + ''.join(bits)


def _eval_expr_3val(expr: ExprNode, values: dict, default: str = '0') -> list:
    """Evaluate expression in 3-valued mode, returns list of bits."""
    if expr.node_type == NodeType.IDENTIFIER:
        val = values[expr.value]
        if isinstance(val, list):
            return val[:]
        return [val]

    if expr.node_type == NodeType.LITERAL:
        # Parse literal in 3val mode
        return _parse_input_value(expr.value, '3val')

    if expr.node_type == NodeType.CALL:
        op = expr.value.upper()
        # Evaluate all args to lists of bits
        arg_bits = [_eval_expr_3val(a, values, default) for a in expr.args]

        if op in ('NOT', 'BUF'):
            return _eval_unary_op(op, arg_bits[0])
        elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            return _eval_variadic_op(op, arg_bits)
        elif op == 'MUX':
            return _eval_mux(arg_bits[0], arg_bits[1], arg_bits[2])
        elif op == 'EQ':
            return _eval_eq(arg_bits[0], arg_bits[1])
        elif op in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            return _eval_reduce_op(op, arg_bits[0])
        else:
            raise ValueError(f"Unknown operator: {op}")

    raise ValueError(f"Unknown node type: {expr.node_type}")


def _eval_unary_op(op: str, a: list) -> list:
    """Evaluate unary operation."""
    if op == 'NOT':
        return [{'0': '1', '1': '0', 'X': 'X'}[b] for b in a]
    elif op == 'BUF':
        return a[:]
    else:
        raise ValueError(f"Unknown unary op: {op}")


def _eval_variadic_op(op: str, args: list) -> list:
    """Evaluate variadic operation (AND, OR, XOR, NAND, NOR, XNOR)."""
    # All args must have same width
    width = len(args[0])
    for arg in args:
        if len(arg) != width:
            raise WidthMismatchError(f"Width mismatch in {op}: {len(arg)} vs {width}")

    result = []
    for i in range(width):
        bits = [arg[i] for arg in args]
        if op == 'AND':
            result.append(_and_3val(bits))
        elif op == 'OR':
            result.append(_or_3val(bits))
        elif op == 'XOR':
            result.append(_xor_3val(bits))
        elif op == 'NAND':
            result.append(_not_3val(_and_3val(bits)))
        elif op == 'NOR':
            result.append(_not_3val(_or_3val(bits)))
        elif op == 'XNOR':
            result.append(_not_3val(_xor_3val(bits)))
        else:
            raise ValueError(f"Unknown variadic op: {op}")
    return result


def _and_3val(bits: list) -> str:
    """3-valued AND. Returns '0', '1', or 'X'."""
    has_zero = False
    has_x = False
    for b in bits:
        if b == '0':
            return '0'
        if b == 'X':
            has_x = True
    # No zeros, all ones or unknown
    if has_x:
        return 'X'
    return '1'


def _or_3val(bits: list) -> str:
    """3-valued OR. Returns '0', '1', or 'X'."""
    has_one = False
    has_x = False
    for b in bits:
        if b == '1':
            return '1'
        if b == 'X':
            has_x = True
    # No ones, all zeros or unknown
    if has_x:
        return 'X'
    return '0'


def _xor_3val(bits: list) -> str:
    """3-valued XOR. Returns '0', '1', or 'X'."""
    # XOR with X produces X
    for b in bits:
        if b == 'X':
            return 'X'
    # No X, compute regular XOR
    result = 0
    for b in bits:
        result ^= int(b)
    return str(result)


def _not_3val(b: str) -> str:
    """3-valued NOT."""
    if b == '0': return '1'
    if b == '1': return '0'
    return 'X'


def _eval_mux(sel: list, a: list, b: list) -> list:
    """3-valued MUX."""
    width = len(a)
    if len(b) != width:
        raise WidthMismatchError(f"MUX: width mismatch between a ({len(a)}) and b ({len(b)})")
    if len(sel) == 1:
        # Scalar select
        s = sel[0]
        if s == '0':
            return b[:]
        elif s == '1':
            return a[:]
        else:  # X
            # MUX(X, a, b) = a if a == b, else X (per bit)
            result = []
            for i in range(width):
                if a[i] == b[i]:
                    result.append(a[i])
                else:
                    result.append('X')
            return result
    else:
        # Vector select - use bit 0
        return _eval_mux([sel[0]], a, b)


def _eval_eq(a: list, b: list) -> list:
    """3-valued EQ. Returns 1 when definitively equal, 0 when definitively unequal, X when uncertain."""
    if len(a) != len(b):
        raise WidthMismatchError(f"EQ: width mismatch between a ({len(a)}) and b ({len(b)})")

    has_x = False
    for i in range(len(a)):
        ai, bi = a[i], b[i]
        if ai == 'X' or bi == 'X':
            has_x = True
        elif ai != bi:
            # Found definitive inequality
            # All remaining bits could be X, but we already know it's unequal
            return ['0']

    # All definitively equal so far
    if has_x:
        return ['X']
    return ['1']


def _eval_reduce_op(op: str, a: list) -> list:
    """3-valued reduction operator. Returns list with single bit."""
    if op == 'REDUCE_AND':
        return [_and_3val(a)]
    elif op == 'REDUCE_OR':
        return [_or_3val(a)]
    elif op == 'REDUCE_XOR':
        return [_xor_3val(a)]
    else:
        raise ValueError(f"Unknown reduction op: {op}")


# ==============================================================================
# Cycle detection
# ==============================================================================

def _detect_cycles(circuit: Circuit, filename: Optional[str] = None) -> None:
    """Detect cycles in the circuit graph."""
    visited = set()
    rec_stack = set()
    path = []

    def dfs(sig_name: str) -> bool:
        visited.add(sig_name)
        rec_stack.add(sig_name)
        path.append(sig_name)

        if sig_name in circuit.assignments:
            expr = circuit.assignments[sig_name]
            deps = _get_identifiers(expr)
            for dep in deps:
                if dep not in circuit.all_signals:
                    continue
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(dep)
                    raise cycle_error(" -> ".join(path[cycle_start:] + [dep]))

        path.pop()
        rec_stack.remove(sig_name)
        return False

    for sig in circuit.outputs:
        sig_name = sig.name if isinstance(sig, SignalInfo) else sig
        if sig_name not in visited:
            if dfs(sig_name):
                return
    for sig_name in list(circuit.assignments.keys()):
        if sig_name not in visited:
            if dfs(sig_name):
                return


# ==============================================================================
# Evaluator
# ==============================================================================

_OP_FUNCS = {
    'NOT': lambda x: 1 - x, 'BUF': lambda x: x,
    'AND': lambda a: functools.reduce(int.__and__, a, 1),
    'OR': lambda a: functools.reduce(int.__or__, a, 0),
    'XOR': lambda a: functools.reduce(int.__xor__, a, 0),
    'NAND': lambda a: 1 - functools.reduce(int.__and__, a, 1),
    'NOR': lambda a: 1 - functools.reduce(int.__or__, a, 0),
    'XNOR': lambda a: 1 - functools.reduce(int.__xor__, a, 0),
}

def _eval_expr(expr: ExprNode, values: dict) -> int:
    if expr.node_type == NodeType.IDENTIFIER: return values[expr.value]
    if expr.node_type == NodeType.LITERAL: return int(expr.value)
    if expr.node_type == NodeType.CALL: return _OP_FUNCS[expr.value]([_eval_expr(a, values) for a in expr.args])
    raise ValueError(f"Unknown node type: {expr.node_type}")


def evaluate_circuit(circuit: Circuit, inputs: dict, default: int = 0, mode: str = '2val') -> dict:
    for n in inputs:
        if n not in circuit.inputs: raise UnknownInputError(n)
    for sig in circuit.inputs:
        if sig.name not in inputs: raise MissingInputError(sig.name)

    if mode == '2val':
        # 2-valued mode validation
        for n, v in inputs.items():
            if v not in (0, 1): raise InputValueParseError(str(v))
        default_val = '0' if default == 0 else '1'
    else:
        # 3-valued mode - parse all input values
        default_val = '0' if default == 0 else '1'
        for n, v in inputs.items():
            if not isinstance(v, str):
                raise InputValueParseError(str(v))

    # Convert inputs to internal representation
    vals = {}
    for s in circuit.inputs:
        if s.name in inputs:
            if mode == '2val':
                vals[s.name] = inputs[s.name]
            else:
                v = inputs[s.name]
                if isinstance(v, list):
                    vals[s.name] = v
                else:
                    # Parse string value
                    parsed = _parse_input_value(v, mode)
                    if s.width > 1 and len(parsed) != s.width:
                        raise InputWidthMismatchError(f"Width mismatch for '{s.name}': expected {s.width}, got {len(parsed)}")
                    vals[s.name] = parsed
        else:
            if mode == '2val':
                vals[s.name] = default
            else:
                vals[s.name] = [default_val] * s.width

    max_iter = len(circuit.all_signals) + 1
    for _ in range(max_iter):
        changed = False
        for name, expr in circuit.assignments.items():
            if name in vals: continue
            deps = _get_identifiers(expr)
            if all(d in vals for d in deps):
                if mode == '2val':
                    vals[name] = _eval_expr(expr, vals)
                else:
                    vals[name] = _eval_expr_3val(expr, vals, default_val)
                changed = True
        if not changed: break

    out = {}
    for sig in circuit.outputs:
        if sig.name not in vals: raise unassigned_signal(sig.name)
        out[sig.name] = vals[sig.name]
    return out


# ==============================================================================
# Command Handlers
# ==============================================================================

def _process(filename: str, args, handler) -> int:
    try:
        with open(filename) as f: text = f.read()
    except FileNotFoundError:
        print(json.dumps(CliUsageError(f"File not found: {filename}").to_dict(args.command)), file=sys.stderr)
        return 1

    # Determine format
    fmt = args.format
    if fmt == 'auto':
        if filename.endswith('.circ'):
            fmt = 'circ'
        elif filename.endswith('.json'):
            fmt = 'json'
        elif filename.endswith('.bench'):
            fmt = 'bench'
        else:
            e = UnknownInputFormatError(f"Cannot determine format from extension: {filename}", filename)
            print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
            return e.exit_code

    try:
        if fmt == 'circ':
            tokens = tokenize(text, filename)
            stmts = parse(tokens, filename)
            circuit = validate(stmts, filename)
        elif fmt == 'json':
            circuit = parse_json(text, filename)
        elif fmt == 'bench':
            circuit = parse_bench(text, filename)
        else:
            raise ValueError(f"Unknown format: {fmt}")
    except CircoptError as e:
        if e.file is None: e.file = filename
        print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
        return e.exit_code

    return handler(circuit, fmt)


def eval_command(args) -> int:
    def handler(c, fmt):
        inputs = {}
        if args.set_val:
            for s in args.set_val:
                if '=' not in s:
                    print(json.dumps(CliUsageError(f"Invalid --set format: '{s}'. Expected name=value").to_dict("eval")), file=sys.stderr)
                    return 1
                n, v = s.split('=', 1)
                n = n.strip()
                v = v.strip()
                if args.mode == '2val':
                    # 2-valued mode: parse as int
                    try: v_val = int(v)
                    except ValueError:
                        print(json.dumps(InputValueParseError(v).to_dict("eval")), file=sys.stderr)
                        return 2
                    if v_val not in (0, 1):
                        print(json.dumps(InputValueParseError(f"Value must be 0 or 1 in 2val mode: '{v}'").to_dict("eval")), file=sys.stderr)
                        return 2
                    inputs[n] = v_val
                else:
                    # 3-valued mode: keep as string for parsing
                    inputs[n] = v

        try:
            outputs = evaluate_circuit(c, inputs, default=1 if args.default else 0, mode=args.mode)
        except CircoptError as e:
            print(json.dumps(e.to_dict("eval")), file=sys.stderr)
            return e.exit_code

        if args.json:
            mode = args.mode
            radix = args.radix
            outputs_list = []
            for sig in c.outputs:
                val = outputs[sig.name]
                if mode == '2val':
                    # Format for 2val - single int values
                    if sig.width == 1:
                        formatted = str(val)
                    else:
                        formatted = str(val)
                    outputs_list.append({"name": sig.name, "msb": sig.msb, "lsb": sig.lsb, "value": formatted})
                else:
                    # 3-valued mode
                    if radix != 'bin':
                        print(json.dumps(RadixNotAllowedIn3ValError("Radix 'hex' or 'dec' not allowed with --mode 3val").to_dict("eval")), file=sys.stderr)
                        return 1
                    if sig.width == 1:
                        formatted = val[0] if isinstance(val, list) else str(val)
                    else:
                        formatted = '0b' + ''.join(val) if isinstance(val, list) else str(val)
                    outputs_list.append({"name": sig.name, "msb": sig.msb, "lsb": sig.lsb, "value": formatted})

            print(json.dumps({"ok": True, "command": "eval", "mode": mode, "radix": radix, "outputs": outputs_list}))
        else:
            for sig in c.outputs:
                val = outputs[sig.name]
                if args.mode == '2val':
                    # 2-valued output - simple int
                    print(f"{sig.name}={val}")
                else:
                    # 3-valued mode - check radix restriction
                    if args.radix != 'bin':
                        print(json.dumps(RadixNotAllowedIn3ValError("Radix 'hex' or 'dec' not allowed with --mode 3val").to_dict("eval")), file=sys.stderr)
                        return 1
                    if sig.width == 1:
                        print(f"{sig.name}={val[0]}")
                    else:
                        print(f"{sig.name}=0b{''.join(val)}")
        return 0

    return _process(args.file, args, handler)


def check_command(args) -> int:
    def handler(c, fmt):
        ins = sorted(({"name": s.name, "msb": 0, "lsb": 0} for s in c.inputs), key=lambda x: x["name"])
        outs = sorted(({"name": s.name, "msb": 0, "lsb": 0} for s in c.outputs), key=lambda x: x["name"])
        print(json.dumps({"ok": True, "command": "check", "format": fmt, "inputs": ins, "outputs": outs}))
        return 0
    return _process(args.file, args, handler)


def stats_command(args) -> int:
    def handler(c, fmt):
        # Count inputs, outputs, wires
        inputs_count = len(c.inputs)
        outputs_count = len(c.outputs)
        wires_count = len(c.wires)
        assignments_count = len(c.assignments)

        # Calculate edges_count (total dependency references with duplicates)
        def count_edges(expr):
            if expr.node_type == NodeType.IDENTIFIER:
                return 1
            if expr.node_type == NodeType.LITERAL:
                return 0
            if expr.node_type == NodeType.CALL:
                return sum(count_edges(a) for a in expr.args)
            if expr.node_type == NodeType.CONCAT:
                return sum(count_edges(a) for a in expr.args)
            if expr.node_type == NodeType.BIT_INDEX:
                return 1
            if expr.node_type == NodeType.BIT_SLICE:
                return 1
            return 0

        edges_count = sum(count_edges(expr) for expr in c.assignments.values())

        # Calculate depth
        def calc_depth(expr, memo):
            if expr.node_type == NodeType.IDENTIFIER:
                return 1
            if expr.node_type == NodeType.LITERAL:
                return 1
            if expr.node_type == NodeType.CALL:
                if expr.value.upper() in ('MUX', 'ITE'):
                    if len(expr.args) != 3:
                        return 1
                    return max(calc_depth(expr.args[0], memo), calc_depth(expr.args[1], memo), calc_depth(expr.args[2], memo)) + 1
                child_depths = [calc_depth(a, memo) for a in expr.args]
                return max(child_depths) if child_depths else 1
            if expr.node_type == NodeType.CONCAT:
                child_depths = [calc_depth(a, memo) for a in expr.args]
                return max(child_depths) if child_depths else 1
            if expr.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                return 1
            return 1

        # Depth from inputs to outputs: find max depth among all output signals
        depth_memo = {}
        for lhs, expr in c.assignments.items():
            depth_memo[lhs] = calc_depth(expr, depth_memo)

        # For each output, we need to compute its depth
        # A circuit's depth is the maximum depth from any input to any output
        # Each assignment adds 1, literal-only assignment has depth 1

        # Build signal depth graph
        signal_depth = {}

        def get_signal_depth(sig_name):
            if sig_name in signal_depth:
                return signal_depth[sig_name]
            if sig_name not in c.assignments:
                # Input signal - depth is 0 (or 1 depending on definition)
                # According to spec: "A literal-only assignment has depth 1"
                # For inputs, they have no dependency chain, so depth contribution is 0
                if sig_name in c.all_signals:
                    signal_depth[sig_name] = 0
                    return 0
                return 0

            expr = c.assignments[sig_name]
            if expr.node_type == NodeType.IDENTIFIER:
                # Wire assigned from input - depth 1
                signal_depth[sig_name] = 1
                return 1
            if expr.node_type == NodeType.LITERAL:
                signal_depth[sig_name] = 1
                return 1
            if expr.node_type == NodeType.BIT_INDEX:
                signal_depth[sig_name] = get_signal_depth(expr.value)
                return signal_depth[sig_name]
            if expr.node_type == NodeType.BIT_SLICE:
                signal_depth[sig_name] = get_signal_depth(expr.value[0])
                return signal_depth[sig_name]
            if expr.node_type == NodeType.CALL:
                op = expr.value.upper()
                if op in ('MUX', 'ITE'):
                    if len(expr.args) == 3:
                        d = max(get_signal_depth(expr.args[0].value if expr.args[0].node_type == NodeType.IDENTIFIER else '_tmp'),
                                get_signal_depth(expr.args[1].value if expr.args[1].node_type == NodeType.IDENTIFIER else '_tmp'),
                                get_signal_depth(expr.args[2].value if expr.args[2].node_type == NodeType.IDENTIFIER else '_tmp')) + 1
                        signal_depth[sig_name] = d
                        return d
                child_deps = []
                for arg in expr.args:
                    if arg.node_type == NodeType.IDENTIFIER:
                        child_deps.append(get_signal_depth(arg.value))
                    elif arg.node_type == NodeType.CALL:
                        # This is an operator call inline, shouldn't happen in AST
                        child_deps.append(get_signal_depth(sig_name + '_inner'))
                    elif arg.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                        child_deps.append(get_signal_depth(arg.value[0] if isinstance(arg.value, tuple) else arg.value))
                    else:
                        pass
                if child_deps:
                    d = max(child_deps) + 1
                else:
                    d = 1
                signal_depth[sig_name] = d
                return d
            if expr.node_type == NodeType.CONCAT:
                child_deps = []
                for arg in expr.args:
                    if arg.node_type == NodeType.IDENTIFIER:
                        child_deps.append(get_signal_depth(arg.value))
                    elif arg.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                        child_deps.append(get_signal_depth(arg.value[0] if isinstance(arg.value, tuple) else arg.value))
                    else:
                        pass
                if child_deps:
                    d = max(child_deps) + 1
                else:
                    d = 1
                signal_depth[sig_name] = d
                return d
            signal_depth[sig_name] = 1
            return 1

        max_depth = 0
        for out_sig in c.outputs:
            d = get_signal_depth(out_sig.name)
            if d > max_depth:
                max_depth = d

        depth = max_depth

        # Calculate op_histogram
        op_categories = ['BUF', 'CONCAT', 'NOT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ']
        op_histogram = {cat: 0 for cat in op_categories}

        def classify_expr(expr):
            if expr.node_type == NodeType.IDENTIFIER:
                return 'BUF'
            if expr.node_type == NodeType.LITERAL:
                return 'BUF'
            if expr.node_type == NodeType.BIT_INDEX:
                return 'BUF'
            if expr.node_type == NodeType.BIT_SLICE:
                return 'BUF'
            if expr.node_type == NodeType.CONCAT:
                return 'CONCAT'
            if expr.node_type == NodeType.CALL:
                return expr.value.upper()
            return None

        for lhs, expr in c.assignments.items():
            cat = classify_expr(expr)
            if cat in op_histogram:
                op_histogram[cat] += 1

        result = {
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
        print(json.dumps(result))
        return 0

    return _process(args.file, args, handler)


def dot_command(args) -> int:
    def handler(c, fmt):
        # Handle --cone filtering
        cone_outputs = None
        if args.cone:
            cone_outputs = set(args.cone.split(','))
            # Check all specified outputs exist
            for name in cone_outputs:
                if name not in c.all_signals or name not in c.outputs:
                    raise UndefinedNameError(f"unknown output: {name}")

        # Collect all nodes (signals and operator nodes)
        # For DOT: signal nodes and assignment operator nodes

        # Track which nodes to include (for cone filtering)
        include_signals = set()
        include_ops = set()

        if cone_outputs:
            # Backward cone analysis - start from specified outputs
            to_visit = list(cone_outputs)
            while to_visit:
                sig_name = to_visit.pop()
                if sig_name in include_signals:
                    continue
                # Add this signal
                include_signals.add(sig_name)
                # If it has an assignment, add the operator and its deps
                if sig_name in c.assignments:
                    expr = c.assignments[sig_name]
                    op_node_id = f"op:{sig_name}"
                    if op_node_id not in include_ops:
                        include_ops.add(op_node_id)
                        # Collect dependencies
                        deps = []
                        def collect_deps(e):
                            if e.node_type == NodeType.IDENTIFIER:
                                deps.append(e.value)
                            elif e.node_type == NodeType.CALL:
                                for arg in e.args:
                                    collect_deps(arg)
                            elif e.node_type == NodeType.CONCAT:
                                for arg in e.args:
                                    collect_deps(arg)
                            elif e.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                                base = e.value[0] if isinstance(e.value, tuple) else e.value
                                deps.append(base)
                        collect_deps(expr)
                        for dep in deps:
                            if dep not in include_signals:
                                to_visit.append(dep)
        else:
            # Include everything
            for sig in c.inputs:
                include_signals.add(sig.name)
            for sig in c.outputs:
                include_signals.add(sig.name)
            for sig in c.wires:
                include_signals.add(sig.name)
            include_ops = {f"op:{lhs}" for lhs in c.assignments.keys()}

        # Build nodes list
        nodes = []
        edges = []

        def get_op_label(expr):
            if expr.node_type == NodeType.CALL:
                return expr.value.upper()
            elif expr.node_type == NodeType.CONCAT:
                return "CONCAT"
            else:
                # IDENTIFIER, LITERAL, BIT_INDEX, BIT_SLICE -> BUF
                return "BUF"

        for sig_name in sorted(include_signals):
            if sig_name in c.all_signals:
                sig_info = c.all_signals[sig_name]
                if sig_name in c.inputs:
                    # Input node
                    if sig_info.width == 1:
                        label = sig_name
                    else:
                        label = f"{sig_name}[{sig_info.msb}:{sig_info.lsb}]"
                    nodes.append((f"in:{sig_name}", label))
                else:
                    # Wire or output - use sig: label
                    if sig_info.width == 1:
                        label = sig_name
                    else:
                        label = f"{sig_name}[{sig_info.msb}:{sig_info.lsb}]"
                    nodes.append((f"sig:{sig_name}", label))

        for op_node_name in sorted(include_ops):
            lhs = op_node_name[4:]  # Remove "op:"
            if lhs in c.assignments:
                expr = c.assignments[lhs]
                label = get_op_label(expr)
                nodes.append((op_node_name, label))

        # Build edges
        for lhs, expr in c.assignments.items():
            if f"op:{lhs}" not in include_ops:
                continue
            op_node = f"op:{lhs}"

            def collect_and_add_deps(e):
                deps = []
                if e.node_type == NodeType.IDENTIFIER:
                    deps.append(e.value)
                elif e.node_type == NodeType.CALL:
                    for arg in e.args:
                        deps.extend(collect_and_add_deps(arg))
                elif e.node_type == NodeType.CONCAT:
                    for arg in e.args:
                        deps.extend(collect_and_add_deps(arg))
                elif e.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                    base = e.value[0] if isinstance(e.value, tuple) else e.value
                    deps.append(base)
                return deps

            deps = collect_and_add_deps(expr)

            # Deduplicate edges
            seen_edges = set()
            for dep in deps:
                edge_key = (dep, lhs)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)

                if dep in c.inputs:
                    source = f"in:{dep}"
                else:
                    source = f"sig:{dep}"

                edges.append((source, op_node))

            # Always add edge from op to sig
            target = f"sig:{lhs}"
            edge_key = (op_node, target)
            if edge_key not in seen_edges:
                edges.append((op_node, target))

        # Sort nodes and edges lexicographically
        nodes.sort(key=lambda x: x[0])
        edges.sort(key=lambda x: (x[0], x[1]))

        # Generate DOT content
        lines = ["digraph circopt {", ""]
        for node_id, label in nodes:
            lines.append(f"    {node_id} [label=\"{label}\")")
        if nodes:
            lines.append("")
        for src, tgt in edges:
            lines.append(f"    {src} -> {tgt}")
        lines.append("}")

        dot_content = "\n".join(lines)

        # Write to output file
        with open(args.output, 'w') as f:
            f.write(dot_content)

        result = {
            "ok": True,
            "command": "dot",
            "dot_path": args.output
        }
        if args.json:
            print(json.dumps(result))

        return 0

    return _process(args.file, args, handler)


class UndefinedNameError(CircoptError):
    exit_code = 3; error_type = "UndefinedNameError"


# ==============================================================================
# cone command
# ==============================================================================

def cone_command(args) -> int:
    def handler(c, fmt):
        # Parse output names
        output_names = set(args.outputs.split(',')) if args.outputs else set()

        # Check all specified outputs exist
        for name in output_names:
            if name not in c.all_signals:
                raise UndefinedNameError(f"unknown output: {name}")

        # Determine output format
        out_format = args.out_format
        if out_format is None:
            if args.output.endswith('.circ'):
                out_format = 'circ'
            elif args.output.endswith('.json'):
                out_format = 'json'
            else:
                raise UnknownOutputFormatError(f"Cannot infer output format from extension: {args.output}")

        if out_format == 'bench':
            raise UnsupportedFeatureError("BENCH output not supported in cone command")

        # Perform backward cone analysis
        include_signals = set(output_names)
        to_visit = list(output_names)

        while to_visit:
            sig_name = to_visit.pop()
            if sig_name not in c.assignments:
                continue
            expr = c.assignments[sig_name]

            def collect_deps(e):
                deps = []
                if e.node_type == NodeType.IDENTIFIER:
                    deps.append(e.value)
                elif e.node_type == NodeType.CALL:
                    for arg in e.args:
                        deps.extend(collect_deps(arg))
                elif e.node_type == NodeType.CONCAT:
                    for arg in e.args:
                        deps.extend(collect_deps(arg))
                elif e.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                    base = e.value[0] if isinstance(e.value, tuple) else e.value
                    deps.append(base)
                return deps

            for dep in collect_deps(expr):
                if dep not in include_signals:
                    include_signals.add(dep)
                    to_visit.append(dep)

        # Build the new circuit
        new_circuit = Circuit()

        # Add inputs (sorted by name)
        for sig in c.inputs:
            if sig.name in include_signals:
                new_circuit.inputs.append(sig)
                new_circuit.all_signals[sig.name] = sig

        # Add outputs (sorted by name, only the requested ones)
        for sig in c.outputs:
            if sig.name in output_names:
                new_circuit.outputs.append(sig)
                new_circuit.all_signals[sig.name] = sig

        # Add wires (only those needed, sorted by name)
        for sig in c.wires:
            if sig.name in include_signals:
                new_circuit.wires.append(sig)
                new_circuit.all_signals[sig.name] = sig

        # Add assignments (only those needed, in dependency order)
        # Topological sort: assignments that don't depend on others first
        def get_dependencies(sig_name):
            if sig_name not in c.assignments:
                return set()
            expr = c.assignments[sig_name]
            deps = set()
            def collect(e):
                if e.node_type == NodeType.IDENTIFIER:
                    deps.add(e.value)
                elif e.node_type == NodeType.CALL:
                    for arg in e.args:
                        collect(arg)
                elif e.node_type == NodeType.CONCAT:
                    for arg in e.args:
                        collect(arg)
                elif e.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                    base = e.value[0] if isinstance(e.value, tuple) else e.value
                    deps.add(base)
            collect(expr)
            return deps

        # Get all relevant assignments
        relevant_assignments = {name: c.assignments[name] for name in include_signals if name in c.assignments}

        # Topological sort using Kahn's algorithm
        in_degree = {}
        graph = {}
        for name in relevant_assignments:
            deps = get_dependencies(name) & include_signals
            in_degree[name] = len(deps)
            graph[name] = deps

        # Start with nodes that have no dependencies within include_signals
        queue = [name for name, deg in in_degree.items() if deg == 0]
        sorted_assignments = []

        while queue:
            # Sort for deterministic output
            queue.sort()
            node = queue.pop(0)
            sorted_assignments.append(node)
            for other in relevant_assignments:
                if node in graph[other]:
                    in_degree[other] -= 1
                    if in_degree[other] == 0:
                        queue.append(other)

        # Add any remaining nodes (cycles should have been caught during validation)
        for name in relevant_assignments:
            if name not in sorted_assignments:
                sorted_assignments.append(name)

        # Sort by LHS name for ties (as per spec)
        # Actually, the topological sort already gives dependency order
        # For ties (nodes with same depth), sort by LHS name
        # We need to compute depth for tie-breaking

        def compute_depth(sig_name):
            if sig_name in new_circuit.inputs or sig_name in new_circuit.wires:
                if sig_name in new_circuit.wires:
                    # Check if this wire is assigned from a literal or identifier
                    if sig_name in relevant_assignments:
                        expr = relevant_assignments[sig_name]
                        if expr.node_type in (NodeType.LITERAL, NodeType.IDENTIFIER):
                            return 0
                return 0
            if sig_name not in relevant_assignments:
                return 0
            deps = get_dependencies(sig_name) & include_signals
            if not deps:
                return 0
            return max(compute_depth(d) for d in deps) + 1

        # Sort by depth, then by name for ties
        sorted_assignments.sort(key=lambda n: (compute_depth(n), n))

        # Add assignments in sorted order
        for name in sorted_assignments:
            new_circuit.assignments[name] = relevant_assignments[name]

        # Write output
        if out_format == 'circ':
            # Canonical .circ format
            lines = []

            # Input line (sorted by name)
            input_names = sorted([s.name for s in new_circuit.inputs])
            if input_names:
                lines.append("input " + " ".join(input_names))

            # Output line (sorted by name)
            output_names_sorted = sorted([s.name for s in new_circuit.outputs])
            if output_names_sorted:
                lines.append("output " + " ".join(output_names_sorted))

            # Wire line (sorted by name, omit if none)
            wire_names = sorted([s.name for s in new_circuit.wires])
            if wire_names:
                lines.append("wire " + " ".join(wire_names))

            # Blank line
            lines.append("")

            # Assignments in dependency order with tie-breaking by LHS name
            # We already have sorted_assignments which is topologically sorted
            # But we need to handle tie-breaking within the same depth
            # Re-sort with depth and name
            from collections import defaultdict
            depth_groups = defaultdict(list)
            for name in sorted_assignments:
                d = compute_depth(name)
                depth_groups[d].append(name)

            final_order = []
            for d in sorted(depth_groups.keys()):
                depth_groups[d].sort()
                final_order.extend(depth_groups[d])

            for name in final_order:
                expr = new_circuit.assignments[name]
                rhs_str = _format_expr_circ(expr)
                lines.append(f"{name} = {rhs_str}")

            content = "\n".join(lines) + "\n"

            with open(args.output, 'w') as f:
                f.write(content)

        elif out_format == 'json':
            # Canonical JSON format
            result = {
                "format_version": 1,
                "inputs": sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in new_circuit.inputs], key=lambda x: x["name"]),
                "outputs": sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in new_circuit.outputs], key=lambda x: x["name"]),
                "wires": sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in new_circuit.wires], key=lambda x: x["name"]),
                "assignments": []
            }

            # Assignments in dependency order with tie-breaking
            from collections import defaultdict
            depth_groups = defaultdict(list)
            for name in sorted_assignments:
                d = compute_depth(name)
                depth_groups[d].append(name)

            final_order = []
            for d in sorted(depth_groups.keys()):
                depth_groups[d].sort()
                final_order.extend(depth_groups[d])

            for name in final_order:
                expr = new_circuit.assignments[name]
                rhs_str = _format_expr_circ(expr)
                result["assignments"].append({"lhs": name, "rhs": rhs_str})

            with open(args.output, 'w') as f:
                json.dump(result, f, indent=2)

        return 0

    return _process(args.file, args, handler)


def _format_expr_circ(expr: ExprNode) -> str:
    """Format an expression in canonical .circ format."""
    if expr.node_type == NodeType.IDENTIFIER:
        return expr.value
    if expr.node_type == NodeType.LITERAL:
        return expr.value
    if expr.node_type == NodeType.CALL:
        op = expr.value.upper()
        # Flatten nested associative operators and sort arguments
        if op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            all_args = []
            for arg in expr.args:
                if arg.node_type == NodeType.CALL and arg.value.upper() == op:
                    all_args.extend(arg.args)
                else:
                    all_args.append(arg)
            # Sort arguments lexicographically by their string representation
            arg_strs = []
            for arg in all_args:
                s = _format_expr_circ(arg)
                arg_strs.append(s)
            arg_strs.sort()
            return f"{op}({', '.join(arg_strs)})"
        else:
            arg_strs = [_format_expr_circ(arg) for arg in expr.args]
            return f"{op}({', '.join(arg_strs)})"
    if expr.node_type == NodeType.CONCAT:
        arg_strs = [_format_expr_circ(arg) for arg in expr.args]
        return "{" + ", ".join(arg_strs) + "}"
    if expr.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
        base = expr.value[0] if isinstance(expr.value, tuple) else expr.value
        if isinstance(expr.value, tuple) and len(expr.value) == 2:
            # BIT_INDEX
            idx = expr.value[1]
            return f"{base}[{idx}]"
        else:
            # BIT_SLICE
            msb, lsb = expr.value[1], expr.value[2]
            return f"{base}[{msb}:{lsb}]"
    return str(expr.value)


# ==============================================================================
# truth-table command
# ==============================================================================

def truth_table_command(args) -> int:
    def handler(c, fmt):
        # Check 2val only
        # (truth table is for 2val only per spec)

        # Check input bit limit
        total_input_bits = sum(inp.width for inp in c.inputs)
        if total_input_bits > args.max_input_bits:
            raise InputLimitExceeded(f"Total input bits {total_input_bits} exceeds limit {args.max_input_bits}")

        # Expand inputs: sort by name, then expand bits
        sorted_inputs = sorted(c.inputs, key=lambda x: x.name)
        input_bits = []  # List of (signal_name, bit_index) tuples
        for inp in sorted_inputs:
            for bit_idx in range(inp.msb, inp.lsb - 1, -1):  # MSB to LSB
                input_bits.append((inp.name, bit_idx))

        M = len(input_bits)
        num_rows = 1 << M

        radix = args.radix or 'bin'

        # Prepare output
        output_names = sorted([s.name for s in c.outputs], key=lambda x: x.name)
        input_names = sorted([s.name for s in c.inputs], key=lambda x: x.name)

        if args.json:
            # JSON output
            rows = []
            for row_idx in range(num_rows):
                # Generate input values
                inputs_dict = {}
                for i, (sig_name, bit_idx) in enumerate(input_bits):
                    bit_val = (row_idx >> (M - 1 - i)) & 1
                    if sig_name not in inputs_dict:
                        inputs_dict[sig_name] = []
                    inputs_dict[sig_name].append(str(bit_val))

                # Format inputs
                formatted_inputs = {}
                for sig_name in input_names:
                    # Get the signal info
                    sig = c.all_signals[sig_name]
                    bits = inputs_dict[sig_name]
                    if sig.width == 1:
                        formatted_inputs[sig_name] = bits[0]
                    else:
                        # For vector, we need to format according to radix
                        # But for JSON truth table, we use binary format per bit
                        val = int(''.join(bits), 2)
                        if radix == 'bin':
                            formatted_inputs[sig_name] = ''.join(bits)
                        elif radix == 'hex':
                            formatted_inputs[sig_name] = hex(val)[2:].upper()
                        else:  # dec
                            formatted_inputs[sig_name] = str(val)

                # Evaluate circuit
                eval_inputs = {}
                for sig_name in input_names:
                    # For evaluation, we need the integer value
                    bits = inputs_dict[sig_name]
                    eval_inputs[sig_name] = int(''.join(bits), 2)

                outputs = evaluate_circuit(c, eval_inputs, mode='2val')

                formatted_outputs = {}
                for out_name in output_names:
                    val = outputs[out_name]
                    sig = c.all_signals[out_name]
                    if sig.width == 1:
                        formatted_outputs[out_name] = str(val)
                    else:
                        if radix == 'bin':
                            formatted_outputs[out_name] = bin(val)[2:].zfill(sig.width)
                        elif radix == 'hex':
                            formatted_outputs[out_name] = hex(val)[2:].upper()
                        else:  # dec
                            formatted_outputs[out_name] = str(val)

                rows.append({
                    "inputs": formatted_inputs,
                    "outputs": formatted_outputs
                })

            result = {
                "ok": True,
                "command": "truth-table",
                "radix": radix,
                "inputs": [{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in sorted_inputs],
                "outputs": [{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in sorted(c.outputs, key=lambda x: x.name)],
                "rows": rows
            }
            print(json.dumps(result))
            return 0
        else:
            # CSV output
            # Header
            header = input_names + output_names
            print(",".join(header))

            for row_idx in range(num_rows):
                # Generate input values
                inputs_dict = {}
                for i, (sig_name, bit_idx) in enumerate(input_bits):
                    bit_val = (row_idx >> (M - 1 - i)) & 1
                    if sig_name not in inputs_dict:
                        inputs_dict[sig_name] = []
                    inputs_dict[sig_name].append(str(bit_val))

                # Evaluate circuit
                eval_inputs = {}
                for sig_name in input_names:
                    bits = inputs_dict[sig_name]
                    eval_inputs[sig_name] = int(''.join(bits), 2)

                outputs = evaluate_circuit(c, eval_inputs, mode='2val')

                # Format row values
                row_values = []
                for sig_name in input_names:
                    bits = inputs_dict[sig_name]
                    if len(bits) == 1:
                        row_values.append(bits[0])
                    else:
                        val = int(''.join(bits), 2)
                        if radix == 'bin':
                            row_values.append(''.join(bits))
                        elif radix == 'hex':
                            row_values.append(hex(val)[2:].upper())
                        else:  # dec
                            row_values.append(str(val))

                for out_name in output_names:
                    val = outputs[out_name]
                    sig = c.all_signals[out_name]
                    if sig.width == 1:
                        row_values.append(str(val))
                    else:
                        if radix == 'bin':
                            row_values.append(bin(val)[2:].zfill(sig.width))
                        elif radix == 'hex':
                            row_values.append(hex(val)[2:].upper())
                        else:  # dec
                            row_values.append(str(val))

                print(",".join(row_values))

            return 0

    return _process(args.file, args, handler)


# ==============================================================================
# equiv command
# ==============================================================================

def equiv_command(args) -> int:
    def handler(c_a, fmt_a):
        # Process second circuit
        try:
            with open(args.file_b) as f:
                text_b = f.read()
        except FileNotFoundError:
            print(json.dumps(CliUsageError(f"File not found: {args.file_b}").to_dict("equiv")), file=sys.stderr)
            return 1

        # Determine format for second file
        fmt_b = args.format
        if fmt_b == 'auto':
            if args.file_b.endswith('.circ'):
                fmt_b = 'circ'
            elif args.file_b.endswith('.json'):
                fmt_b = 'json'
            elif args.file_b.endswith('.bench'):
                fmt_b = 'bench'
            else:
                e = UnknownInputFormatError(f"Cannot determine format from extension: {args.file_b}", args.file_b)
                print(json.dumps(e.to_dict("equiv")), file=sys.stderr)
                return e.exit_code

        try:
            if fmt_b == 'circ':
                tokens = tokenize(text_b, args.file_b)
                stmts = parse(tokens, args.file_b)
                c_b = validate(stmts, args.file_b)
            elif fmt_b == 'json':
                c_b = parse_json(text_b, args.file_b)
            elif fmt_b == 'bench':
                c_b = parse_bench(text_b, args.file_b)
            else:
                raise ValueError(f"Unknown format: {fmt_b}")
        except CircoptError as e:
            if e.file is None:
                e.file = args.file_b
            print(json.dumps(e.to_dict("equiv")), file=sys.stderr)
            return e.exit_code

        # Check port compatibility
        # Inputs must match exactly: same names, same msb, same lsb
        inputs_a_sorted = sorted(c_a.inputs, key=lambda x: x.name)
        inputs_b_sorted = sorted(c_b.inputs, key=lambda x: x.name)

        if len(inputs_a_sorted) != len(inputs_b_sorted):
            raise PortMismatchError(f"Input count mismatch: {len(inputs_a_sorted)} vs {len(inputs_b_sorted)}")

        for ia, ib in zip(inputs_a_sorted, inputs_b_sorted):
            if ia.name != ib.name or ia.msb != ib.msb or ia.lsb != ib.lsb:
                raise PortMismatchError(f"Input port mismatch: {ia.name}[{ia.msb}:{ia.lsb}] vs {ib.name}[{ib.msb}:{ib.lsb}]")

        # Outputs: must match unless --outputs restricts
        if args.outputs:
            output_names = set(args.outputs.split(','))
            # Check all specified outputs exist in both circuits
            for name in output_names:
                if name not in c_a.all_signals or name not in c_b.all_signals:
                    raise UndefinedNameError(f"unknown output: {name}")
            # Filter outputs to only those specified
            outputs_a = [s for s in c_a.outputs if s.name in output_names]
            outputs_b = [s for s in c_b.outputs if s.name in output_names]
        else:
            # Must match exactly
            outputs_a_sorted = sorted(c_a.outputs, key=lambda x: x.name)
            outputs_b_sorted = sorted(c_b.outputs, key=lambda x: x.name)

            if len(outputs_a_sorted) != len(outputs_b_sorted):
                raise PortMismatchError(f"Output count mismatch: {len(outputs_a_sorted)} vs {len(outputs_b_sorted)}")

            for oa, ob in zip(outputs_a_sorted, outputs_b_sorted):
                if oa.name != ob.name or oa.msb != ob.msb or oa.lsb != ob.lsb:
                    raise PortMismatchError(f"Output port mismatch: {oa.name}[{oa.msb}:{oa.lsb}] vs {ob.name}[{ob.msb}:{ob.lsb}]")

            outputs_a = outputs_a_sorted
            outputs_b = outputs_b_sorted

        # Sort outputs by name
        outputs_a = sorted(outputs_a, key=lambda x: x.name)
        outputs_b = sorted(outputs_b, key=lambda x: x.name)

        # Get total input bits
        sorted_inputs = sorted(c_a.inputs, key=lambda x: x.name)
        input_bits = []
        for inp in sorted_inputs:
            for bit_idx in range(inp.msb, inp.lsb - 1, -1):
                input_bits.append((inp.name, bit_idx))

        M = len(input_bits)
        num_combinations = 1 << M

        # Set random seed if provided
        if args.seed is not None:
            import random
            random.seed(args.seed)

        # Determine method
        if M <= args.max_input_bits:
            # Exhaustive check
            method = "exhaustive"
            comparisons = num_combinations

            for row_idx in range(num_combinations):
                # Generate input values
                eval_inputs = {}
                for i, (sig_name, bit_idx) in enumerate(input_bits):
                    bit_val = (row_idx >> (M - 1 - i)) & 1
                    if sig_name not in eval_inputs:
                        eval_inputs[sig_name] = []
                    eval_inputs[sig_name].append(bit_val)

                # Convert to integer values for each input signal
                final_inputs = {}
                for sig_name in [s.name for s in sorted_inputs]:
                    bits = eval_inputs[sig_name]
                    final_inputs[sig_name] = int(''.join(str(b) for b in bits), 2)

                # Evaluate both circuits
                outs_a = evaluate_circuit(c_a, final_inputs, mode='2val')
                outs_b = evaluate_circuit(c_b, final_inputs, mode='2val')

                # Compare outputs
                for out_a, out_b in zip(outputs_a, outputs_b):
                    if outs_a[out_a.name] != outs_b[out_b.name]:
                        # Found counterexample
                        counterexample_inputs = []
                        for sig_name in [s.name for s in sorted_inputs]:
                            sig = c_a.all_signals[sig_name]
                            bits = eval_inputs[sig_name]
                            val_str = ''.join(str(b) for b in bits)
                            counterexample_inputs.append({
                                "name": sig_name,
                                "msb": sig.msb,
                                "lsb": sig.lsb,
                                "value": val_str
                            })

                        a_outputs = [{"name": out.name, "msb": out.msb, "lsb": out.lsb,
                                       "value": str(outs_a[out.name])} for out in outputs_a]
                        b_outputs = [{"name": out.name, "msb": out.msb, "lsb": out.lsb,
                                       "value": str(outs_b[out.name])} for out in outputs_b]

                        result = {
                            "ok": True,
                            "command": "equiv",
                            "equivalent": False,
                            "method": method,
                            "comparisons": row_idx + 1,
                            "counterexample": {
                                "inputs": counterexample_inputs,
                                "outputs_compared": [out.name for out in outputs_a],
                                "a_outputs": a_outputs,
                                "b_outputs": b_outputs
                            }
                        }
                        print(json.dumps(result))
                        return 10

            # All combinations checked, circuits are equivalent
            result = {
                "ok": True,
                "command": "equiv",
                "equivalent": True,
                "method": method,
                "comparisons": comparisons
            }
            print(json.dumps(result))
            return 0

        else:
            # Randomized check
            method = "randomized"
            trials = args.trials

            for _ in range(trials):
                # Generate random inputs
                eval_inputs = {}
                for sig_name in [s.name for s in sorted_inputs]:
                    sig = c_a.all_signals[sig_name]
                    width = sig.width
                    # Generate random bits
                    rand_val = 0
                    for _ in range(width):
                        rand_val = (rand_val << 1) | (1 if random.random() < 0.5 else 0)
                    eval_inputs[sig_name] = rand_val

                # Evaluate both circuits
                outs_a = evaluate_circuit(c_a, eval_inputs, mode='2val')
                outs_b = evaluate_circuit(c_b, eval_inputs, mode='2val')

                # Compare outputs
                for out_a, out_b in zip(outputs_a, outputs_b):
                    if outs_a[out_a.name] != outs_b[out_b.name]:
                        # Found counterexample - need to format inputs as binary strings
                        counterexample_inputs = []
                        for sig_name in [s.name for s in sorted_inputs]:
                            sig = c_a.all_signals[sig_name]
                            val = eval_inputs[sig_name]
                            val_str = bin(val)[2:].zfill(sig.width)
                            counterexample_inputs.append({
                                "name": sig_name,
                                "msb": sig.msb,
                                "lsb": sig.lsb,
                                "value": val_str
                            })

                        a_outputs = [{"name": out.name, "msb": out.msb, "lsb": out.lsb,
                                       "value": str(outs_a[out.name])} for out in outputs_a]
                        b_outputs = [{"name": out.name, "msb": out.msb, "lsb": out.lsb,
                                       "value": str(outs_b[out.name])} for out in outputs_b]

                        result = {
                            "ok": True,
                            "command": "equiv",
                            "equivalent": False,
                            "method": method,
                            "comparisons": _ + 1,
                            "counterexample": {
                                "inputs": counterexample_inputs,
                                "outputs_compared": [out.name for out in outputs_a],
                                "a_outputs": a_outputs,
                                "b_outputs": b_outputs
                            }
                        }
                        print(json.dumps(result))
                        return 10

            # All trials passed, circuits are equivalent
            result = {
                "ok": True,
                "command": "equiv",
                "equivalent": True,
                "method": method,
                "comparisons": trials
            }
            print(json.dumps(result))
            return 0

    # Parse first circuit
    try:
        with open(args.file_a) as f:
            text_a = f.read()
    except FileNotFoundError:
        print(json.dumps(CliUsageError(f"File not found: {args.file_a}").to_dict("equiv")), file=sys.stderr)
        return 1

    # Determine format for first file
    fmt_a = args.format
    if fmt_a == 'auto':
        if args.file_a.endswith('.circ'):
            fmt_a = 'circ'
        elif args.file_a.endswith('.json'):
            fmt_a = 'json'
        elif args.file_a.endswith('.bench'):
            fmt_a = 'bench'
        else:
            e = UnknownInputFormatError(f"Cannot determine format from extension: {args.file_a}", args.file_a)
            print(json.dumps(e.to_dict("equiv")), file=sys.stderr)
            return e.exit_code

    try:
        if fmt_a == 'circ':
            tokens = tokenize(text_a, args.file_a)
            stmts = parse(tokens, args.file_a)
            c_a = validate(stmts, args.file_a)
        elif fmt_a == 'json':
            c_a = parse_json(text_a, args.file_a)
        elif fmt_a == 'bench':
            c_a = parse_bench(text_a, args.file_a)
        else:
            raise ValueError(f"Unknown format: {fmt_a}")
    except CircoptError as e:
        if e.file is None:
            e.file = args.file_a
        print(json.dumps(e.to_dict("equiv")), file=sys.stderr)
        return e.exit_code

    return handler(c_a, fmt_a)


def lint_command(args) -> int:
    def handler(c, fmt):
        warnings = []

        # Check for unused inputs
        # Build the set of signals used by outputs
        used_signals = set()

        def collect_used(expr):
            if expr.node_type == NodeType.IDENTIFIER:
                used_signals.add(expr.value)
            elif expr.node_type == NodeType.CALL:
                for arg in expr.args:
                    collect_used(arg)
            elif expr.node_type == NodeType.CONCAT:
                for arg in expr.args:
                    collect_used(arg)
            elif expr.node_type in (NodeType.BIT_INDEX, NodeType.BIT_SLICE):
                base = expr.value[0] if isinstance(expr.value, tuple) else expr.value
                used_signals.add(base)

        # Collect all signals used by outputs (transitively)
        for out_sig in c.outputs:
            collect_used(c.assignments[out_sig.name])

        # Check each input is used
        for inp in c.inputs:
            if inp.name not in used_signals:
                warnings.append({
                    "type": "UnusedInputWarning",
                    "name": inp.name,
                    "message": f"input '{inp.name}' is not used by any output"
                })

        # Check for constant outputs
        max_input_bits = args.max_input_bits

        # Calculate total input bits
        total_input_bits = sum(inp.width for inp in c.inputs)

        if total_input_bits <= max_input_bits:
            # Exhaustive check - evaluate all 2^M input combinations
            from itertools import product

            # For each input, generate all combinations
            num_inputs = len(c.inputs)
            input_names = [inp.name for inp in c.inputs]

            # For each output, track if it varies
            output_values = {}

            for bits in product([0, 1], repeat=num_inputs):
                inputs_dict = {}
                for i, name in enumerate(input_names):
                    inputs_dict[name] = bits[i]

                # Evaluate the circuit
                try:
                    results = evaluate_circuit(c, inputs_dict, mode='2val')
                except Exception:
                    # Skip if evaluation fails
                    continue

                for out_sig in c.outputs:
                    val = results[out_sig.name]
                    if out_sig.name not in output_values:
                        output_values[out_sig.name] = []
                    output_values[out_sig.name].append(val)

            # Check each output for constant values
            for out_sig in c.outputs:
                if out_sig.name in output_values:
                    vals = output_values[out_sig.name]
                    if all(v == vals[0] for v in vals):
                        # Constant output
                        val = vals[0]
                        if out_sig.width == 1:
                            value_str = str(val)
                        else:
                            # For vectors, need to format properly (they're ints now)
                            value_str = str(val)
                        warnings.append({
                            "type": "ConstantOutputWarning",
                            "name": out_sig.name,
                            "message": f"output '{out_sig.name}' is constant",
                            "value": value_str
                        })
        else:
            # Skip exhaustive check
            warnings.append({
                "type": "ConstantOutputCheckSkippedWarning",
                "name": None,
                "message": f"constant output check skipped: {total_input_bits} input bits exceeds limit {max_input_bits}"
            })

        # Sort warnings lexicographically by (type, name)
        warnings.sort(key=lambda w: (w["type"], w.get("name") or ""))

        result = {
            "ok": True,
            "command": "lint",
            "warnings": warnings
        }
        print(json.dumps(result))
        return 0

    return _process(args.file, args, handler)


def version_command(args) -> int:
    v = "0.1.0"
    if args.json:
        print(json.dumps({"ok": True, "command": "__version__", "version": v}))
    else:
        print(v)
    return 0


# ==============================================================================
# Main
# ==============================================================================

def main() -> int:
    P = argparse.ArgumentParser(prog='circopt', add_help=False)
    P.add_argument('--help', action='store_true')
    P.add_argument('--version', action='store_true')
    P.add_argument('--json', action='store_true')
    P.add_argument('--seed', type=int, default=None,
                   help='Random seed for reproducible results')
    args, remaining = P.parse_known_args(sys.argv[1:])

    if args.help:
        P.print_help(); return 0
    if args.version:
        return version_command(args)
    if not remaining:
        print(json.dumps(CliUsageError("No command specified. Use 'check <file.circ>' to validate a circuit file.").to_dict("__cli__")), file=sys.stderr)
        return 1

    cmd = remaining[0]

    if cmd == 'check':
        cp = argparse.ArgumentParser(prog='circopt check', add_help=False)
        cp.add_argument('file', help='Circuit file to check')
        cp.add_argument('--json', action='store_true')
        cp.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        cp.command = 'check'
        cp_args, _ = cp.parse_known_args(remaining[1:])
        return check_command(cp_args)

    if cmd == 'eval':
        ep = argparse.ArgumentParser(prog='circopt eval', add_help=False)
        ep.add_argument('file', help='Circuit file to evaluate')
        ep.add_argument('--set', dest='set_val', action='append', default=[], help='Set input value (name=value)')
        ep.add_argument('--default', choices=['0', '1'], help='Default value for unspecified inputs')
        ep.add_argument('--allow-extra', action='store_true', help='Allow extra inputs')
        ep.add_argument('--mode', choices=['2val', '3val'], default='2val', help='Evaluation mode (default: 2val)')
        ep.add_argument('--radix', choices=['bin', 'hex', 'dec'], default='bin', help='Output radix (default: bin)')
        ep.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        ep.add_argument('--json', action='store_true')
        ep.command = 'eval'
        ep_args, _ = ep.parse_known_args(remaining[1:])
        if ep_args.default:
            ep_args.default = (ep_args.default == '1')
        else:
            ep_args.default = False
        return eval_command(ep_args)

    if cmd == 'stats':
        sp = argparse.ArgumentParser(prog='circopt stats', add_help=False)
        sp.add_argument('file', help='Circuit file to analyze')
        sp.add_argument('--json', action='store_true')
        sp.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        sp.command = 'stats'
        sp_args, _ = sp.parse_known_args(remaining[1:])
        return stats_command(sp_args)

    if cmd == 'lint':
        lp = argparse.ArgumentParser(prog='circopt lint', add_help=False)
        lp.add_argument('file', help='Circuit file to lint')
        lp.add_argument('--max-input-bits', type=int, default=16,
                        help='Maximum input bits for exhaustive constant check (default: 16)')
        lp.add_argument('--json', action='store_true')
        lp.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        lp.command = 'lint'
        lp_args, _ = lp.parse_known_args(remaining[1:])
        return lint_command(lp_args)

    if cmd == 'dot':
        dp = argparse.ArgumentParser(prog='circopt dot', add_help=False)
        dp.add_argument('file', help='Circuit file to export')
        dp.add_argument('-o', '--output', required=True, help='Output DOT file path')
        dp.add_argument('--cone', help='Only include nodes in backward cone of specified outputs (comma-separated)')
        dp.add_argument('--json', action='store_true')
        dp.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        dp.command = 'dot'
        dp_args, _ = dp.parse_known_args(remaining[1:])
        return dot_command(dp_args)

    if cmd == 'cone':
        cp = argparse.ArgumentParser(prog='circopt cone', add_help=False)
        cp.add_argument('file', help='Circuit file')
        cp.add_argument('-o', '--output', required=True, help='Output file path')
        cp.add_argument('--outputs', required=True, help='Comma-separated list of output signals to extract')
        cp.add_argument('--out-format', choices=['circ', 'json'], help='Output format (default: infer from -o extension)')
        cp.add_argument('--json', action='store_true')
        cp.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        # Inherit --seed from global
        cp_args, _ = cp.parse_known_args(remaining[1:])
        cp_args.seed = args.seed
        cp.command = 'cone'
        return cone_command(cp_args)

    if cmd == 'truth-table':
        tt = argparse.ArgumentParser(prog='circopt truth-table', add_help=False)
        tt.add_argument('file', help='Circuit file')
        tt.add_argument('--max-input-bits', type=int, required=True,
                        help='Maximum number of input bits for truth table')
        tt.add_argument('--radix', choices=['bin', 'hex', 'dec'], default='bin',
                        help='Output radix (default: bin)')
        tt.add_argument('-o', help='Output file path (CSV if not --json)')
        tt.add_argument('--out-format', choices=['csv', 'json'], help='Output format (default: infer from -o or --json)')
        tt.add_argument('--json', action='store_true', help='Output JSON to stdout')
        tt.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format (default: auto)')
        tt_args, _ = tt.parse_known_args(remaining[1:])
        tt_args.seed = args.seed
        tt.command = 'truth-table'
        return truth_table_command(tt_args)

    if cmd == 'equiv':
        eq = argparse.ArgumentParser(prog='circopt equiv', add_help=False)
        eq.add_argument('file_a', help='First circuit file')
        eq.add_argument('file_b', help='Second circuit file')
        eq.add_argument('--max-input-bits', type=int, required=True,
                        help='Maximum input bits for exhaustive check')
        eq.add_argument('--trials', type=int, default=10000,
                        help='Number of randomized trials (default: 10000)')
        eq.add_argument('--outputs', help='Comma-separated outputs to compare (default: all)')
        eq.add_argument('--json', action='store_true', help='Output JSON to stdout')
        eq.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                        help='Input format for both files (default: auto)')
        eq_args, _ = eq.parse_known_args(remaining[1:])
        eq_args.seed = args.seed
        eq.command = 'equiv'
        return equiv_command(eq_args)

    print(json.dumps(CliUsageError(f"Unknown command: {cmd}").to_dict("__cli__")), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
