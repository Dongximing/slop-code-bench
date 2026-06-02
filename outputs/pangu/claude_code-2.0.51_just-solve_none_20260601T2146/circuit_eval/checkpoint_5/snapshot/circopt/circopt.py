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

def _e(name: str, exit_code: int, base: type = None) -> type:
    """Create error class inline."""
    class E(base or CircoptError):
        exit_code = exit_code
        error_type = name
    return E


class CircoptError(Exception):
    """Base error with JSON serialization."""
    exit_code: int = 1
    error_type = "CircoptError"

    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self, command: str) -> dict:
        d = {"ok": False, "command": command, "exit_code": self.exit_code,
             "error": {"type": self.error_type, "message": self.message}}
        if self.file: d["error"]["file"] = self.file
        if self.line: d["error"]["line"] = self.line
        if self.col: d["error"]["col"] = self.col
        return d


CliUsageError = _e("CliUsageError", 1)
CircParseError = _e("CircParseError", 2)
MissingInputError = _e("MissingInputError", 1)
UnknownInputError = _e("UnknownInputError", 1)
InputValueParseError = _e("InputValueParseError", 2)
WidthMismatchError = _e("WidthMismatchError", 3)
IndexOutOfBoundsError = _e("IndexOutOfBoundsError", 3)
InputWidthMismatchError = _e("InputWidthMismatchError", 3)
RadixNotAllowedIn3ValError = _e("RadixNotAllowedIn3ValError", 1)

# New errors for Part 5
UnknownInputFormatError = _e("UnknownInputFormatError", 1)
JsonParseError = _e("JsonParseError", 2)
JsonSchemaError = _e("JsonSchemaError", 3)
BenchParseError = _e("BenchParseError", 2)
RedefinitionError = _e("RedefinitionError", 3)


class ValidationError(CircoptError):
    exit_code = 3; error_type = "ValidationError"


# Validation errors
decl_after_assign = lambda n="": ValidationError("Declaration after assignment: '" + n + "'" if n else "Declaration after assignment")
duplicate_name = lambda n: ValidationError("Duplicate name: '" + n + "'")
undefined_name = lambda n: ValidationError("Undefined name: '" + n + "'")
unassigned_signal = lambda n: ValidationError("Unassigned signal: '" + n + "'")
input_assignment = lambda n: ValidationError("Cannot assign to input: '" + n + "'")
multiple_assignment = lambda n: ValidationError("Multiple assignment to: '" + n + "'")
arity_error = lambda op, e, g: ValidationError(f"Arity error: {op} expects {'>=' if op in ('AND','OR','XOR','NAND','NOR','XNOR') else ''}{e}, got {g}")
cycle_error = lambda p: ValidationError("Cycle detected: " + " -> ".join(p))


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
    rhs: ExprNode = None


@dataclass
class ExprNode(Node):
    value: str = ""
    args: list = field(default_factory=list)


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


# ==============================================================================
# Validator
# ==============================================================================

@dataclass
class SignalInfo:
    name: str; msb: int; lsb: int
    @property
    def width(self): return self.msb - self.lsb + 1


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: dict = field(default_factory=dict)
    all_signals: dict = field(default_factory=dict)


