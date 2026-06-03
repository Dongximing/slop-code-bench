#!/usr/bin/env python3
"""Debug version of circopt."""

import sys
import json
import re
from enum import auto
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple, Set
from collections import defaultdict

# Version
VERSION = "0.1.0"

# =============================================================================
# Error Classes
# =============================================================================

class CircoptError(Exception):
    """Base exception for circopt errors."""
    pass


class CliUsageError(CircoptError):
    """CLI usage error - exit code 1."""
    pass


class FileNotFoundError(CircoptError):
    """File not found - exit code 1."""
    pass


class CircParseError(CircoptError):
    """Parse error in circuit file - exit code 2."""
    def __init__(self, message: str, file: str, line: int, col: int):
        super().__init__(message)
        self.file = file
        self.line = line
        self.col = col


class DeclarationAfterAssignmentError(CircoptError):
    """Declaration after assignment - exit code 3."""
    pass


class DuplicateNameError(CircoptError):
    """Duplicate name declaration - exit code 3."""
    pass


class UndefinedNameError(CircoptError):
    """Undefined name used - exit code 3."""
    pass


class UnassignedSignalError(CircoptError):
    """Signal not assigned - exit code 3."""
    pass


class InputAssignmentError(CircoptError):
    """Tried to assign to input - exit code 3."""
    pass


class MultipleAssignmentError(CircoptError):
    """Multiple assignments to same signal - exit code 3."""
    pass


class ArityError(CircoptError):
    """Wrong operator arity - exit code 3."""
    pass


class CycleError(CircoptError):
    """Cycle detected in assignment graph - exit code 3."""
    def __init__(self, message: str, cycle: List[str]):
        super().__init__(message)
        self.cycle = cycle


# =============================================================================
# AST Nodes
# =============================================================================

@dataclass
class Signal:
    name: str
    is_input: bool = False
    is_output: bool = False
    assigned: bool = False


@dataclass
class Expr:
    pass


@dataclass
class ExprIdentifier(Expr):
    name: str


@dataclass
class ExprLiteral(Expr):
    value: bool  # True for 1, False for 0


@dataclass
class ExprCall(Expr):
    op: str
    args: List[Expr]


@dataclass
class Assignment:
    lhs: str
    expr: Expr
    line: int
    col: int


# =============================================================================
# Parser
# =============================================================================

# Operator arities
OPERATOR_ARITIES = {
    'NOT': 1,
    'BUF': 1,
    'AND': 2,   # minimum
    'OR': 2,
    'XOR': 2,
    'NAND': 2,
    'NOR': 2,
    'XNOR': 2,
}

VALID_OPERATORS = set(OPERATOR_ARITIES.keys())


