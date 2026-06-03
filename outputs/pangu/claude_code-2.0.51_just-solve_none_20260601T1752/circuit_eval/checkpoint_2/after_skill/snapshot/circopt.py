#!/usr/bin/env python3
"""Circuit optimization and analysis tool."""

import sys
import json
from dataclasses import dataclass
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


class MissingInputError(CircoptError):
    """Missing required input - exit code 1."""
    pass


class UnknownInputError(CircoptError):
    """Unknown input provided - exit code 1."""
    pass


class InputValueParseError(CircoptError):
    """Invalid input value - must be 0 or 1 - exit code 2."""
    pass


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
            raise self.error("Expected identifier")

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
            name = self.get_identifier().upper()  # Normalize for keywords
            if not name:
                break
            names.append(name)
            self.skip_whitespace()

        return names

    def parse_expr(self) -> Expr:
        """Parse an expression."""
        self.skip_whitespace()

        # Check for identifier
        peek_char = self.peek()
        if peek_char and (peek_char.isalpha() or peek_char == '_'):
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
        if peek_char and peek_char.isdigit():
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

    def parse_line(self) -> Optional[Tuple[str, List[str]]]:
        """Parse a single line. Returns (type, data) or None."""
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

        # Check for declaration
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
                    # Normalize: preserve case for identifiers, but check for duplicates
                    name_lower = name.lower()

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
# Evaluator (2-valued Boolean)
# =============================================================================

