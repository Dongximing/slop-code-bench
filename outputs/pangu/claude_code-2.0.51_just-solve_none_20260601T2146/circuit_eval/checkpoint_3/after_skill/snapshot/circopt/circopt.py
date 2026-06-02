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
    exit_code: int = 1
    error_type = "CircoptError"

    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self, command: str) -> dict:
        r = {"ok": False, "command": command, "exit_code": self.exit_code,
             "error": {"type": self.error_type, "message": self.message}}
        if self.file is not None: r["error"]["file"] = self.file
        if self.line is not None: r["error"]["line"] = self.line
        if self.col is not None: r["error"]["col"] = self.col
        return r


def _make_subclass(name, exit_code):
    return type(name, (CircoptError,), {
        "exit_code": exit_code,
        "error_type": name,
        "__init__": lambda self, msg, *a: super().__init__(msg, *a)
    })


CliUsageError = _make_subclass("CliUsageError", 1)
CircParseError = _make_subclass("CircParseError", 2)
MissingInputError = _make_subclass("MissingInputError", 1)
UnknownInputError = _make_subclass("UnknownInputError", 1)
InputValueParseError = _make_subclass("InputValueParseError", 2)
WidthMismatchError = _make_subclass("WidthMismatchError", 3)
IndexOutOfBoundsError = _make_subclass("IndexOutOfBoundsError", 3)
InputWidthMismatchError = _make_subclass("InputWidthMismatchError", 3)


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
    line: int = 0
    col: int = 0


@dataclass
class DeclarationNode(Node):
    names: list = field(default_factory=list)


@dataclass
class AssignmentNode(Node):
    lhs: str = ""
    rhs: 'ExprNode' = None  # forward reference


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
        return self.text[self.pos] if self.pos < len(self.text) else None

    def advance(self) -> str:
        ch = self.text[self.pos]
        self.pos += 1
        if ch == '\n': self.line += 1; self.col = 1
        else: self.col += 1
        return ch

    def skip_ws_comments(self):
        while self.pos < len(self.text):
            ch = self.peek()
            if ch.isspace(): self.advance()
            elif ch == '#':
                while self.pos < len(self.text) and self.peek() != '\n':
                    self.advance()
            else: break

    def next_token(self) -> Token:
        self.skip_ws_comments()
        if self.pos >= len(self.text):
            return Token(TokenType.EOF, '', self.line, self.col)

        ch = self.peek()
        sl, sc = self.line, self.col

        # Identifier or keyword
        if ch.isalpha() or ch == '_':
            v = self.advance()
            while self.pos < len(self.text):
                nxt = self.peek()
                if nxt.isalnum() or nxt == '_': v += self.advance()
                else: break
            return Token(TokenType.OP if v.upper() in self.OPS else TokenType.IDENTIFIER, v, sl, sc)

        # Number or binary/hex literal
        if ch.isdigit():
            first = self.advance()
            if first == '0' and self.pos < len(self.text) and self.peek() in ('b', 'x', 'B', 'X'):
                pref = self.advance().lower()
                v = '0' + pref; has_digit = False
                while self.pos < len(self.text):
                    nxt = self.peek()
                    if nxt.isalnum() or nxt == '_':
                        if nxt.isalnum(): has_digit = True
                        v += self.advance()
                    else: break
                if not has_digit:
                    raise CircParseError(f"Unsized literal must have at least one digit after prefix '0{pref}'",
                                          self.filename, sl, sc)
                return Token(TokenType.LITERAL, v, sl, sc)
            v = first
            while self.pos < len(self.text) and self.peek().isdigit():
                v += self.advance()
            return Token(TokenType.NUMBER, v, sl, sc)

        # Single character tokens
        single = {'=': TokenType.EQUALS, ',': TokenType.COMMA, '(': TokenType.LPAREN,
                   ')': TokenType.RPAREN, '[': TokenType.LBRACKET, ']': TokenType.RBRACKET,
                   ':': TokenType.COLON, '{': TokenType.LBRACE, '}': TokenType.RBRACE}
        if ch in single:
            self.advance()
            return Token(single[ch], ch, sl, sc)

        raise CircParseError(f"Unexpected character: '{ch}'", self.filename, sl, sc)

    def tokenize(self) -> list:
        tokens = []
        while True:
            t = self.next_token()
            tokens.append(t)
            if t.type == TokenType.EOF:
                break
        return tokens


# ==============================================================================
# Parser
# ==============================================================================

