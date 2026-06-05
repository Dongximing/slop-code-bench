#!/usr/bin/env python3
"""
Circuit Optimizer - CLI tool for validating and optimizing .circ circuit files.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


__version__ = "0.1.0"


class ExitCode(Enum):
    SUCCESS = 0
    CLI_USAGE_ERROR = 1
    PARSE_ERROR = 2
    VALIDATION_ERROR = 3
    INTERNAL_ERROR = 4
    NON_EQUIVALENCE = 10


@dataclass
class SignalInfo:
    """Information about a signal (input, output, wire)."""
    name: str
    msb: int = 0
    lsb: int = 0


@dataclass
class Circuit:
    """Represents a parsed circuit."""
    inputs: list[SignalInfo] = field(default_factory=list)
    outputs: list[SignalInfo] = field(default_factory=list)
    assignments: dict[str, 'Assignment'] = field(default_factory=dict)

    # Track order of declarations
    input_names: list[str] = field(default_factory=list)
    output_names: list[str] = field(default_factory=list)
    wire_names: list[str] = field(default_factory=list)

    # Track which assignments happened
    assigned_signals: set[str] = field(default_factory=set)


@dataclass
class Assignment:
    """Represents an assignment statement."""
    lhs: str
    operator: str
    operands: list[str]
    line: int
    col: int


class ErrorType(Enum):
    CLI_USAGE_ERROR = "CliUsageError"
    FILE_NOT_FOUND_ERROR = "FileNotFoundError"
    CIRC_PARSE_ERROR = "CircParseError"
    DECLARATION_AFTER_ASSIGNMENT_ERROR = "DeclarationAfterAssignmentError"
    DUPLICATE_NAME_ERROR = "DuplicateNameError"
    UNDEFINED_NAME_ERROR = "UndefinedNameError"
    UNASSIGNED_SIGNAL_ERROR = "UnassignedSignalError"
    INPUT_ASSIGNMENT_ERROR = "InputAssignmentError"
    MULTIPLE_ASSIGNMENT_ERROR = "MultipleAssignmentError"
    ARITY_ERROR = "ArityError"
    CYCLE_ERROR = "CycleError"


class CircuitError(Exception):
    """Base class for circuit-related errors."""

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col
        self.error_type: Optional[ErrorType] = None

    def to_json_dict(self, command: str, exit_code: ExitCode) -> dict:
        return {
            "ok": False,
            "command": command,
            "exit_code": exit_code.value,
            "error": {
                "type": self.error_type.value if self.error_type else "UnknownError",
                "message": self.message,
                "file": self.file,
                "line": self.line,
                "col": self.col
            }
        }


class CliUsageError(CircuitError):
    def __init__(self, message: str):
        super().__init__(message)
        self.error_type = ErrorType.CLI_USAGE_ERROR


class FileNotFoundError(CircuitError):
    def __init__(self, file: str):
        super().__init__(f"File not found: {file}", file=file)
        self.error_type = ErrorType.FILE_NOT_FOUND_ERROR


class CircParseError(CircuitError):
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.error_type = ErrorType.CIRC_PARSE_ERROR


class DeclarationAfterAssignmentError(CircuitError):
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.error_type = ErrorType.DECLARATION_AFTER_ASSIGNMENT_ERROR


class DuplicateNameError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Duplicate name: {name}", file, line, col)
        self.error_type = ErrorType.DUPLICATE_NAME_ERROR


class UndefinedNameError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Undefined name: {name}", file, line, col)
        self.error_type = ErrorType.UNDEFINED_NAME_ERROR


class UnassignedSignalError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Unassigned signal: {name}", file, line, col)
        self.error_type = ErrorType.UNASSIGNED_SIGNAL_ERROR


class InputAssignmentError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Cannot assign to input: {name}", file, line, col)
        self.error_type = ErrorType.INPUT_ASSIGNMENT_ERROR


class MultipleAssignmentError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Multiple assignment to: {name}", file, line, col)
        self.error_type = ErrorType.MULTIPLE_ASSIGNMENT_ERROR


class ArityError(CircuitError):
    def __init__(self, operator: str, arity: int, expected: str,
                 file: Optional[str] = None, line: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(
            f"{operator} expects {expected} operands, got {arity}",
            file, line, col
        )
        self.error_type = ErrorType.ARITY_ERROR


class CycleError(CircuitError):
    def __init__(self, cycle_path: list[str], file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        path_str = " -> ".join(cycle_path)
        super().__init__(f"Cycle detected: {path_str}", file, line, col)
        self.error_type = ErrorType.CYCLE_ERROR
        self.cycle_path = cycle_path


class MissingInputError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Missing input value: {name}", file, line, col)
        self.error_type = ErrorType.CLI_USAGE_ERROR  # Exit code 1


class UnknownInputError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Unknown input: {name}", file, line, col)
        self.error_type = ErrorType.CLI_USAGE_ERROR  # Exit code 1


class InputValueParseError(CircuitError):
    def __init__(self, name: str, value: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Invalid input value for {name}: {value}", file, line, col)
        self.error_type = ErrorType.CIRC_PARSE_ERROR  # Exit code 2


# Operator arity requirements: (min, max)
OPERATOR_ARITY = {
    "NOT": (1, 1),
    "BUF": (1, 1),
    "AND": (2, None),  # >= 2
    "OR": (2, None),   # >= 2
    "XOR": (2, None),  # >= 2
    "NAND": (2, None), # >= 2
    "NOR": (2, None),  # >= 2
    "XNOR": (2, None), # >= 2
}


def is_valid_identifier(name: str) -> bool:
    """Check if a string is a valid identifier."""
    if not name:
        return False
    if name[0].isdigit():
        return False
    return all(c.isalnum() or c == '_' for c in name)


class Tokenizer:
    """Tokenizer for .circ files."""

    def __init__(self, filename: str, content: str):
        self.filename = filename
        self.content = content
        self.pos = 0
        self.line = 1
        self.col = 1

    def peek(self):
        """Peek at the next token without consuming it."""
        saved_pos = self.pos
        saved_line = self.line
        saved_col = self.col
        token = self.next_token()
        self.pos = saved_pos
        self.line = saved_line
        self.col = saved_col
        return token

    def next_token(self):
        """Get the next token, or None if at end."""
        # Skip whitespace
        while self.pos < len(self.content):
            ch = self.content[self.pos]
            if ch.isspace():
                if ch == '\n':
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.pos += 1
            elif ch == '#':
                # Comment - skip to end of line
                while self.pos < len(self.content) and self.content[self.pos] != '\n':
                    self.pos += 1
            else:
                break

        if self.pos >= len(self.content):
            return None

        # Parse identifier, number, or operators
        ch = self.content[self.pos]
        if ch.isalpha() or ch == '_':
            # Identifier
            start = self.pos
            while (self.pos < len(self.content) and
                   (self.content[self.pos].isalnum() or self.content[self.pos] == '_')):
                self.pos += 1
            self.col += self.pos - start
            return self.content[start:self.pos].upper()
        elif ch.isdigit():
            # Number (literal)
            start = self.pos
            while (self.pos < len(self.content) and
                   self.content[self.pos].isdigit()):
                self.pos += 1
            self.col += self.pos - start
            return self.content[start:self.pos]
        elif ch == '=':
            self.pos += 1
            self.col += 1
            return '='
        elif ch == ',':
            self.pos += 1
            self.col += 1
            return ','
        elif ch == '(':
            self.pos += 1
            self.col += 1
            return '('
        elif ch == ')':
            self.pos += 1
            self.col += 1
            return ')'
        elif ch == 'X' or ch == 'x':
            # Check if it's a standalone X literal
            start = self.pos
            self.pos += 1
            self.col += 1
            # If next char is alphanumeric or _, it's part of identifier
            if (self.pos < len(self.content) and
                (self.content[self.pos].isalnum() or self.content[self.pos] == '_')):
                # This continues an identifier, let parser handle it
                return 'X'
            return 'X'

        # Unexpected character
        raise CircParseError(
            f"Unexpected character: '{ch}'",
            self.filename,
            self.line,
            self.col
        )


class Parser:
    """Parser for .circ files."""

    def __init__(self, filename: str, content: str):
        self.filename = filename
        self.tokenizer = Tokenizer(filename, content)
        self.circuit = Circuit()
        self.assignment_started = False

    def parse(self) -> Circuit:
        """Parse the entire file."""
        while True:
            token = self.tokenizer.peek()
            if token is None:
                break

            # Get current position before consuming
            line = self.tokenizer.line
            col = self.tokenizer.col

            # Check for declaration
            if token and token.upper() == 'INPUT':
                self._parse_declaration("input", 'input_names', 'inputs', line, col)
            elif token and token.upper() == 'OUTPUT':
                self._parse_declaration("output", 'output_names', 'outputs', line, col)
            elif token and token.upper() == 'WIRE':
                self._parse_declaration("wire", 'wire_names', line, col)
            else:
                # Assignment or identifier expression
                if self.assignment_started:
                    raise CircParseError(
                        f"Expected assignment or end of file, found '{current_token}'",
                        self.filename, line, col
                    )
                self._parse_assignment(line, col)

        self._check_all_signals_assigned()
        self._check_cycles()
        return self.circuit

    def _parse_declaration(self, decl_type: str, names_field: str,
                            line: int, col: int):
        """Parse a declaration line."""
        if self.assignment_started:
            raise DeclarationAfterAssignmentError(
                f"Declaration after first assignment",
                self.filename, line, col
            )

        # Consume 'input', 'output', or 'wire'
        keyword = self.tokenizer.next_token()

        # Parse names
        while True:
            token = self.tokenizer.peek()
            if token is None:
                raise CircParseError(
                    f"Expected names after {decl_type}",
                    self.filename, line, col
                )

            if token == 'X':
                # Consume X and check if it's standalone
                self.tokenizer.next_token()
                # If next is not alphabetic, it's a literal X
                next_t = self.tokenizer.peek()
                if next_t is None or not (next_t.isalpha() or next_t == '_'):
                    raise CircParseError(
                        "Literal 'X' value is not allowed in .circ files",
                        self.filename, line, col
                    )
                # It's part of an identifier, put it back conceptually
                # The actual name would be 'X' + something
                name = 'X'
            else:
                name = self.tokenizer.next_token()

            # Validate identifier
            if not is_valid_identifier(name):
                raise CircParseError(
                    f"Invalid identifier: {name}",
                    self.filename, line, col
                )

            # Add to appropriate tracking
            names_list = getattr(self.circuit, names_field)
            if name in names_list:
                raise DuplicateNameError(name, self.filename, line, col)

            names_list.append(name)

            # Create signal info (wire declarations don't create SignalInfo)
            if decl_type != 'wire':
                signal_info = SignalInfo(name=name, msb=0, lsb=0)
                if names_field == 'input_names':
                    self.circuit.inputs.append(signal_info)
                else:
                    self.circuit.outputs.append(signal_info)

            # Check for more names
            next_token = self.tokenizer.peek()
            if (next_token is None or
                next_token.upper() in ('INPUT', 'OUTPUT', 'WIRE', '=', '(', ')', ',')):
                break

    def _parse_assignment(self, line: int, col: int):
        """Parse an assignment statement."""
        self.assignment_started = True

        # Get LHS
        lhs = self.tokenizer.next_token()
        if lhs is None:
            raise CircParseError(
                "Expected LHS for assignment",
                self.filename, line, col
            )

        # Validate LHS
        if lhs in self.circuit.input_names:
            raise InputAssignmentError(lhs, self.filename, line, col)

        is_defined = lhs in self.circuit.wire_names or lhs in self.circuit.output_names
        if not is_defined:
            raise UndefinedNameError(lhs, self.filename, line, col)

        # Check for multiple assignment
        if lhs in self.circuit.assigned_signals:
            raise MultipleAssignmentError(lhs, self.filename, line, col)

        self.circuit.assigned_signals.add(lhs)

        # Check for '='
        eq_token = self.tokenizer.next_token()
        if eq_token != '=':
            raise CircParseError(
                f"Expected '=', found '{eq_token}'",
                self.filename, line, col
            )

        # Parse expression
        operator = self.tokenizer.next_token()
        if operator is None:
            raise CircParseError(
                "Expected operator after '='",
                self.filename, line, col
            )

        operator_upper = operator.upper()
        if operator_upper not in OPERATOR_ARITY:
            raise CircParseError(
                f"Unknown operator: {operator}",
                self.filename, line, col
            )

        # Check for '('
        open_paren = self.tokenizer.next_token()
        if open_paren != '(':
            raise CircParseError(
                f"Expected '(', found '{open_paren}'",
                self.filename, line, col
            )

        # Parse operands
        operands = []
        while True:
            token = self.tokenizer.next_token()
            if token is None:
                raise CircParseError(
                    "Unexpected end of expression",
                    self.filename, line, col
                )

            if token == 'X':
                # Check if it's a standalone X literal
                next_t = self.tokenizer.peek()
                if next_t is None or not (next_t.isalpha() or next_t == '_'):
                    raise CircParseError(
                        "Literal 'X' value is not allowed in .circ files",
                        self.filename, line, col
                    )
                # It continues an identifier
                # Actually, a standalone X cannot be part of expression
                # This would be caught earlier in tokenization

            if not is_valid_identifier(token) and token not in ('0', '1'):
                raise CircParseError(
                    f"Expected operand, found '{token}'",
                    self.filename, line, col
                )
            operands.append(token)

            next_token = self.tokenizer.peek()
            if next_token == ')':
                break
            elif next_token == ',':
                self.tokenizer.next_token()  # consume ','
            elif next_token is None:
                raise CircParseError(
                    "Expected ')' or ',' in expression",
                    self.filename, line, col
                )

        # Consume ')'
        close_paren = self.tokenizer.next_token()
        if close_paren != ')':
            raise CircParseError(
                f"Expected ')', found '{close_paren}'",
                self.filename, line, col
            )

        # Check arity
        min_arity, max_arity = OPERATOR_ARITY[operator_upper]
        actual_arity = len(operands)

        if max_arity is None:
            if actual_arity < min_arity:
                raise ArityError(operator, actual_arity, f"at least {min_arity}",
                                self.filename, line, col)
        elif actual_arity < min_arity or actual_arity > max_arity:
            if min_arity == max_arity:
                raise ArityError(operator, actual_arity, f"{min_arity}",
                                self.filename, line, col)
            else:
                raise ArityError(operator, actual_arity, f"{min_arity}-{max_arity}",
                                self.filename, line, col)

        # Create assignment
        assignment = Assignment(
            lhs=lhs,
            operator=operator_upper,
            operands=operands,
            line=line,
            col=col
        )
        self.circuit.assignments[lhs] = assignment

    def _check_all_signals_assigned(self):
        """Check that all wires and outputs are assigned."""
        for name in self.circuit.wire_names:
            if name not in self.circuit.assigned_signals:
                raise UnassignedSignalError(name, self.filename)
        for name in self.circuit.output_names:
            if name not in self.circuit.assigned_signals:
                raise UnassignedSignalError(name, self.filename)

    def _check_cycles(self):
        """Check for cycles in the dependency graph."""
        # Build adjacency list for dependencies
        graph = {name: set() for name in self.circuit.wire_names + self.circuit.output_names}

        for lhs, assignment in self.circuit.assignments.items():
            for operand in assignment.operands:
                if operand in graph:
                    graph[lhs].add(operand)

        # DFS to detect cycles
        visited = set()
        recursion_stack = set()
        path = []

        def dfs(node):
            if node in recursion_stack:
                # Found cycle
                cycle_start = path.index(node)
                cycle_path = path[cycle_start:] + [node]
                raise CycleError(cycle_path, self.filename)

            if node in visited:
                return

            visited.add(node)
            recursion_stack.add(node)
            path.append(node)

            for neighbor in graph.get(node, []):
                dfs(neighbor)

            path.pop()
            recursion_stack.remove(node)

        for node in graph:
            if node not in visited:
                dfs(node)


def parse_file(filename: str) -> Circuit:
    """Parse a .circ file and return the circuit."""
    try:
        with open(filename, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(filename)

    parser = Parser(filename, content)
    return parser.parse()


def format_success_json(command: str, **kwargs) -> str:
    """Format a successful JSON response."""
    result = {"ok": True, "command": command}
    result.update(kwargs)
    return json.dumps(result)


def format_error_json(error: CircuitError, command: str, exit_code: ExitCode) -> str:
    """Format an error JSON response."""
    return json.dumps(error.to_json_dict(command, exit_code))


def print_version(json_output: bool):
    """Print version information."""
    if json_output:
        print(format_success_json("__version__", version=__version__))
    else:
        print(__version__)


def cmd_check(filename: str, json_output: bool) -> int:
    """Execute the check command."""
    try:
        circuit = parse_file(filename)

        # Sort signals by name lexicographically
        inputs = sorted(circuit.inputs, key=lambda x: x.name)
        outputs = sorted(circuit.outputs, key=lambda x: x.name)

        # Build output
        inputs_json = [{"name": i.name, "msb": i.msb, "lsb": i.lsb} for i in inputs]
        outputs_json = [{"name": o.name, "msb": o.msb, "lsb": o.lsb} for o in outputs]

        if json_output:
            print(format_success_json(
                "check",
                format="circ",
                inputs=inputs_json,
                outputs=outputs_json
            ))
        else:
            print(f"OK: Circuit loaded successfully")
            print(f"  Inputs: {', '.join(i['name'] for i in inputs_json)}")
            print(f"  Outputs: {', '.join(o['name'] for o in outputs_json)}")

        return ExitCode.SUCCESS.value

    except CircuitError as e:
        if json_output:
            print(format_error_json(e, "check", _get_exit_code(e)))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return _get_exit_code(e).value


# Boolean evaluation for 2-valued logic
def eval_not(operands: list[str], values: dict[str, int]) -> int:
    return 1 - values[operands[0]]

def eval_buf(operands: list[str], values: dict[str, int]) -> int:
    return values[operands[0]]

def eval_and(operands: list[str], values: dict[str, int]) -> int:
    result = 1
    for op in operands:
        result &= values[op]
    return result

def eval_or(operands: list[str], values: dict[str, int]) -> int:
    result = 0
    for op in operands:
        result |= values[op]
    return result

def eval_xor(operands: list[str], values: dict[str, int]) -> int:
    result = 0
    for op in operands:
        result ^= values[op]
    return result

def eval_nand(operands: list[str], values: dict[str, int]) -> int:
    result = 1
    for op in operands:
        result &= values[op]
    return 1 - result

def eval_nor(operands: list[str], values: dict[str, int]) -> int:
    result = 0
    for op in operands:
        result |= values[op]
    return 1 - result

def eval_xnor(operands: list[str], values: dict[str, int]) -> int:
    result = 0
    for op in operands:
        result ^= values[op]
    return 1 - result


OPERATOR_EVAL = {
    "NOT": eval_not,
    "BUF": eval_buf,
    "AND": eval_and,
    "OR": eval_or,
    "XOR": eval_xor,
    "NAND": eval_nand,
    "NOR": eval_nor,
    "XNOR": eval_xnor,
}


def topological_sort_evaluate(circuit: Circuit, input_values: dict[str, int]) -> dict[str, int]:
    """Evaluate circuit in topological order and return signal values."""
    values = dict(input_values)

    # Build dependency graph
    graph = {name: set() for name in circuit.wire_names + circuit.output_names}
    for lhs, assignment in circuit.assignments.items():
        for operand in assignment.operands:
            if operand in graph:
                graph[lhs].add(operand)

    # Kahn's algorithm for topological sort
    in_degree = {node: 0 for node in graph}
    for node in graph:
        for neighbor in graph.get(node, []):
            in_degree[neighbor] = in_degree.get(neighbor, 0) + 1

    # Start with nodes that have no incoming edges (but we need to process in dependency order)
    # Actually, we want to process from inputs to outputs, so we reverse the edges
    # Build reverse graph: node -> nodes that depend on it
    reverse_graph = {node: set() for node in graph}
    for node in graph:
        for neighbor in graph.get(node, []):
            reverse_graph[neighbor].add(node)

    # Calculate in-degree based on reverse graph (how many dependencies)
    dep_count = {node: 0 for node in graph}
    for node in graph:
        for dep in graph.get(node, []):
            if dep in dep_count:  # Only count if dep is also a signal to evaluate
                dep_count[node] += 1

    # Process in order: find nodes whose dependencies are all resolved
    ready = [node for node in graph if dep_count[node] == 0]

    while ready:
        # Sort for deterministic order
        ready.sort()
        node = ready.pop(0)

        # Evaluate this node
        assignment = circuit.assignments[node]
        evaluator = OPERATOR_EVAL[assignment.operator]
        values[node] = evaluator(assignment.operands, values)

        # Update dependents
        for dependent in reverse_graph.get(node, []):
            dep_count[dependent] -= 1
            if dep_count[dependent] == 0:
                ready.append(dependent)

    return values


def cmd_eval(filename: str, set_values: list[str], default: Optional[int],
             allow_extra: bool, json_output: bool) -> int:
    """Execute the eval command."""
    try:
        circuit = parse_file(filename)

        # Parse input values from --set options
        input_values = {}
        for sv in set_values:
            if '=' not in sv:
                raise InputValueParseError(sv, sv)
            name, value_str = sv.split('=', 1)
            if name not in circuit.input_names and not allow_extra:
                raise UnknownInputError(name)
            if name in circuit.input_names:
                # Parse value
                value_str = value_str.strip()
                if value_str not in ('0', '1'):
                    raise InputValueParseError(name, value_str)
                input_values[name] = int(value_str)

        # Apply default to missing inputs
        if default is not None:
            for inp in circuit.input_names:
                if inp not in input_values:
                    input_values[inp] = default

        # Check for missing required inputs
        for inp in circuit.input_names:
            if inp not in input_values:
                raise MissingInputError(inp)

        # Evaluate circuit
        values = topological_sort_evaluate(circuit, input_values)

        # Get output values
        outputs_sorted = sorted(circuit.outputs, key=lambda x: x.name)
        output_values = [{"name": o.name, "msb": 0, "lsb": 0, "value": str(values[o.name])} for o in outputs_sorted]

        if json_output:
            print(format_success_json(
                "eval",
                mode="2val",
                radix="bin",
                outputs=output_values
            ))
        else:
            for out in output_values:
                print(f"{out['name']}={out['value']}")

        return ExitCode.SUCCESS.value

    except CircuitError as e:
        if json_output:
            print(format_error_json(e, "eval", _get_exit_code(e)))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return _get_exit_code(e).value


def _get_exit_code(error: CircuitError) -> ExitCode:
    """Map error type to exit code."""
    mapping = {
        ErrorType.CLI_USAGE_ERROR: ExitCode.CLI_USAGE_ERROR,
        ErrorType.FILE_NOT_FOUND_ERROR: ExitCode.CLI_USAGE_ERROR,
        ErrorType.CIRC_PARSE_ERROR: ExitCode.PARSE_ERROR,
        ErrorType.DECLARATION_AFTER_ASSIGNMENT_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.DUPLICATE_NAME_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.UNDEFINED_NAME_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.UNASSIGNED_SIGNAL_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.INPUT_ASSIGNMENT_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.MULTIPLE_ASSIGNMENT_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.ARITY_ERROR: ExitCode.VALIDATION_ERROR,
        ErrorType.CYCLE_ERROR: ExitCode.VALIDATION_ERROR,
    }
    return mapping.get(error.error_type, ExitCode.INTERNAL_ERROR)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        prog="circopt.py",
        description="Circuit optimizer and validator",
        add_help=False,
        exit_on_error=False
    )

    parser.add_argument('--help', action='store_true', help='Show help message')
    parser.add_argument('--version', action='store_true', help='Show version')
    parser.add_argument('--json', action='store_true', help='Output in JSON format')
    parser.add_argument('command', nargs='?', help='Command to run')
    parser.add_argument('args', nargs=argparse.REMAINDER, help='Command arguments')

    args, unknown = parser.parse_known_args()

    # Handle --help (plain text, even with --json)
    if args.help:
        print("Usage: circopt.py [options] <command> [command-options]")
        print()
        print("Options:")
        print("  --help           Show this help message")
        print("  --version        Show version")
        print("  --json           Output in JSON format")
        print()
        print("Commands:")
        print("  check <file.circ>  Validate a .circ file")
        print("  eval <file.circ>   Evaluate circuit with inputs")
        sys.exit(0)

    # Handle --version
    if args.version:
        print_version(args.json)
        sys.exit(0)

    # No command provided
    if args.command is None:
        if args.json:
            print(json.dumps({
                "ok": False,
                "command": "__cli__",
                "exit_code": ExitCode.CLI_USAGE_ERROR.value,
                "error": {
                    "type": "CliUsageError",
                    "message": "No command provided",
                    "file": None,
                    "line": None,
                    "col": None
                }
            }))
        else:
            print("Error: No command provided", file=sys.stderr)
            print("Try 'circopt.py --help' for more information.", file=sys.stderr)
        sys.exit(ExitCode.CLI_USAGE_ERROR.value)

    # Handle check command
    if args.command == 'check':
        # Parse remaining args
        check_parser = argparse.ArgumentParser(prog="circopt.py check", exit_on_error=False)
        check_parser.add_argument('file', help='Circuit file to check')
        check_parser.add_argument('--json', action='store_true', help='Output in JSON format')

        check_args = check_parser.parse_args(args.args)

        # Use global --json flag if set, otherwise use local
        json_output = args.json or check_args.json

        exit_code = cmd_check(check_args.file, json_output)
        sys.exit(exit_code)

    # Handle eval command
    if args.command == 'eval':
        # Parse remaining args
        eval_parser = argparse.ArgumentParser(prog="circopt.py eval", exit_on_error=False)
        eval_parser.add_argument('file', help='Circuit file to evaluate')
        eval_parser.add_argument('--set', action='append', dest='set_values', default=[],
                                  help='Set input value (name=value)')
        eval_parser.add_argument('--default', type=int, choices=[0, 1],
                                  help='Default value for unspecified inputs')
        eval_parser.add_argument('--allow-extra', action='store_true',
                                  help='Allow extra input names (not in circuit)')
        eval_parser.add_argument('--json', action='store_true',
                                  help='Output in JSON format')

        eval_args = eval_parser.parse_args(args.args)

        # Use global --json flag if set, otherwise use local
        json_output = args.json or eval_args.json

        exit_code = cmd_eval(eval_args.file, eval_args.set_values, eval_args.default,
                              eval_args.allow_extra, json_output)
        sys.exit(exit_code)

    # Unknown command
    if args.json:
        print(json.dumps({
            "ok": False,
            "command": "__cli__",
            "exit_code": ExitCode.CLI_USAGE_ERROR.value,
            "error": {
                "type": "CliUsageError",
                "message": f"Unknown command: {args.command}",
                "file": None,
                "line": None,
                "col": None
            }
        }))
    else:
        print(f"Error: Unknown command '{args.command}'", file=sys.stderr)
        print("Try 'circopt.py --help' for more information.", file=sys.stderr)
    sys.exit(ExitCode.CLI_USAGE_ERROR.value)


if __name__ == "__main__":
    main()
