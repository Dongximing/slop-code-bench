#!/usr/bin/env python3
"""
Circuit optimizer and validator for .circ files.
"""

import argparse
import json
import sys
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field


# Version string
__version__ = "0.1.0"


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


# =============================================================================
# Parser classes
# =============================================================================

class TokenType(Enum):
    IDENTIFIER = "IDENTIFIER"
    NUMBER = "NUMBER"
    LPAREN = "("
    RPAREN = ")"
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
            start_pos = self.pos

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
            elif ch.isdigit():
                start_pos = self.pos
                while self.pos < len(self.text) and self.text[self.pos].isdigit():
                    self.pos += 1
                    self.col += 1
                value = self.text[start_pos:self.pos]
                self._add_token(TokenType.NUMBER, value, self.line, self.col)
            elif ch == '(':
                self._add_token(TokenType.LPAREN, '(', self.line, self.col)
                self.pos += 1
                self.col += 1
            elif ch == ')':
                self._add_token(TokenType.RPAREN, ')', self.line, self.col)
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


class Parser:
    """Parser for .circ files."""

    # Supported operators and their arities
    OPERATORS = {
        'NOT': 1,
        'BUF': 1,
        'AND': -1,  # >= 2
        'OR': -1,
        'XOR': -1,
        'NAND': -1,
        'NOR': -1,
        'XNOR': -1,
    }

    def __init__(self, tokens: list[Token], filename: str):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0
        self.assignments_started = False

    def parse(self) -> dict:
        """Parse the circuit file and return declarations and assignments."""
        result = {
            'inputs': set(),
            'outputs': set(),
            'wires': set(),
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

        all_signals = result['inputs'] | result['outputs'] | result['wires']

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

        # Parse names
        names = []
        while self.pos < len(self.tokens):
            token = self.peek()
            if token.type in (TokenType.NEWLINE, TokenType.EOF):
                break
            if token.type != TokenType.IDENTIFIER:
                raise CircParseError(
                    f"Expected identifier, got '{token.value}'",
                    self.filename, token.line, token.col
                )
            # Check for duplicate
            name = token.value
            target_set = {'INPUT': result['inputs'], 'OUTPUT': result['outputs'], 'WIRE': result['wires']}[decl_type]

            # Check for duplicate across all signals
            all_signals = result['inputs'] | result['outputs'] | result['wires']
            if name in all_signals:
                raise DuplicateNameError(f"Duplicate name '{name}'")

            target_set.add(name)
            names.append(name)
            self.consume()

        # Consume newline
        if self.pos < len(self.tokens) and self.peek().type == TokenType.NEWLINE:
            self.consume()

        return names

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
        """Parse an expression: identifier, literal, or OP(...)"""
        token = self.consume()

        if token.type == TokenType.IDENTIFIER:
            # Check for forbidden literal X
            if token.value == 'X':
                raise CircParseError(
                    "Forbidden literal 'X' value",
                    self.filename, token.line, token.col
                )
            # Could be a literal (0 or 1) or an identifier
            if token.value in ('0', '1'):
                return ('literal', token.value)
            # Check if it's an operator call
            next_token = self.peek()
            if next_token.type == TokenType.LPAREN:
                return self._parse_operator_call(token)
            return ('identifier', token.value)
        elif token.type == TokenType.LPAREN:
            # Start of operator call
            return self._parse_operator_call_in_parens()
        else:
            raise CircParseError(
                f"Expected expression, got '{token.value}'",
                self.filename, token.line, token.col
            )

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

        return ('call', op_name, args)

    def _parse_operator_call_in_parens(self):
        """Parse an operator call that starts with (OP(...))"""
        # Consume the opening paren
        self.consume()

        # The next token should be the operator name
        op_token = self.consume()
        if op_token.type != TokenType.IDENTIFIER:
            raise CircParseError(
                f"Expected operator name, got '{op_token.value}'",
                self.filename, op_token.line, op_token.col
            )

        # Parse the operator call
        result = self._parse_operator_call(op_token)

        # Consume the closing paren for the whole expression
        if self.pos < len(self.tokens) and self.peek().type == TokenType.RPAREN:
            self.consume()

        return result

    def peek(self, offset: int = 0) -> Token:
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
# Validator
# =============================================================================

@dataclass
class Circuit:
    inputs: set[str]
    outputs: set[str]
    wires: set[str]
    assignments: list[tuple[str, tuple]]  # (lhs, rhs)


def validate_circuit(circuit: dict, filename: str) -> Circuit:
    """Validate a parsed circuit and check for errors."""
    inputs = circuit['inputs']
    outputs = circuit['outputs']
    wires = circuit['wires']
    assignments = circuit['assignments']

    # Check that all LHS are valid
    for lhs, _ in assignments:
        if lhs in inputs:
            raise InputAssignmentError(f"Cannot assign to input '{lhs}'")
        if lhs not in outputs and lhs not in wires:
            raise UndefinedNameError(f"Undefined signal '{lhs}'")

    # Build dependency graph for cycle detection
    deps = {name: set() for name in wires | outputs}

    def collect_deps(expr, signal_name):
        if expr[0] == 'identifier':
            dep_name = expr[1]
            if dep_name in inputs | wires | outputs:
                deps[signal_name].add(dep_name)
        elif expr[0] == 'literal':
            pass
        elif expr[0] == 'call':
            for arg in expr[2]:
                collect_deps(arg, signal_name)

    for lhs, rhs in assignments:
        collect_deps(rhs, lhs)

    # Check for cycles
    visited = set()
    rec_stack = set()
    cycle_path = []

    def dfs(node):
        visited.add(node)
        rec_stack.add(node)
        cycle_path.append(node)

        for neighbor in deps.get(node, set()):
            if neighbor not in inputs and neighbor not in wires | outputs:
                continue  # Skip non-signal dependencies
            if neighbor not in visited:
                if dfs(neighbor):
                    return True
            elif neighbor in rec_stack:
                # Found a cycle - extract it
                start_idx = cycle_path.index(neighbor)
                cycle = cycle_path[start_idx:] + [neighbor]
                raise CycleError(cycle)

        cycle_path.pop()
        rec_stack.remove(node)
        return False

    for node in wires | outputs:
        if node not in visited:
            dfs(node)

    return Circuit(inputs=inputs, outputs=outputs, wires=wires, assignments=assignments)


# =============================================================================
# 2-valued evaluation
# =============================================================================


def evaluate_circuit(
    circuit: Circuit,
    inputs: dict[str, int],
    default: int = 0,
    allow_extra: bool = False
) -> dict[str, int]:
    """Evaluate a circuit with given input values.

    Args:
        circuit: The validated circuit to evaluate.
        inputs: Dictionary mapping input names to their values (0 or 1).
        default: Default value for inputs not provided (0 or 1).
        allow_extra: If True, ignore extra inputs not in the circuit.

    Returns:
        Dictionary mapping output names to their computed values.

    Raises:
        MissingInputError: If a required input is missing.
        UnknownInputError: If an unknown input is provided and allow_extra is False.
        InputValueParseError: If an input value is not 0 or 1.
    """
    # Validate input values
    for name, value in inputs.items():
        if value not in (0, 1):
            raise InputValueParseError(
                f"Input '{name}' value must be 0 or 1, got '{value}'"
            )

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
        values[name] = 0  # temporary

    for name in circuit.outputs:
        values[name] = 0  # temporary

    # Define Boolean operators
    operators = {
        'NOT': lambda args: 1 - args[0],
        'BUF': lambda args: args[0],
        'AND': lambda args: int(all(args)),
        'OR': lambda args: int(any(args)),
        'XOR': lambda args: sum(args) % 2,
        'NAND': lambda args: 1 - int(all(args)),
        'NOR': lambda args: 1 - int(any(args)),
        'XNOR': lambda args: 1 - (sum(args) % 2),
    }

    def eval_expr(expr: tuple) -> int:
        """Evaluate an expression and return its value."""
        if expr[0] == 'identifier':
            name = expr[1]
            if name in inputs:
                return inputs.get(name, default)
            return values[name]
        elif expr[0] == 'literal':
            return int(expr[1])
        elif expr[0] == 'call':
            op_name = expr[1]
            args = [eval_expr(arg) for arg in expr[2]]
            return operators[op_name](args)
        else:
            raise ValueError(f"Unknown expression type: {expr[0]}")

    # Topologically sort assignments for evaluation
    # Build dependency graph
    deps: dict[str, set[str]] = {name: set() for name in circuit.wires | circuit.outputs}

    def collect_expr_deps(expr: tuple, signal_name: str):
        if expr[0] == 'identifier':
            dep_name = expr[1]
            if dep_name in circuit.inputs | circuit.wires | circuit.outputs:
                deps[signal_name].add(dep_name)
        elif expr[0] == 'call':
            for arg in expr[2]:
                collect_expr_deps(arg, signal_name)

    for lhs, rhs in circuit.assignments:
        if lhs in deps:  # Only track wires and outputs
            collect_expr_deps(rhs, lhs)

    # Topological sort using DFS
    visited: set[str] = set()
    temp_visited: set[str] = set()
    order: list[str] = []

    def topological_sort(node: str):
        if node in temp_visited:
            raise CycleError([node])  # Should not happen after validation
        if node in visited:
            return
        temp_visited.add(node)
        for neighbor in deps.get(node, set()):
            if neighbor in circuit.wires | circuit.outputs:
                topological_sort(neighbor)
        temp_visited.remove(node)
        visited.add(node)
        order.append(node)

    for node in circuit.wires | circuit.outputs:
        topological_sort(node)

    # Evaluate in order
    for name in order:
        # Find the assignment for this signal
        for lhs, rhs in circuit.assignments:
            if lhs == name:
                values[name] = eval_expr(rhs)
                break

    # Return only output values
    result = {}
    for name in sorted(circuit.outputs):
        result[name] = values[name]

    return result


# =============================================================================
# CLI
# =============================================================================

def output_json(command: str, data: dict):
    """Output JSON to stdout."""
    print(json.dumps(data))


def output_plain(command: str, data: dict):
    """Output plain text to stdout."""
    if command == '__version__':
        print(data['version'])
    elif command == 'check':
        # Format inputs and outputs
        inputs = sorted(data['inputs'], key=lambda x: x['name'])
        outputs = sorted(data['outputs'], key=lambda x: x['name'])
        print(f"Inputs: {', '.join(i['name'] for i in inputs)}")
        print(f"Outputs: {', '.join(o['name'] for o in outputs)}")


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
    filename: str
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
            value = int(value_str)
        except ValueError:
            if json_flag:
                print(json.dumps({
                    "ok": False,
                    "command": "eval",
                    "exit_code": 2,
                    "error": {
                        "type": "InputValueParseError",
                        "message": f"Invalid value for input '{name}': '{value_str}' (must be 0 or 1)",
                        "file": None,
                        "line": None,
                        "col": None
                    }
                }))
            else:
                print(f"Error: Invalid value for input '{name}': '{value_str}' (must be 0 or 1)")
            return 2
        inputs[name] = value

    # Evaluate the circuit
    try:
        results = evaluate_circuit(circuit, inputs, default, allow_extra)
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

    # Output results
    if json_flag:
        outputs = []
        for name, value in results.items():
            outputs.append({
                "name": name,
                "msb": 0,
                "lsb": 0,
                "value": str(value)
            })
        data = {
            "ok": True,
            "command": "eval",
            "mode": "2val",
            "radix": "bin",
            "outputs": outputs
        }
        output_json("eval", data)
    else:
        for name, value in sorted(results.items()):
            print(f"{name}={value}")

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
    inputs = sorted([{"name": name, "msb": 0, "lsb": 0} for name in circuit.inputs],
                    key=lambda x: x['name'])
    outputs = sorted([{"name": name, "msb": 0, "lsb": 0} for name in circuit.outputs],
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
    import sys

    # If no arguments provided, show error
    if len(sys.argv) == 1:
        # Check for --json flag
        json_flag = '--json' in sys.argv
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "__cli__",
                "exit_code": 1,
                "error": {
                    "type": "CliUsageError",
                    "message": "No command specified",
                    "file": None,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print("Error: No command specified")
            print("Usage: python circopt.py <command> [options]")
            print("Commands: check, eval")
        return 1

    # Check for global flags
    args = sys.argv[1:]

    # Handle --help
    if '--help' in args:
        parser = argparse.ArgumentParser(
            prog="circopt",
            description="Circuit optimizer and validator for .circ files"
        )
        parser.add_argument('--help', action='store_true', help='Show help message')
        parser.add_argument('--version', action='store_true', help='Show version')
        parser.add_argument('--json', action='store_true', help='Output in JSON format')
        parser.print_help()
        return 0

    # Handle --version
    if '--version' in args:
        json_flag = '--json' in args
        return handle_version(json_flag)

    # Parse the command
    if args[0] == 'check':
        json_flag = '--json' in args
        # Find the filename (first non-flag argument after 'check')
        filename = None
        for arg in args[1:]:
            if arg != '--json':
                filename = arg
                break

        if filename is None:
            if json_flag:
                print(json.dumps({
                    "ok": False,
                    "command": "check",
                    "exit_code": 1,
                    "error": {
                        "type": "CliUsageError",
                        "message": "Missing required argument: <file.circ>",
                        "file": None,
                        "line": None,
                        "col": None
                    }
                }))
            else:
                print("Error: Missing required argument: <file.circ>")
            return 1

        return handle_check(filename, json_flag, "check")
    elif args[0] == 'eval':
        json_flag = '--json' in args
        # Parse --set arguments
        set_inputs: list[str] = []
        default = 0
        allow_extra = False

        i = 1
        while i < len(args):
            arg = args[i]
            if arg == '--set':
                if i + 1 < len(args):
                    set_inputs.append(args[i + 1])
                    i += 2
                else:
                    if json_flag:
                        print(json.dumps({
                            "ok": False,
                            "command": "eval",
                            "exit_code": 1,
                            "error": {
                                "type": "CliUsageError",
                                "message": "Missing value for --set",
                                "file": None,
                                "line": None,
                                "col": None
                            }
                        }))
                    else:
                        print("Error: Missing value for --set")
                    return 1
            elif arg == '--default':
                if i + 1 < len(args):
                    try:
                        default = int(args[i + 1])
                        if default not in (0, 1):
                            if json_flag:
                                print(json.dumps({
                                    "ok": False,
                                    "command": "eval",
                                    "exit_code": 2,
                                    "error": {
                                        "type": "InputValueParseError",
                                        "message": f"--default must be 0 or 1, got '{args[i + 1]}'",
                                        "file": None,
                                        "line": None,
                                        "col": None
                                    }
                                }))
                            else:
                                print(f"Error: --default must be 0 or 1, got '{args[i + 1]}'")
                            return 2
                    except ValueError:
                        if json_flag:
                            print(json.dumps({
                                "ok": False,
                                "command": "eval",
                                "exit_code": 2,
                                "error": {
                                    "type": "InputValueParseError",
                                    "message": f"--default must be 0 or 1, got '{args[i + 1]}'",
                                    "file": None,
                                    "line": None,
                                    "col": None
                                }
                            }))
                        else:
                            print(f"Error: --default must be 0 or 1, got '{args[i + 1]}'")
                        return 2
                    i += 2
                else:
                    if json_flag:
                        print(json.dumps({
                            "ok": False,
                            "command": "eval",
                            "exit_code": 1,
                            "error": {
                                "type": "CliUsageError",
                                "message": "Missing value for --default",
                                "file": None,
                                "line": None,
                                "col": None
                            }
                        }))
                    else:
                        print("Error: Missing value for --default")
                    return 1
            elif arg == '--allow-extra':
                allow_extra = True
                i += 1
            elif arg == '--json':
                i += 1
            else:
                # This is the filename
                filename = arg
                break

        if filename is None:
            if json_flag:
                print(json.dumps({
                    "ok": False,
                    "command": "eval",
                    "exit_code": 1,
                    "error": {
                        "type": "CliUsageError",
                        "message": "Missing required argument: <file.circ>",
                        "file": None,
                        "line": None,
                        "col": None
                    }
                }))
            else:
                print("Error: Missing required argument: <file.circ>")
            return 1

        return handle_eval(filename, set_inputs, default, allow_extra, json_flag, "eval")
    else:
        json_flag = '--json' in args
        if json_flag:
            print(json.dumps({
                "ok": False,
                "command": "__cli__",
                "exit_code": 1,
                "error": {
                    "type": "CliUsageError",
                    "message": f"Unknown command '{args[0]}'",
                    "file": None,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print(f"Error: Unknown command '{args[0]}'")
        return 1


if __name__ == '__main__':
    sys.exit(main())