class Parser:
    def __init__(self, text: str, filename: str):
        self.text = text
        self.filename = filename
        self.lines = text.split('\n')
        self.line_num = 0
        self.col_num = 0
        self.pos = 0
        self.len = len(text)

        # Track declarations phase
        self.in_declarations = True
        self.inputs: Set[str] = set()
        self.outputs: Set[str] = set()
        self.wires: Set[str] = set()
        self.all_names: Set[str] = set()

        # Track assignments
        self.assignments: List[Assignment] = []
        self.assigned_names: Set[str] = set()

    def debug(self, msg):
        print(f"DEBUG: {msg}")

    def peek(self) -> Optional[str]:
        """Peek at next character without consuming."""
        if self.pos >= self.len:
            return None
        return self.text[self.pos]

    def get_char(self) -> Optional[str]:
        """Get next character and advance."""
        if self.pos >= self.len:
            return None
        char = self.text[self.pos]
        self.pos += 1
        if char == '\n':
            self.line_num += 1
            self.col_num = 0
        else:
            self.col_num += 1
        return char

    def skip_whitespace(self):
        """Skip whitespace and comments."""
        while self.pos < self.len:
            char = self.peek()
            if char is None:
                break
            if char.isspace():
                self.get_char()
            elif char == '#':
                # Skip comment to end of line
                while self.pos < self.len:
                    c = self.get_char()
                    if c == '\n' or c is None:
                        break
            else:
                break

    def get_identifier(self) -> str:
        """Parse an identifier."""
        char = self.peek()
        if not char or (not char.isalpha() and char != '_'):
            raise self.error(f"Expected identifier, got {repr(char)}")

        result = []
        while self.pos < self.len:
            c = self.peek()
            if c is None:
                break
            if c.isalnum() or c == '_':
                result.append(self.get_char())
            else:
                break

        return ''.join(result)

    def get_name_list(self) -> List[str]:
        """Parse a space-separated list of names."""
        names = []
        while True:
            self.skip_whitespace()
            if self.peek() is None:
                break
            name = self.get_identifier()
            if not name:
                break
            names.append(name)
            self.skip_whitespace()

        return names

    def parse_expr(self) -> Expr:
        """Parse an expression."""
        self.skip_whitespace()

        # Check for identifier
        if self.peek() and (self.peek().isalpha() or self.peek() == '_'):
            ident = self.get_identifier()
            # Check if it's a function call
            self.skip_whitespace()
            if self.peek() == '(':
                self.get_char()  # consume '('
                args = self.parse_expr_list()
                self.skip_whitespace()
                if self.peek() != ')':
                    raise self.error("Expected ')'")
                self.get_char()  # consume ')'

                op = ident.upper()
                if op not in VALID_OPERATORS:
                    raise self.error(f"Unknown operator '{ident}'")
                return ExprCall(op, args)
            else:
                return ExprIdentifier(ident)

        # Check for literal
        if self.peek() and self.peek().isdigit():
            digit = self.get_char()
            if digit == '0':
                return ExprLiteral(False)
            elif digit == '1':
                return ExprLiteral(True)
            elif digit == 'X':
                raise self.error("Literal 'X' is not allowed")

        raise self.error("Expected expression")

    def parse_expr_list(self) -> List[Expr]:
        """Parse a comma-separated list of expressions."""
        exprs = []
        self.skip_whitespace()

        if self.peek() == ')':
            return exprs

        while True:
            exprs.append(self.parse_expr())
            self.skip_whitespace()
            if self.peek() == ',':
                self.get_char()
                self.skip_whitespace()
                continue
            break

        return exprs

    def parse_line(self):
        """Parse a single line."""
        self.skip_whitespace()
        if self.peek() is None:
            return None

        # Check for comment or empty line
        if self.peek() == '#':
            # Skip to end of line
            while self.pos < self.len:
                c = self.get_char()
                if c == '\n' or c is None:
                    break
            return None

        start_pos = self.pos
        start_line = self.line_num
        start_col = self.col_num

        # Check for declaration or assignment
        if self.peek() and (self.peek().isalpha() or self.peek() == '_'):
            ident = self.get_identifier()
            self.skip_whitespace()

            # Declaration line
            if ident.lower() in ('input', 'output', 'wire'):
                names = self.get_name_list()
                if ident.lower() == 'wire' and len(names) == 0:
                    pass  # Empty wire declaration is allowed
                return (ident.lower(), names, start_line, start_col)

            # Assignment line
            if self.peek() == '=':
                self.get_char()  # consume '='
                expr = self.parse_expr()
                self.skip_whitespace()
                if self.peek() is not None:
                    raise self.error(f"Unexpected character after expression")

                lhs = ident
                return ('assignment', lhs, expr, start_line, start_col)

            raise self.error(f"Unexpected '{ident}'")

        raise self.error(f"Unexpected character '{self.peek()}'")

    def error(self, message: str) -> CircParseError:
        """Create a parse error at current position."""
        return CircParseError(
            message,
            self.filename,
            self.line_num + 1,  # 1-based
            self.col_num + 1 if self.col_num > 0 else 1
        )

    def parse(self):
        """Parse the entire file."""
        # First pass: collect declarations and assignments
        while self.pos < self.len:
            result = self.parse_line()
            if result is None:
                # Skip to next line
                while self.pos < self.len:
                    c = self.get_char()
                    if c == '\n' or c is None:
                        break
                continue

            line_type = result[0]

            if line_type in ('input', 'output', 'wire'):
                decl_type, names, line, col = result

                # Check if we've already seen assignments
                if self.in_declarations and len(self.assignments) > 0:
                    raise DeclarationAfterAssignmentError(
                        f"Declaration after assignment at line {line}"
                    )

                for name in names:
                    # Check for duplicates
                    if name in self.all_names:
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'"
                        )

                    self.all_names.add(name)

                    if decl_type == 'input':
                        self.inputs.add(name)
                    elif decl_type == 'output':
                        self.outputs.add(name)
                    else:  # wire
                        self.wires.add(name)

            elif line_type == 'assignment':
                _, lhs, expr, line, col = result
                self.in_declarations = False
                self.assignments.append(Assignment(lhs, expr, line, col))

            # Move to next line
            while self.pos < self.len:
                c = self.get_char()
                if c == '\n' or c is None:
                    break

        return {
            'inputs': self.inputs,
            'outputs': self.outputs,
            'wires': self.wires,
            'assignments': self.assignments
        }


# =============================================================================
# Validator
# =============================================================================