class Parser:

    def __init__(self, tokens: list, filename: Optional[str] = None):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0
        self.current_line = 1

    def peek(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def advance(self) -> Token:
        t = self.peek()
        if t is not None:
            self.pos += 1
            self.current_line = t.line
        return t

    def expect(self, expected: TokenType, msg: str) -> Token:
        t = self.peek()
        if t is None or t.type != expected:
            raise CircParseError(f"{msg}, got {t.type.name if t else 'EOF'}: '{t.value if t else ''}'",
                                  self.filename, self.current_line, 1)
        return self.advance()

    def parse_expr(self) -> ExprNode:
        t = self.peek()
        if t is None:
            raise CircParseError("Unexpected end of file in expression", self.filename, self.current_line, 1)

        if t.type == TokenType.IDENTIFIER:
            self.advance()
            if self.peek() and self.peek().type == TokenType.LBRACKET:
                return self.parse_bit_slice(t.line, t.col, t.value)
            return ExprNode(NodeType.IDENTIFIER, t.line, t.col, t.value)

        if t.type == TokenType.NUMBER:
            self.advance()
            if t.value not in ('0', '1'):
                raise CircParseError(f"Invalid literal: {t.value}. Only 0 and 1 are allowed.",
                                      self.filename, t.line, t.col)
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.LITERAL:
            self.advance()
            return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)

        if t.type == TokenType.OP:
            op = t.value.upper()
            if op in self.OPS or op in {'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}:
                return self.parse_call()
            if self.is_sized_literal(t.value):
                self.advance()
                return ExprNode(NodeType.LITERAL, t.line, t.col, t.value)
            raise CircParseError(f"Unknown operator: {op}", self.filename, t.line, t.col)

        if t.type == TokenType.LBRACE:
            return self.parse_concat()

        raise CircParseError(f"Unexpected token in expression: {t.type.name}: '{t.value}'",
                              self.filename, t.line, t.col)

    @staticmethod
    def is_sized_literal(v: str) -> bool:
        return bool(re.match(r'^\d+[b-h](.+)$', v))

    def parse_bit_slice(self, line: int, col: int, name: str) -> ExprNode:
        self.expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
        idx = int(self.expect(TokenType.NUMBER, "Expected index number").value)
        if self.peek() and self.peek().type == TokenType.COLON:
            self.advance()
            lo = int(self.expect(TokenType.NUMBER, "Expected low bound").value)
            self.expect(TokenType.RBRACKET, "Expected ']' after slice")
            return ExprNode(NodeType.BIT_SLICE, line, col, (name, idx, lo))
        self.expect(TokenType.RBRACKET, "Expected ']' after index")
        return ExprNode(NodeType.BIT_INDEX, line, col, (name, idx))

    def parse_concat(self) -> ExprNode:
        sl = self.current_line
        sc = self.peek().col if self.peek() else 1
        self.expect(TokenType.LBRACE, "Expected '{' for concatenation")
        args = [self.parse_expr()]
        while self.peek() and self.peek().type == TokenType.COMMA:
            self.advance()
            args.append(self.parse_expr())
        self.expect(TokenType.RBRACE, "Expected '}' after concatenation")
        return ExprNode(NodeType.CONCAT, sl, sc, args)

    def parse_call(self) -> ExprNode:
        op = self.expect(TokenType.OP, "Expected operator name")
        self.expect(TokenType.LPAREN, f"Expected '(' after '{op.value}'")
        args = [self.parse_expr()] if self.peek() and self.peek().type != TokenType.RPAREN else []
        while self.peek() and self.peek().type == TokenType.COMMA:
            self.advance()
            args.append(self.parse_expr())
        self.expect(TokenType.RPAREN, "Expected ')' after call")
        return ExprNode(NodeType.CALL, op.line, op.col, op.value, args)

    def parse_declaration(self) -> DeclarationNode:
        t = self.expect(TokenType.OP, "Expected declaration type (input, output, wire)")
        t_upper = t.value.upper()
        if t_upper not in {'INPUT', 'OUTPUT', 'WIRE'}:
            raise CircParseError(f"Expected declaration type, got '{t.value}'", self.filename, t.line, t.col)

        names = []
        while True:
            tok = self.peek()
            if (tok is None or tok.type == TokenType.EOF or tok.type != TokenType.IDENTIFIER or
                tok.line != t.line):
                break
            self.advance()
            name = tok.value
            msb = lsb = 0
            if self.peek() and self.peek().type == TokenType.LBRACKET:
                self.expect(TokenType.LBRACKET, f"Expected '[' after '{name}'")
                msb = int(self.expect(TokenType.NUMBER, f"Expected MSB for '{name}'").value)
                self.expect(TokenType.COLON, f"Expected ':' after MSB for '{name}'")
                lsb = int(self.expect(TokenType.NUMBER, f"Expected LSB for '{name}'").value)
                self.expect(TokenType.RBRACKET, f"Expected ']' after LSB for '{name}'")
                if msb < lsb:
                    raise CircParseError(f"MSB must be >= LSB for '{name}'", self.filename, tok.line, tok.col)
                if lsb < 0:
                    raise CircParseError(f"LSB must be >= 0 for '{name}'", self.filename, tok.line, tok.col)
            names.append((name, msb, lsb))

        if not names:
            raise CircParseError("Expected names after declaration type", self.filename, t.line, t.col)

        nt = (NodeType.INPUT if t_upper == "INPUT" else NodeType.OUTPUT if t_upper == "OUTPUT" else NodeType.WIRE)
        return DeclarationNode(nt, t.line, t.col, names)

    def parse_assignment(self) -> AssignmentNode:
        lt = self.expect(TokenType.IDENTIFIER, "Expected LHS identifier")
        lhs = lt.value
        self.expect(TokenType.EQUALS, f"Expected '=' after LHS '{lhs}'")
        rhs = self.parse_expr()
        return AssignmentNode(NodeType.ASSIGNMENT, line=lt.line, col=lt.col, lhs=lhs, rhs=rhs)

    def parse_line(self) -> Optional[Node]:
        t = self.peek()
        if t is None or t.type == TokenType.EOF:
            return None
        if t.type == TokenType.OP and t.value.upper() in {'INPUT', 'OUTPUT', 'WIRE'}:
            return self.parse_declaration()
        if t.type == TokenType.IDENTIFIER:
            return self.parse_assignment()
        raise CircParseError(f"Unexpected token: {t.type.name}: '{t.value}'", self.filename, t.line, t.col)

    OPS = {'NOT', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'BUF', 'MUX', 'ITE', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'}

    def parse(self) -> list:
        stmts = []; p = self.pos
        while True:
            stmt = self.parse_line()
            if stmt is None:
                if p == self.pos: break  # didn't advance - stop
                p = self.pos
                continue
            stmts.append(stmt)
            p = self.pos
        return stmts


# ==============================================================================
# Validator
# ==============================================================================

@dataclass
class SignalInfo:
    name: str
    msb: int
    lsb: int
    @property
    def width(self): return self.msb - self.lsb + 1


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: dict = field(default_factory=dict)
    all_signals: dict = field(default_factory=dict)


class Validator:
    SUPPORTED_OPS = {
        'NOT': 1, 'BUF': 1,
        'AND': 'multiple', 'OR': 'multiple', 'XOR': 'multiple',
        'NAND': 'multiple', 'NOR': 'multiple', 'XNOR': 'multiple',
        'MUX': 3, 'ITE': 3,
        'REDUCE_AND': 1, 'REDUCE_OR': 1, 'REDUCE_XOR': 1,
        'EQ': 2,
    }

    def __init__(self, filename: Optional[str] = None):
        self.filename = filename
        self.circuit = Circuit()
        self.assignments_seen = False

    def validate(self, stmts: list) -> Circuit:
        # First pass: declarations
        for s in stmts:
            if isinstance(s, DeclarationNode):
                if self.assignments_seen: raise decl_after_assign(s.names[0] if s.names else "")
                self._proc_decl(s)
        # Second pass: assignments
        for s in stmts:
            if isinstance(s, AssignmentNode):
                self.assignments_seen = True
                self._proc_assign(s)
        self._check_all_assigned()
        self._check_cycles()
        return self.circuit

    def _proc_decl(self, d: DeclarationNode):
        for n, msb, lsb in d.names:
            if n in self.circuit.all_signals: raise duplicate_name(n)
            si = SignalInfo(n, msb, lsb)
            self.circuit.all_signals[n] = si
            if d.node_type == NodeType.INPUT: self.circuit.inputs.append(si)
            elif d.node_type == NodeType.OUTPUT: self.circuit.outputs.append(si)
            else: self.circuit.wires.append(si)

    def _proc_assign(self, a: AssignmentNode):
        lhs = a.lhs
        if lhs not in self.circuit.all_signals: raise undefined_name(lhs)
        if lhs in self.circuit.inputs: raise input_assignment(lhs)
        if lhs in self.circuit.assignments: raise multiple_assignment(lhs)
        self._validate_expr(a.rhs)
        self.circuit.assignments[lhs] = a.rhs

    def _validate_expr(self, e: ExprNode):
        if e.node_type == NodeType.IDENTIFIER:
            if e.value not in self.circuit.all_signals: raise undefined_name(e.value)
        elif e.node_type == NodeType.LITERAL:
            pass
        elif e.node_type == NodeType.CALL:
            op = e.value.upper()
            if op not in self.SUPPORTED_OPS: raise CircParseError(f"Unknown operator: {op}", self.filename, e.line, e.col)
            exp = self.SUPPORTED_OPS[op]; got = len(e.args)
            if isinstance(exp, str):
                if got < exp: raise arity_error(op, exp, got)
            else:
                if got != exp: raise arity_error(op, exp, got)
            for arg in e.args: self._validate_expr(arg)

    def _check_all_assigned(self):
        for sig in self.circuit.wires + self.circuit.outputs:
            if sig.name not in self.circuit.assignments: raise unassigned_signal(sig.name)

    def _check_cycles(self):
        visited = set(); rec = set(); path = []
        def dfs(n):
            visited.add(n); rec.add(n); path.append(n)
            if n in self.circuit.assignments:
                for dep in _get_identifiers(self.circuit.assignments[n]):
                    if dep not in self.circuit.all_signals: continue
                    if dep not in visited:
                        if dfs(dep): return True
                    elif dep in rec:
                        raise cycle_error(path[path.index(dep):] + [dep])
            path.pop(); rec.remove(n); return False
        for name in self.circuit.outputs + self.circuit.wires:
            if name not in visited and dfs(name): return


def _get_identifiers(expr: ExprNode) -> list:
    if expr.node_type == NodeType.IDENTIFIER: return [expr.value]
    if expr.node_type == NodeType.LITERAL: return []
    if expr.node_type == NodeType.CALL: return [n for arg in expr.args for n in _get_identifiers(arg)]
    return []


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


def evaluate_circuit(circuit: Circuit, inputs: dict, default: int = 0) -> dict:
    for n in inputs:
        if n not in circuit.inputs: raise UnknownInputError(n)
    for sig in circuit.inputs:
        if sig.name not in inputs: raise MissingInputError(sig.name)
    for n, v in inputs.items():
        if v not in (0, 1): raise InputValueParseError(str(v))

    vals = {s.name: inputs.get(s.name, default) for s in circuit.inputs}
    max_iter = len(circuit.all_signals) + 1
    for _ in range(max_iter):
        changed = False
        for name, expr in circuit.assignments.items():
            if name in vals: continue
            deps = _get_identifiers(expr)
            if all(d in vals for d in deps):
                vals[name] = _eval_expr(expr, vals)
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

    try:
        tokens = Tokenizer(text, filename).tokenize()
        stmts = Parser(tokens, filename).parse()
        circuit = Validator(filename).validate(stmts)
    except CircoptError as e:
        if e.file is None: e.file = filename
        print(json.dumps(e.to_dict(args.command)), file=sys.stderr)
        return e.exit_code

    return handler(circuit, args)


def eval_command(args) -> int:
    def handler(c, a):
        inputs = {}
        if a.set:
            for s in a.set:
                if '=' not in s:
                    print(json.dumps(CliUsageError(f"Invalid --set format: '{s}'. Expected name=value").to_dict("eval")), file=sys.stderr)
                    return 1
                n, v = s.split('=', 1)
                try: v = int(v.strip())
                except ValueError:
                    print(json.dumps(InputValueParseError(v.strip()).to_dict("eval")), file=sys.stderr)
                    return 2
                inputs[n.strip()] = v
        try:
            outputs = evaluate_circuit(c, inputs, default=1 if a.default else 0)
        except CircoptError as e:
            print(json.dumps(e.to_dict("eval")), file=sys.stderr)
            return e.exit_code

        if a.json:
            print(json.dumps({"ok": True, "command": "eval", "mode": "2val", "radix": "bin",
                               "outputs": [{"name": n, "msb": 0, "lsb": 0, "value": str(v)} for n, v in sorted(outputs.items())]}))
        else:
            for n in sorted(outputs): print(f"{n}={outputs[n]}")
        return 0

    return _process(args.file, args, handler)


def check_command(args) -> int:
    def handler(c, _):
        ins = sorted(({"name": s.name, "msb": 0, "lsb": 0} for s in c.inputs), key=lambda x: x["name"])
        outs = sorted(({"name": s.name, "msb": 0, "lsb": 0} for s in c.outputs), key=lambda x: x["name"])
        print(json.dumps({"ok": True, "command": "check", "format": "circ", "inputs": ins, "outputs": outs}))
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
        cp.command = 'check'
        cp_args, _ = cp.parse_known_args(remaining[1:])
        return check_command(cp_args)

    if cmd == 'eval':
        ep = argparse.ArgumentParser(prog='circopt eval', add_help=False)
        ep.add_argument('file', help='Circuit file to evaluate')
        ep.add_argument('--set', dest='set_val', action='append', default=[], help='Set input value (name=value)')
        ep.add_argument('--default', choices=['0', '1'], help='Default value for unspecified inputs')
        ep.add_argument('--allow-extra', action='store_true', help='Allow extra inputs')
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