def validate(stmts: list, filename: Optional[str] = None) -> Circuit:
    SUPPORTED_OPS = {
        'NOT': 1, 'BUF': 1,
        'AND': 'multiple', 'OR': 'multiple', 'XOR': 'multiple',
        'NAND': 'multiple', 'NOR': 'multiple', 'XNOR': 'multiple',
        'MUX': 3, 'ITE': 3,
        'REDUCE_AND': 1, 'REDUCE_OR': 1, 'REDUCE_XOR': 1,
        'EQ': 2,
    }

    circuit = Circuit()
    assignments_seen = False

    # First pass: declarations
    for s in stmts:
        if isinstance(s, DeclarationNode):
            if assignments_seen: raise decl_after_assign(s.names[0] if s.names else "")
            for n, msb, lsb in s.names:
                if n in circuit.all_signals: raise duplicate_name(n)
                si = SignalInfo(n, msb, lsb)
                circuit.all_signals[n] = si
                if s.node_type == NodeType.INPUT: circuit.inputs.append(si)
                elif s.node_type == NodeType.OUTPUT: circuit.outputs.append(si)
                else: circuit.wires.append(si)

    # Second pass: assignments
    for s in stmts:
        if isinstance(s, AssignmentNode):
            assignments_seen = True
            lhs = s.lhs
            if lhs not in circuit.all_signals: raise undefined_name(lhs)
            if lhs in circuit.inputs: raise input_assignment(lhs)
            if lhs in circuit.assignments: raise multiple_assignment(lhs)

            def validate_expr(e: ExprNode):
                if e.node_type == NodeType.IDENTIFIER:
                    if e.value not in circuit.all_signals: raise undefined_name(e.value)
                elif e.node_type == NodeType.LITERAL:
                    pass
                elif e.node_type == NodeType.CALL:
                    op = e.value.upper()
                    if op not in SUPPORTED_OPS: raise CircParseError(f"Unknown operator: {op}", filename, e.line, e.col)
                    exp = SUPPORTED_OPS[op]; got = len(e.args)
                    if isinstance(exp, str):
                        if exp == 'multiple' and got < 1: raise arity_error(op, exp, got)
                    elif got != exp: raise arity_error(op, exp, got)
                    for arg in e.args: validate_expr(arg)

            validate_expr(s.rhs)
            circuit.assignments[lhs] = s.rhs

    # Check all wires/outputs assigned
    for sig in circuit.wires + circuit.outputs:
        if sig.name not in circuit.assignments: raise unassigned_signal(sig.name)

    # Check for cycles
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
    for sig in circuit.outputs + circuit.wires:
        if sig.name not in visited and dfs(sig.name): return

    return circuit


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

    # Helper to validate port object
    def validate_port(port, port_type):
        if not isinstance(port, dict):
            raise JsonSchemaError(f"{port_type} must be an object", filename)
        if 'name' not in port:
            raise JsonSchemaError(f"{port_type} missing 'name' field", filename)
        name = port['name']
        msb = port.get('msb', 0)
        lsb = port.get('lsb', 0)
        if not isinstance(msb, int) or not isinstance(lsb, int):
            raise JsonSchemaError(f"{port_type} '{name}': msb and lsb must be integers", filename)
        if msb < lsb:
            raise JsonSchemaError(f"{port_type} '{name}': msb ({msb}) must be >= lsb ({lsb})", filename)
        if lsb < 0:
            raise JsonSchemaError(f"{port_type} '{name}': lsb must be >= 0", filename)
        return name, msb, lsb

    # Parse inputs
    if not isinstance(data['inputs'], list):
        raise JsonSchemaError("'inputs' must be a list", filename)
    for i, port in enumerate(data['inputs']):
        name, msb, lsb = validate_port(port, f"inputs[{i}]")
        if name in seen_signals:
            raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
        si = SignalInfo(name, msb, lsb)
        circuit.inputs.append(si)
        circuit.all_signals[name] = si
        seen_signals.add(name)

    # Parse outputs
    if not isinstance(data['outputs'], list):
        raise JsonSchemaError("'outputs' must be a list", filename)
    for i, port in enumerate(data['outputs']):
        name, msb, lsb = validate_port(port, f"outputs[{i}]")
        if name in seen_signals:
            raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
        si = SignalInfo(name, msb, lsb)
        circuit.outputs.append(si)
        circuit.all_signals[name] = si
        seen_signals.add(name)

    # Parse wires
    if not isinstance(data['wires'], list):
        raise JsonSchemaError("'wires' must be a list", filename)
    for i, port in enumerate(data['wires']):
        name, msb, lsb = validate_port(port, f"wires[{i}]")
        if name in seen_signals:
            raise RedefinitionError(f"Signal '{name}' defined multiple times", filename)
        si = SignalInfo(name, msb, lsb)
        circuit.wires.append(si)
        circuit.all_signals[name] = si
        seen_signals.add(name)

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

    # Check for cycles
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
    for sig in circuit.outputs + circuit.wires:
        if sig.name not in visited and dfs(sig.name): return

    return circuit


def parse_expr_from_tokens(tokens: list, filename: Optional[str] = None) -> ExprNode:
    """Parse expression from tokens (simplified parser for JSON RHS)."""
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
            OPS_JSON = {'NOT', 'BUF', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'}
            if op in OPS_JSON:
                return parse_call()
            if re.match(r'^\d+[b-h](.+)$', t.value):
                advance()
                return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)
            raise CircParseError(f"Unknown operator: {op}", filename, t.line, t.col)

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

    def parse_call() -> ExprNode:
        op = expect(TokenType.OP, "Expected operator name")
        expect(TokenType.LPAREN, f"Expected '(' after '{op.value}'")
        args = [parse_expr()] if peek() and peek().type != TokenType.RPAREN else []
        while peek() and peek().type == TokenType.COMMA:
            advance()
            args.append(parse_expr())
        expect(TokenType.RPAREN, "Expected ')' after call")
        return ExprNode(NodeType.CALL, op.line, op.col, op.value, args)

    expr = parse_expr()
    if peek() is not None:
        raise CircParseError(f"Unexpected trailing tokens in expression", filename, current_line, 1)
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
            import re
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

    print(json.dumps(CliUsageError(f"Unknown command: {cmd}").to_dict("__cli__")), file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