class Validator:
    def __init__(self, parse_result: dict):
        self.inputs = parse_result['inputs']
        self.outputs = parse_result['outputs']
        self.wires = parse_result['wires']
        self.assignments = parse_result['assignments']

        self.all_signals = self.inputs | self.outputs | self.wires
        self.assigned_from: Dict[str, List[str]] = defaultdict(list)  # signal -> list of signals it depends on
        self.signal_users: Dict[str, List[str]] = defaultdict(list)   # signal -> list of signals that use it

    def validate(self):
        """Run all validation checks."""
        # Check that all LHS are valid
        for assign in self.assignments:
            self._check_lhs(assign)

        # Check that all RHS identifiers are defined
        for assign in self.assignments:
            self._check_rhs_defined(assign.expr)

        # Check operator arities
        for assign in self.assignments:
            self._check_arity(assign.expr)

        # Check all signals are assigned exactly once
        self._check_assignment_completeness()

        # Check for cycles
        self._check_cycles()

    def _check_lhs(self, assign: Assignment):
        """Check LHS is valid."""
        lhs = assign.lhs

        if lhs in self.inputs:
            raise InputAssignmentError(f"Cannot assign to input '{lhs}'")

        if lhs not in self.outputs and lhs not in self.wires:
            raise UndefinedNameError(f"Undefined signal '{lhs}'")

    def _check_rhs_defined(self, expr: Expr):
        """Check all identifiers in expression are defined."""
        if isinstance(expr, ExprIdentifier):
            if expr.name not in self.all_signals:
                raise UndefinedNameError(f"Undefined signal '{expr.name}'")
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                self._check_rhs_defined(arg)

    def _check_arity(self, expr: Expr):
        """Check operator arity."""
        if isinstance(expr, ExprCall):
            op = expr.op
            min_arity = OPERATOR_ARITIES[op]
            actual_arity = len(expr.args)

            if actual_arity < min_arity:
                raise ArityError(
                    f"Operator '{op}' requires at least {min_arity} arguments, got {actual_arity}"
                )

            for arg in expr.args:
                self._check_arity(arg)

    def _check_assignment_completeness(self):
        """Check all wires and outputs are assigned exactly once."""
        assigned = set()

        for assign in self.assignments:
            lhs = assign.lhs
            if lhs in assigned:
                raise MultipleAssignmentError(
                    f"Multiple assignments to '{lhs}'"
                )
            assigned.add(lhs)

        # Check for unassigned signals
        for wire in self.wires:
            if wire not in assigned:
                raise UnassignedSignalError(f"Unassigned wire '{wire}'")

        for output in self.outputs:
            if output not in assigned:
                raise UnassignedSignalError(f"Unassigned output '{output}'")

    def _check_cycles(self):
        """Check for cycles in the assignment graph."""
        # Build dependency graph
        for assign in self.assignments:
            lhs = assign.lhs
            deps = self._collect_deps(assign.expr)
            self.assigned_from[lhs] = deps
            for dep in deps:
                self.signal_users[dep].append(lhs)

        # Check for cycles using DFS
        visited = set()
        rec_stack = set()
        path = []

        def dfs(node: str) -> Optional[List[str]]:
            visited.add(node)
            rec_stack.add(node)
            path.append(node)

            for neighbor in self.assigned_from.get(node, []):
                if neighbor not in visited:
                    cycle = dfs(neighbor)
                    if cycle:
                        return cycle
                elif neighbor in rec_stack:
                    # Found a cycle
                    cycle_start = path.index(neighbor)
                    return path[cycle_start:] + [neighbor]

            path.pop()
            rec_stack.remove(node)
            return None

        # Start from each unvisited signal
        for signal in self.all_signals:
            if signal not in visited:
                cycle = dfs(signal)
                if cycle:
                    cycle_path = ' -> '.join(cycle)
                    raise CycleError(
                        f"Cycle detected: {cycle_path}",
                        cycle
                    )

    def _collect_deps(self, expr: Expr) -> List[str]:
        """Collect all signal dependencies from an expression."""
        deps = []
        if isinstance(expr, ExprIdentifier):
            deps.append(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                deps.extend(self._collect_deps(arg))
        return deps


# =============================================================================
# Circuit Interface
# =============================================================================

@dataclass
class Interface:
    inputs: List[Dict[str, Any]]
    outputs: List[Dict[str, Any]]


def get_circuit_interface(inputs: Set[str], outputs: Set[str]) -> Interface:
    """Get the circuit interface (inputs and outputs sorted lexicographically)."""
    input_list = sorted([{'name': name, 'msb': 0, 'lsb': 0} for name in inputs],
                        key=lambda x: x['name'])
    output_list = sorted([{'name': name, 'msb': 0, 'lsb': 0} for name in outputs],
                         key=lambda x: x['name'])
    return Interface(inputs=input_list, outputs=output_list)


# =============================================================================
# CLI
# =============================================================================

def print_json_ok(command: str, **kwargs):
    """Print JSON success response."""
    result = {'ok': True, 'command': command}
    result.update(kwargs)
    print(json.dumps(result))


def print_json_error(command: str, exit_code: int, error_type: str,
                     message: str, file: Optional[str] = None,
                     line: Optional[int] = None, col: Optional[int] = None):
    """Print JSON error response."""
    error_obj = {'type': error_type, 'message': message}
    if file is not None:
        error_obj['file'] = file
    if line is not None:
        error_obj['line'] = line
    if col is not None:
        error_obj['col'] = col

    result = {
        'ok': False,
        'command': command,
        'exit_code': exit_code,
        'error': error_obj
    }
    print(json.dumps(result))


def get_exit_code(error: Exception) -> int:
    """Get the exit code for an error."""
    if isinstance(error, (CliUsageError, FileNotFoundError)):
        return 1
    elif isinstance(error, CircParseError):
        return 2
    elif isinstance(error, (DeclarationAfterAssignmentError, DuplicateNameError,
                            UndefinedNameError, UnassignedSignalError,
                            InputAssignmentError, MultipleAssignmentError,
                            ArityError, CycleError)):
        return 3
    elif isinstance(error, CircoptError):
        return 4
    else:
        return 4


def get_error_type(error: Exception) -> str:
    """Get the error type name."""
    return type(error).__name__


def check_file(filename: str, use_json: bool) -> int:
    """Check a .circ file. Returns exit code."""
    try:
        # Read file
        try:
            with open(filename, 'r') as f:
                text = f.read()
        except FileNotFoundError:
            if use_json:
                print_json_error(
                    'check',
                    1, 'FileNotFoundError',
                    f"File not found: {filename}"
                )
            else:
                print(f"Error: File not found: {filename}")
            return 1

        # Parse
        parser = Parser(text, filename)
        parse_result = parser.parse()

        # Validate
        validator = Validator(parse_result)
        validator.validate()

        # Success
        if use_json:
            interface = get_circuit_interface(parse_result['inputs'], parse_result['outputs'])
            print_json_ok('check', format='circ',
                         inputs=interface.inputs,
                         outputs=interface.outputs)
        else:
            interface = get_circuit_interface(parse_result['inputs'], parse_result['outputs'])
            print(f"inputs: {[i['name'] for i in interface.inputs]}")
            print(f"outputs: {[o['name'] for o in interface.outputs]}")

        return 0

    except CircParseError as e:
        if use_json:
            print_json_error('check', 2, 'CircParseError', str(e),
                           file=e.file, line=e.line, col=e.col)
        else:
            print(f"Parse error at {e.file}:{e.line}:{e.col}: {e}")
        return 2

    except (DeclarationAfterAssignmentError, DuplicateNameError,
            UndefinedNameError, UnassignedSignalError, InputAssignmentError,
            MultipleAssignmentError, ArityError, CycleError) as e:

        error_type = get_error_type(e)

        if isinstance(e, CycleError):
            message = str(e)
        else:
            message = str(e)

        if use_json:
            print_json_error('check', 3, error_type, message)
        else:
            print(f"Validation error: {message}")
        return 3

    except Exception as e:
        if use_json:
            print_json_error('check', 4, get_error_type(e), str(e))
        else:
            print(f"Internal error: {e}")
        return 4


def print_help():
    """Print help text."""
    help_text = """Usage: circopt.py [OPTIONS] COMMAND [ARGS]...

Options:
  --help          Show this help message and exit
  --version       Show version information and exit
  --json          Output JSON instead of plain text

Commands:
  check           Check a .circ file for validity
"""
    print(help_text)


def print_version(json_mode: bool):
    """Print version."""
    if json_mode:
        print_json_ok('__version__', version=VERSION)
    else:
        print(VERSION)


def main(argv: List[str]) -> int:
    """Main entry point."""
    if not argv:
        print_json_error('__cli__', 1, 'CliUsageError',
                        "No command provided")
        return 1

    # Parse global flags
    args = argv[:]
    json_mode = False
    command = None
    command_args = []

    # Extract --json flag if present (before command)
    i = 0
    while i < len(args):
        if args[i] == '--json':
            json_mode = True
            args.pop(i)
        else:
            i += 1

    # Check for help/version before command
    if '--help' in args:
        print_help()
        return 0

    if '--version' in args:
        print_version(json_mode)
        return 0

    # Get command
    if not args:
        if json_mode:
            print_json_error('__cli__', 1, 'CliUsageError',
                           "No command provided")
        else:
            print("Error: No command provided")
        return 1

    command = args[0]
    command_args = args[1:]

    # Handle commands
    if command == 'check':
        if len(command_args) < 1:
            if json_mode:
                print_json_error('check', 1, 'CliUsageError',
                               "Missing required argument: <file.circ>")
            else:
                print("Error: Missing required argument: <file.circ>")
            return 1

        filename = command_args[0]
        return check_file(filename, json_mode)

    else:
        if json_mode:
            print_json_error('__cli__', 1, 'CliUsageError',
                           f"Unknown command: {command}")
        else:
            print(f"Error: Unknown command: {command}")
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))