class Evaluator:
    """Evaluate a circuit with 2-valued Boolean logic."""

    def __init__(self, parse_result: dict, default: int = 0):
        """
        Initialize the evaluator.

        Args:
            parse_result: Result from parser containing inputs, outputs, wires, assignments
            default: Default value for unspecified inputs (0 or 1)
        """
        self.inputs = parse_result['inputs']
        self.outputs = parse_result['outputs']
        self.wires = parse_result['wires']
        self.assignments = parse_result['assignments']
        self.all_signals = self.inputs | self.outputs | self.wires
        self.default = bool(default)

        # Build evaluation graph
        self._build_graph()

        # Boolean operators
        self._ops = {
            'NOT': lambda x: not x,
            'BUF': lambda x: x,
            'AND': lambda x, y: x and y,
            'OR': lambda x, y: x or y,
            'XOR': lambda x, y: x != y,
            'NAND': lambda x, y: not (x and y),
            'NOR': lambda x, y: not (x or y),
            'XNOR': lambda x, y: x == y,
        }

    def _build_graph(self):
        """Build dependency graph and topological order."""
        self.deps: Dict[str, List[str]] = defaultdict(list)  # signal -> dependencies
        self.rev_deps: Dict[str, List[str]] = defaultdict(list)  # signal -> dependents
        self.expr_map: Dict[str, Expr] = {}  # signal -> expression

        for assign in self.assignments:
            lhs = assign.lhs
            self.expr_map[lhs] = assign.expr
            signal_deps = self._collect_deps(assign.expr)
            self.deps[lhs] = signal_deps
            for dep in signal_deps:
                self.rev_deps[dep].append(lhs)

        # Topological sort using Kahn's algorithm
        self._compute_topological_order()

    def _collect_deps(self, expr: Expr) -> List[str]:
        """Collect all signal dependencies from an expression."""
        deps = []
        if isinstance(expr, ExprIdentifier):
            deps.append(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                deps.extend(self._collect_deps(arg))
        return deps

    def _compute_topological_order(self):
        """Compute topological order of evaluation."""
        # Compute in-degree (number of dependencies)
        in_degree: Dict[str, int] = {}
        for signal in self.all_signals:
            if signal not in in_degree:
                in_degree[signal] = 0

        for signal, deps in self.deps.items():
            in_degree[signal] = len(deps)

        # Kahn's algorithm
        queue = [s for s in self.all_signals if in_degree[s] == 0]
        queue.sort()  # Sort for determinism
        self.eval_order = []

        while queue:
            node = queue.pop(0)
            self.eval_order.append(node)

            # For each dependents, reduce in-degree
            for dependent in self.rev_deps.get(node, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
                    queue.sort()  # Keep sorted for determinism

        # The evaluation order is all signals that have expressions (assigned signals)
        self.assigned_signals = list(self.expr_map.keys())

    def evaluate(self, input_values: Dict[str, str], allow_extra: bool = False) -> Dict[str, int]:
        """
        Evaluate the circuit with given input values.

        Args:
            input_values: Dictionary mapping input names to '0' or '1' strings
            allow_extra: If True, ignore extra inputs instead of erroring

        Returns:
            Dictionary mapping output names to 0 or 1

        Raises:
            MissingInputError: A required input is not provided
            UnknownInputError: An unknown input is provided
            InputValueParseError: An input value is not '0' or '1'
        """
        # Validate and parse input values
        parsed_values: Dict[str, bool] = {}

        # Check for unknown inputs
        for name in input_values:
            if name not in self.inputs and not allow_extra:
                raise UnknownInputError(f"Unknown input '{name}'")
            # Validate value
            val = input_values[name]
            if val not in ('0', '1'):
                raise InputValueParseError(f"Invalid value '{val}' for input '{name}', must be 0 or 1")
            parsed_values[name] = val == '1'

        # Set default values for unspecified inputs
        for inp in self.inputs:
            if inp not in parsed_values:
                parsed_values[inp] = self.default

        # Check for missing inputs
        for inp in self.inputs:
            if inp not in parsed_values:
                raise MissingInputError(f"Missing input '{inp}'")

        # Evaluate in topological order - only assigned signals
        for signal in self.eval_order:
            if signal in self.expr_map:
                expr = self.expr_map[signal]
                parsed_values[signal] = self._eval_expr(expr, parsed_values)

        # Collect output values
        results = {}
        for output in sorted(self.outputs):
            if output in parsed_values:
                results[output] = 1 if parsed_values[output] else 0
            else:
                raise MissingInputError(f"Output '{output}' was not computed")

        return results

    def _eval_expr(self, expr: Expr, values: Dict[str, bool]) -> bool:
        """Evaluate an expression with given signal values."""
        if isinstance(expr, ExprIdentifier):
            return values[expr.name]
        elif isinstance(expr, ExprLiteral):
            return expr.value
        elif isinstance(expr, ExprCall):
            op = expr.op
            op_func = self._ops[op]
            arg_values = [self._eval_expr(arg, values) for arg in expr.args]
            return op_func(*arg_values)
        else:
            raise ValueError(f"Unknown expression type: {type(expr)}")


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
    if isinstance(error, (CliUsageError, FileNotFoundError,
                          MissingInputError, UnknownInputError)):
        return 1
    elif isinstance(error, (CircParseError, InputValueParseError)):
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


def parse_set_args(args: List[str]) -> Dict[str, str]:
    """Parse --set name=value arguments."""
    result = {}
    i = 0
    while i < len(args):
        if args[i] == '--set':
            i += 1
            if i >= len(args):
                raise CliUsageError("Missing value for --set")
            arg = args[i]
            if '=' not in arg:
                raise CliUsageError(f"Invalid --set format: {arg}, expected name=value")
            name, value = arg.split('=', 1)
            if not name:
                raise CliUsageError("Empty name in --set argument")
            result[name] = value
        else:
            break
        i += 1
    return result, args[i:]


def print_help():
    """Print help text."""
    help_text = """Usage: circopt.py [OPTIONS] COMMAND [ARGS]...

Options:
  --help          Show this help message and exit
  --version       Show version information and exit
  --json          Output JSON instead of plain text

Commands:
  check           Check a .circ file for validity
  eval            Evaluate a circuit with given inputs
"""
    print(help_text)


def print_version(json_mode: bool):
    """Print version."""
    if json_mode:
        print_json_ok('__version__', version=VERSION)
    else:
        print(VERSION)


def eval_circuit(filename: str, argv: List[str], use_json: bool) -> int:
    """
    Evaluate a circuit with given inputs.

    Args:
        filename: Path to .circ file
        argv: Command arguments (including --set, --default, --allow-extra, --json)
        use_json: Whether to output JSON

    Returns:
        Exit code
    """
    # Parse arguments
    input_values = {}
    default = 0
    allow_extra = False

    i = 0
    remaining_args = []

    while i < len(argv):
        arg = argv[i]

        if arg == '--set':
            i += 1
            if i >= len(argv):
                if use_json:
                    print_json_error('eval', 1, 'CliUsageError',
                                   "Missing value for --set")
                else:
                    print("Error: Missing value for --set")
                return 1
            set_arg = argv[i]
            if '=' not in set_arg:
                if use_json:
                    print_json_error('eval', 1, 'CliUsageError',
                                   f"Invalid --set format: {set_arg}, expected name=value")
                else:
                    print(f"Error: Invalid --set format: {set_arg}, expected name=value")
                return 1
            name, value = set_arg.split('=', 1)
            if not name:
                if use_json:
                    print_json_error('eval', 1, 'CliUsageError',
                                   "Empty name in --set argument")
                else:
                    print("Error: Empty name in --set argument")
                return 1
            input_values[name] = value

        elif arg == '--default':
            i += 1
            if i >= len(argv):
                if use_json:
                    print_json_error('eval', 1, 'CliUsageError',
                                   "Missing value for --default")
                else:
                    print("Error: Missing value for --default")
                return 1
            val = argv[i]
            if val not in ('0', '1'):
                if use_json:
                    print_json_error('eval', 1, 'CliUsageError',
                                   "--default must be 0 or 1")
                else:
                    print("Error: --default must be 0 or 1")
                return 1
            default = int(val)

        elif arg == '--allow-extra':
            allow_extra = True

        elif arg == '--json':
            # Should already be handled at top level, but include here for safety
            pass

        else:
            if arg:  # Skip empty strings
                remaining_args.append(arg)

        i += 1

    if remaining_args:
        # Check if there are any positional arguments we don't recognize
        if use_json:
            print_json_error('eval', 1, 'CliUsageError',
                           f"Unexpected argument: {remaining_args[0]}")
        else:
            print(f"Error: Unexpected argument: {remaining_args[0]}")
        return 1

    try:
        # Read and parse circuit file
        try:
            with open(filename, 'r') as f:
                text = f.read()
        except FileNotFoundError:
            if use_json:
                print_json_error('eval', 1, 'FileNotFoundError',
                               f"File not found: {filename}")
            else:
                print(f"Error: File not found: {filename}")
            return 1

        # Parse
        parser = Parser(text, filename)
        parse_result = parser.parse()

        # Validate
        validator = Validator(parse_result)
        validator.validate()

        # Evaluate
        evaluator = Evaluator(parse_result, default)
        results = evaluator.evaluate(input_values, allow_extra)

        # Output results
        if use_json:
            outputs = []
            for name in sorted(results.keys()):
                outputs.append({
                    'name': name,
                    'msb': 0,
                    'lsb': 0,
                    'value': str(results[name])
                })
            print_json_ok('eval', mode='2val', radix='bin', outputs=outputs)
        else:
            for name in sorted(results.keys()):
                print(f"{name}={results[name]}")

        return 0

    except (MissingInputError, UnknownInputError) as e:
        if use_json:
            print_json_error('eval', 1, get_error_type(e), str(e))
        else:
            print(f"Error: {e}")
        return 1

    except (CircParseError, InputValueParseError) as e:
        if use_json:
            print_json_error('eval', 2, get_error_type(e), str(e))
        else:
            if isinstance(e, CircParseError):
                print(f"Parse error at {e.file}:{e.line}:{e.col}: {e}")
            else:
                print(f"Error: {e}")
        return 2

    except (DeclarationAfterAssignmentError, DuplicateNameError,
            UndefinedNameError, UnassignedSignalError, InputAssignmentError,
            MultipleAssignmentError, ArityError, CycleError) as e:

        error_type = get_error_type(e)

        if use_json:
            print_json_error('eval', 3, error_type, str(e))
        else:
            print(f"Validation error: {e}")
        return 3

    except Exception as e:
        if use_json:
            print_json_error('eval', 4, get_error_type(e), str(e))
        else:
            print(f"Internal error: {e}")
        return 4


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

    elif command == 'eval':
        if len(command_args) < 1:
            if json_mode:
                print_json_error('eval', 1, 'CliUsageError',
                               "Missing required argument: <file.circ>")
            else:
                print("Error: Missing required argument: <file.circ>")
            return 1

        filename = command_args[0]
        args = command_args[1:]
        return eval_circuit(filename, args, json_mode)

    else:
        if json_mode:
            print_json_error('__cli__', 1, 'CliUsageError',
                           f"Unknown command: {command}")
        else:
            print(f"Error: Unknown command: {command}")
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))