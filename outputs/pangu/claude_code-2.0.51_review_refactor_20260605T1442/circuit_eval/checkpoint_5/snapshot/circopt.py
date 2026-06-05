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
    name: str
    msb: int = 0
    lsb: int = 0


@dataclass
class Circuit:
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
    UNKNOWN_INPUT_FORMAT_ERROR = "UnknownInputFormatError"
    JSON_PARSE_ERROR = "JsonParseError"
    JSON_SCHEMA_ERROR = "JsonSchemaError"
    BENCH_PARSE_ERROR = "BenchParseError"
    REDEFINITION_ERROR = "RedefinitionError"


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


class UnknownInputFormatError(CircuitError):
    def __init__(self, filename: str, format: str):
        super().__init__(
            f"Unknown input format: {format}",
            file=filename
        )
        self.error_type = ErrorType.UNKNOWN_INPUT_FORMAT_ERROR


class JsonParseError(CircuitError):
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.error_type = ErrorType.JSON_PARSE_ERROR


class JsonSchemaError(CircuitError):
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.error_type = ErrorType.JSON_SCHEMA_ERROR


class BenchParseError(CircuitError):
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.error_type = ErrorType.BENCH_PARSE_ERROR


class RedefinitionError(CircuitError):
    def __init__(self, name: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(f"Redefinition: {name}", file, line, col)
        self.error_type = ErrorType.REDEFINITION_ERROR


class WidthMismatchError(CircuitError):
    def __init__(self, operator: str, width1: int, width2: int,
                 file: Optional[str] = None, line: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(
            f"Width mismatch for {operator}: operand widths {width1} and {width2} do not match",
            file, line, col
        )
        self.error_type = ErrorType.VALIDATION_ERROR  # Exit code 3


class IndexOutOfBoundsError(CircuitError):
    def __init__(self, signal: str, index: int, width: int,
                 file: Optional[str] = None, line: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(
            f"Index {index} out of bounds for signal {signal} with width {width}",
            file, line, col
        )
        self.error_type = ErrorType.VALIDATION_ERROR  # Exit code 3


class InputWidthMismatchError(CircuitError):
    def __init__(self, name: str, declared_width: int, provided_width: int,
                 file: Optional[str] = None, line: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(
            f"Input {name} declared width {declared_width} does not match provided width {provided_width}",
            file, line, col
        )
        self.error_type = ErrorType.VALIDATION_ERROR  # Exit code 3


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


class RadixNotAllowedIn3ValError(CircuitError):
    def __init__(self, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(
            "Radix not allowed in 3-valued mode: only --radix bin is allowed",
            file, line, col
        )
        self.error_type = ErrorType.CLI_USAGE_ERROR  # Exit code 1


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
    "MUX": (3, 3),     # exactly 3
    "ITE": (3, 3),     # exactly 3
    "REDUCE_AND": (1, 1),
    "REDUCE_OR": (1, 1),
    "REDUCE_XOR": (1, 1),
    "EQ": (2, 2),      # exactly 2
}


def is_valid_identifier(name: str) -> bool:
    """Check if a string is a valid identifier."""
    if not name:
        return False
    if name[0].isdigit():
        return False
    return all(c.isalnum() or c == '_' for c in name)


class Tokenizer:
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
            self.pos += 1
            self.col += 1
            # If next char is alphanumeric or _, it's part of identifier
            # The actual name would be 'X' + something
            return 'X'

        # Unexpected character
        raise CircParseError(
            f"Unexpected character: '{ch}'",
            self.filename,
            self.line,
            self.col
        )


class Parser:
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
            if token.upper() == 'INPUT':
                self._parse_declaration("input", 'input_names', 'inputs', line, col)
            elif token.upper() == 'OUTPUT':
                self._parse_declaration("output", 'output_names', 'outputs', line, col)
            elif token.upper() == 'WIRE':
                self._parse_declaration("wire", 'wire_names', None, line, col)
            else:
                # Assignment or identifier expression
                if self.assignment_started:
                    raise CircParseError(
                        f"Expected assignment or end of file",
                        self.filename, line, col
                    )
                self._parse_assignment(line, col)

        self._check_all_signals_assigned()
        self._check_cycles()
        return self.circuit

    def _parse_declaration(self, decl_type: str, names_field: str,
                            inputs_or_outputs_field: str, line: int, col: int):
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
                if inputs_or_outputs_field == 'input_names':
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


def parse_file(filename: str, format: str = "auto") -> Circuit:
    """Parse a circuit file and return the circuit."""
    try:
        with open(filename, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(filename)

    # Determine format from extension if auto
    if format == "auto":
        if filename.endswith('.circ'):
            format = 'circ'
        elif filename.endswith('.json'):
            format = 'json'
        elif filename.endswith('.bench'):
            format = 'bench'
        else:
            raise UnknownInputFormatError(filename, filename)

    # Parse based on format
    if format == 'circ':
        parser = Parser(filename, content)
        return parser.parse()
    elif format == 'json':
        return parse_json_file(filename, content)
    elif format == 'bench':
        return parse_bench_file(filename, content)
    else:
        raise UnknownInputFormatError(filename, format)


def parse_json_file(filename: str, content: str) -> Circuit:
    """Parse a JSON circuit file and return the Circuit."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise JsonParseError(
            f"Invalid JSON: {e}",
            file=filename,
            line=e.lineno,
            col=e.colno
        )

    # Validate schema
    if not isinstance(data, dict):
        raise JsonSchemaError("JSON root must be an object", filename)

    required_keys = ["format_version", "inputs", "outputs", "wires", "assignments"]
    for key in required_keys:
        if key not in data:
            raise JsonSchemaError(f"Missing required key: {key}", filename)

    if data["format_version"] != 1:
        raise JsonSchemaError(
            f"Unsupported format_version: {data['format_version']}, expected 1",
            filename
        )

    # Parse port list
    def parse_port(port_obj, context):
        if not isinstance(port_obj, dict):
            raise JsonSchemaError(f"Port must be an object in {context}", filename)
        if "name" not in port_obj:
            raise JsonSchemaError(f"Port missing 'name' in {context}", filename)
        msb = port_obj.get("msb", 0)
        lsb = port_obj.get("lsb", 0)
        if not isinstance(msb, int) or not isinstance(lsb, int):
            raise JsonSchemaError(f"Port msb/lsb must be integers in {context}", filename)
        if msb < lsb or lsb < 0:
            raise JsonSchemaError(
                f"Invalid msb/lsb in port {port_obj['name']}: msb ({msb}) < lsb ({lsb}) or lsb < 0",
                filename
            )
        name = port_obj["name"]
        if not is_valid_identifier(name):
            raise JsonSchemaError(f"Invalid identifier in port: {name}", filename)
        return SignalInfo(name=name, msb=msb, lsb=lsb)

    # Parse inputs
    inputs = []
    input_names = []
    if not isinstance(data["inputs"], list):
        raise JsonSchemaError("inputs must be a list", filename)
    for i, port_obj in enumerate(data["inputs"]):
        info = parse_port(port_obj, f"inputs[{i}]")
        inputs.append(info)
        if info.name in input_names:
            raise JsonSchemaError(f"Duplicate input name: {info.name}", filename)
        input_names.append(info.name)

    # Parse outputs
    outputs = []
    output_names = []
    if not isinstance(data["outputs"], list):
        raise JsonSchemaError("outputs must be a list", filename)
    for i, port_obj in enumerate(data["outputs"]):
        info = parse_port(port_obj, f"outputs[{i}]")
        outputs.append(info)
        if info.name in output_names:
            raise JsonSchemaError(f"Duplicate output name: {info.name}", filename)
        output_names.append(info.name)

    # Parse wires
    wires_list = []
    wire_names = []
    if not isinstance(data["wires"], list):
        raise JsonSchemaError("wires must be a list", filename)
    for i, port_obj in enumerate(data["wires"]):
        info = parse_port(port_obj, f"wires[{i}]")
        wires_list.append(info)
        if info.name in wire_names:
            raise JsonSchemaError(f"Duplicate wire name: {info.name}", filename)
        wire_names.append(info.name)

    # Parse assignments
    if not isinstance(data["assignments"], list):
        raise JsonSchemaError("assignments must be a list", filename)

    circuit = Circuit(
        inputs=inputs,
        outputs=outputs,
        input_names=input_names,
        output_names=output_names,
        wire_names=wire_names
    )

    # Track seen LHS for redefinition check
    seen_lhs = set()

    # Expressions from the Parser are reused, so create a temporary Parser for expression parsing
    # We need a dummy filename and content to create a Parser
    for i, assign_obj in enumerate(data["assignments"]):
        if not isinstance(assign_obj, dict):
            raise JsonSchemaError(f"Assignment {i} must be an object", filename)

        if "lhs" not in assign_obj:
            raise JsonSchemaError(f"Assignment {i} missing 'lhs'", filename)
        if "rhs" not in assign_obj:
            raise JsonSchemaError(f"Assignment {i} missing 'rhs'", filename)

        lhs = assign_obj["lhs"]
        rhs = assign_obj["rhs"]

        if not isinstance(lhs, str):
            raise JsonSchemaError(f"Assignment {i} lhs must be a string", filename)
        if not isinstance(rhs, str):
            raise JsonSchemaError(f"Assignment {i} rhs must be a string", filename)

        if not is_valid_identifier(lhs):
            raise JsonSchemaError(f"Invalid identifier in lhs: {lhs}", filename)

        if lhs in seen_lhs:
            raise RedefinitionError(lhs, filename)
        seen_lhs.add(lhs)

        # Check if lhs is defined
        is_defined = False
        if lhs in input_names:
            raise InputAssignmentError(lhs, filename)
        if lhs in wire_names or lhs in output_names:
            is_defined = True

        if not is_defined:
            raise UndefinedNameError(lhs, filename)

        # Parse expression using a temporary parser for the RHS
        # The expr is a full expression like "XOR(a, b)"
        # We use the existing Parser's ability to parse expressions
        # To do this, we create a dummy parser with the RHS in it
        # and hijack some of the parsing logic
        # Actually, let's create a mini-parser for expressions

        # We'll tokenize the RHS string and parse it manually here
        expr_tokenizer = Tokenizer(filename, rhs)
        parsed_operator = None
        parsed_operands = []
        parse_line = 0
        parse_col = 0

        # Get operator
        op = expr_tokenizer.next_token()
        if op is None:
            raise JsonSchemaError(
                f"Assignment {i}: Empty RHS expression",
                filename, parse_line, parse_col
            )
        op_upper = op.upper()
        if op_upper not in OPERATOR_ARITY:
            raise JsonSchemaError(
                f"Assignment {i}: Unknown operator: {op}",
                filename, parse_line, parse_col
            )
        parsed_operator = op_upper

        # Check for '('
        open_paren = expr_tokenizer.next_token()
        if open_paren != '(':
            raise JsonSchemaError(
                f"Assignment {i}: Expected '(', found '{open_paren}'",
                filename, parse_line, parse_col
            )

        # Parse operands
        while True:
            token = expr_tokenizer.next_token()
            if token is None:
                raise JsonSchemaError(
                    f"Assignment {i}: Unexpected end of expression",
                    filename, parse_line, parse_col
                )

            if token == 'X':
                # Check if it's a standalone X literal
                # X as a literal is NOT allowed in JSON format (same as .circ)
                next_t = expr_tokenizer.peek()
                if next_t is None or not (next_t.isalpha() or next_t == '_'):
                    raise JsonParseError(
                        "Literal 'X' value is not allowed",
                        filename, parse_line, parse_col
                    )

            if not is_valid_identifier(token) and token not in ('0', '1'):
                raise JsonParseError(
                    f"Assignment {i}: Expected operand, found '{token}'",
                    filename, parse_line, parse_col
                )
            parsed_operands.append(token)

            next_token = expr_tokenizer.peek()
            if next_token == ')':
                expr_tokenizer.next_token()  # consume ')'
                break
            elif next_token == ',':
                expr_tokenizer.next_token()  # consume ','
            elif next_token is None:
                raise JsonParseError(
                    f"Assignment {i}: Expected ')' or ',' in expression",
                    filename, parse_line, parse_col
                )

        # Check arity
        min_arity, max_arity = OPERATOR_ARITY[parsed_operator]
        actual_arity = len(parsed_operands)

        if max_arity is None:
            if actual_arity < min_arity:
                raise ArityError(parsed_operator, actual_arity, f"at least {min_arity}",
                                filename, parse_line, parse_col)
        elif actual_arity < min_arity or actual_arity > max_arity:
            if min_arity == max_arity:
                raise ArityError(parsed_operator, actual_arity, f"{min_arity}",
                                filename, parse_line, parse_col)
            else:
                raise ArityError(parsed_operator, actual_arity, f"{min_arity}-{max_arity}",
                                filename, parse_line, parse_col)

        # Create assignment
        assignment = Assignment(
            lhs=lhs,
            operator=parsed_operator,
            operands=parsed_operands,
            line=parse_line,
            col=parse_col
        )

        circuit.assignments[lhs] = assignment
        circuit.assigned_signals.add(lhs)

    # Check that all wires and outputs are assigned
    for name in wire_names:
        if name not in circuit.assigned_signals:
            raise UnassignedSignalError(name, filename)
    for name in output_names:
        if name not in circuit.assigned_signals:
            raise UnassignedSignalError(name, filename)

    # Check for cycles - reuse the circuit checking logic
    graph = {name: set() for name in wire_names + output_names}
    for lhs, assignment in circuit.assignments.items():
        for operand in assignment.operands:
            if operand in graph:
                graph[lhs].add(operand)

    # DFS to detect cycles
    visited = set()
    recursion_stack = set()
    path = []

    def dfs(node):
        if node in recursion_stack:
            cycle_start = path.index(node)
            cycle_path = path[cycle_start:] + [node]
            raise CycleError(cycle_path, filename)

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

    return circuit


def parse_bench_file(filename: str, content: str) -> Circuit:
    """Parse a BENCH format circuit file and return the Circuit."""
    lines = content.split('\n')

    circuit = Circuit()
    # Track defined signals (inputs + wires as they are assigned)
    defined_signals = set()  # Signals that can be used as operands
    assigned_signals = set()  # Signals that have assignments
    wire_names_list = []  # Track order of wire names for cycle checking
    output_names_list = []  # Track names for cycle checking

    # Also track declared outputs (with OUTPUT() statements)
    declared_outputs = set()

    for line_num, line in enumerate(lines, start=1):
        # Strip comment and whitespace
        if '#' in line:
            line = line.split('#', 1)[0]
        line = line.strip()

        if not line:
            continue

        col = 1

        # Check for brackets in identifiers - this is not allowed in BENCH
        # (scalars only, so no [MSB:LSB] syntax)
        if '[' in line or ']' in line:
            raise BenchParseError(
                "BENCH format does not support vector signals (brackets not allowed)",
                filename, line_num, 1
            )

        # Parse INPUT(name)
        if line.startswith('INPUT('):
            if not line.endswith(')'):
                raise BenchParseError(
                    "Malformed INPUT statement: missing closing parenthesis",
                    filename, line_num, 1
                )
            name = line[6:-1]  # Extract name from INPUT(name)
            if not is_valid_identifier(name):
                raise BenchParseError(
                    f"Invalid identifier in INPUT: {name}",
                    filename, line_num, 1
                )
            if name in circuit.input_names:
                raise RedefinitionError(name, filename, line_num, 1)
            circuit.input_names.append(name)
            circuit.inputs.append(SignalInfo(name=name, msb=0, lsb=0))
            defined_signals.add(name)

        # Parse OUTPUT(name)
        elif line.startswith('OUTPUT('):
            if not line.endswith(')'):
                raise BenchParseError(
                    "Malformed OUTPUT statement: missing closing parenthesis",
                    filename, line_num, 1
                )
            name = line[7:-1]  # Extract name from OUTPUT(name)
            if not is_valid_identifier(name):
                raise BenchParseError(
                    f"Invalid identifier in OUTPUT: {name}",
                    filename, line_num, 1
                )
            if name in declared_outputs:
                raise RedefinitionError(name, filename, line_num, 1)
            if name in circuit.output_names:
                raise RedefinitionError(name, filename, line_num, 1)
            declared_outputs.add(name)
            circuit.output_names.append(name)
            circuit.outputs.append(SignalInfo(name=name, msb=0, lsb=0))

        # Parse assignment: lhs = OP(arg1, arg2, ...)
        else:
            parts = line.split('=', 1)
            if len(parts) != 2:
                raise BenchParseError(
                    "Expected '=' in assignment or valid INPUT/OUTPUT statement",
                    filename, line_num, 1
                )

            lhs = parts[0].strip()
            rhs = parts[1].strip()

            # Validate lhs identifier
            if not is_valid_identifier(lhs):
                raise BenchParseError(
                    f"Invalid identifier in LHS: {lhs}",
                    filename, line_num, 1
                )

            # Check if lhs is already defined (as input or wire)
            if lhs in circuit.input_names:
                raise InputAssignmentError(lhs, filename, line_num, 1)

            if lhs in defined_signals:
                raise RedefinitionError(lhs, filename, line_num, 1)

            # Parse RHS: OP(args...)
            if '(' not in rhs or not rhs.endswith(')'):
                raise BenchParseError(
                    "Expected operator call in RHS: OP(args...)",
                    filename, line_num, 1
                )

            op_name = rhs.split('(')[0]
            args_str = rhs[len(op_name)+1:-1]  # Remove OP( and trailing )
            op_upper = op_name.upper()

            # Parse arguments
            if args_str:
                args = [arg.strip() for arg in args_str.split(',')]
            else:
                args = []

            # Check that LHS is declared as output (implicit wire declaration in BENCH)
            # If not, we need to treat it as a wire
            if lhs in circuit.output_names:
                # Already declared as output, good
                pass
            else:
                # Implicit wire declaration
                circuit.wire_names.append(lhs)

            # Mark LHS as defined and assigned
            defined_signals.add(lhs)

            # Validate arguments - must be identifiers or literals (BENCH allows 0/1 as literals)
            for arg in args:
                if not is_valid_identifier(arg) and arg not in ('0', '1'):
                    # BENCH does not allow literals in RHS
                    raise BenchParseError(
                        f"Literals not allowed in BENCH RHS: {arg}",
                        filename, line_num, 1
                    )
                # Check that argument is defined (either an input or already assigned)
                if arg not in defined_signals:
                    raise UndefinedNameError(arg, filename, line_num, 1)

            # Check operator arity
            if op_upper not in OPERATOR_ARITY:
                raise BenchParseError(
                    f"Unknown operator: {op_name}",
                    filename, line_num, 1
                )

            min_arity, max_arity = OPERATOR_ARITY[op_upper]
            actual_arity = len(args)

            if max_arity is None:
                if actual_arity < min_arity:
                    raise ArityError(op_upper, actual_arity, f"at least {min_arity}",
                                    filename, line_num, 1)
            elif actual_arity < min_arity or actual_arity > max_arity:
                if min_arity == max_arity:
                    raise ArityError(op_upper, actual_arity, f"{min_arity}",
                                    filename, line_num, 1)
                else:
                    raise ArityError(op_upper, actual_arity, f"{min_arity}-{max_arity}",
                                    filename, line_num, 1)

            # Create assignment
            assignment = Assignment(
                lhs=lhs,
                operator=op_upper,
                operands=args,
                line=line_num,
                col=1
            )

            circuit.assignments[lhs] = assignment

            # LHS is now an assigned signal
            # It's already in defined_signals for future references
            assigned_signals.add(lhs)

    # Check that all declared outputs have been referenced (defined as input or assigned)
    for out_name in circuit.output_names:
        if out_name not in defined_signals:
            raise UndefinedNameError(out_name, filename)

    # Check for cycles
    all_signals = circuit.wire_names + circuit.output_names
    graph = {name: set() for name in all_signals}
    for lhs, assignment in circuit.assignments.items():
        for operand in assignment.operands:
            if operand in graph:
                graph[lhs].add(operand)

    visited = set()
    recursion_stack = set()
    path = []

    def dfs(node):
        if node in recursion_stack:
            cycle_start = path.index(node)
            cycle_path = path[cycle_start:] + [node]
            raise CycleError(cycle_path, filename)

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

    return circuit


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


def cmd_check(filename: str, json_output: bool, format: str) -> int:
    """Execute the check command."""
    try:
        circuit = parse_file(filename, format)

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
    heapq = __import__('heapq')

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
    heapq.heapify(ready)

    while ready:
        node = heapq.heappop(ready)

        # Evaluate this node
        assignment = circuit.assignments[node]
        evaluator = OPERATOR_EVAL[assignment.operator]
        values[node] = evaluator(assignment.operands, values)

        # Update dependents
        for dependent in reverse_graph.get(node, []):
            dep_count[dependent] -= 1
            if dep_count[dependent] == 0:
                heapq.heappush(ready, dependent)

    return values


def parse_binary_value(value_str: str, mode: str) -> list[int]:
    """Parse a binary value string into a list of bits (0, 1, or 'X').

    Supports formats:
    - 0, 1, X (scalar)
    - 0b[01Xx_]+ (unsized binary)
    - <N>'b[01Xx_]+ (sized binary)

    In 2val mode, X is not allowed in binary strings.
    """
    value_str = value_str.strip().lower()

    # Scalar values
    if value_str in ('0', '1', 'x'):
        return [0 if value_str == '0' else 1]

    # Binary format
    if value_str.startswith('0b'):
        bits_str = value_str[2:]
        # Remove underscores
        bits_str = bits_str.replace('_', '')
        result = []
        for ch in bits_str:
            if ch == '0':
                result.append(0)
            elif ch == '1':
                result.append(1)
            elif ch == 'x':
                if mode == '2val':
                    raise InputValueParseError("binary", value_str)
                result.append('X')
            else:
                raise InputValueParseError("binary", value_str)
        return result

    # Sized binary format: <N>'b...
    if "'b" in value_str:
        parts = value_str.split("'b")
        if len(parts) != 2:
            raise InputValueParseError("sized binary", value_str)
        width_str, bits_str = parts
        try:
            width = int(width_str)
        except ValueError:
            raise InputValueParseError("sized binary", value_str)

        # Remove underscores
        bits_str = bits_str.replace('_', '')

        # Check width match (after removing underscores, but we already did that)
        if len(bits_str) != width:
            raise InputWidthMismatchError(
                "sized binary", width, len(bits_str)
            )

        result = []
        for ch in bits_str:
            if ch == '0':
                result.append(0)
            elif ch == '1':
                result.append(1)
            elif ch == 'x':
                if mode == '2val':
                    raise InputValueParseError("sized binary", value_str)
                result.append('X')
            else:
                raise InputValueParseError("sized binary", value_str)
        return result

    # Hex and decimal formats - only allowed in 2val mode
    if mode == '3val':
        # Check if this looks like hex or decimal
        if value_str.startswith('0x') or ("'h" in value_str) or ("'d" in value_str):
            raise InputValueParseError("non-binary format in 3val mode", value_str)

    # Try to parse as decimal (for 2val mode)
    try:
        val = int(value_str)
        # Convert to binary bits
        if val == 0:
            return [0]
        bits = []
        while val > 0:
            bits.append(val & 1)
            val >>= 1
        return bits[::-1]  # Reverse to MSB first
    except ValueError:
        raise InputValueParseError("value", value_str)


def parse_input_value(value_str: str, mode: str) -> list:
    """Parse an input value string into a list of bits (0, 1, or 'X')."""
    value_str = value_str.strip()

    # Check for binary format
    if value_str.startswith('0b') or ("'b" in value_str):
        return parse_binary_value(value_str, mode)

    # Scalar values (0, 1, x/X)
    if value_str.lower() in ('0', '1', 'x'):
        return [0 if value_str == '0' else 1]

    # Hex format (not allowed in 3val mode)
    if mode == '3val':
        if value_str.startswith('0x') or ("'h" in value_str):
            raise InputValueParseError(value_str, "hex not allowed in 3val mode")

    # Decimal format (not allowed in 3val mode)
    if mode == '3val':
        if value_str[0].isdigit() and not value_str.startswith('0b'):
            raise InputValueParseError(value_str, "decimal not allowed in 3val mode")

    # Try to parse as decimal (for 2val mode fallback)
    try:
        val = int(value_str)
        if val == 0:
            return [0]
        bits = []
        while val > 0:
            bits.append(val & 1)
            val >>= 1
        return bits[::-1]  # Reverse to MSB first
    except ValueError:
        raise InputValueParseError("input", value_str)


def bits_to_output(value_bits: list, width: int, radix: str) -> str:
    """Convert a list of bits to output string."""
    # Pad or truncate to width
    if len(value_bits) < width:
        value_bits = [0] * (width - len(value_bits)) + value_bits
    elif len(value_bits) > width:
        value_bits = value_bits[-width:]

    # If width is 1, use scalar format
    if width == 1:
        bit = value_bits[0]
        if bit == 0:
            return "0"
        elif bit == 1:
            return "1"
        else:
            return "X"

    # Vector format
    bits_str = ''.join(str(b) if b != 'X' else 'X' for b in value_bits)

    if radix == 'bin':
        return "0b" + bits_str
    elif radix == 'hex':
        # Convert binary to hex
        # Pad to multiple of 4
        padded = bits_str
        while len(padded) % 4 != 0:
            padded = '0' + padded
        hex_digits = []
        for i in range(0, len(padded), 4):
            nibble = padded[i:i+4]
            # Check for X in this nibble
            if 'X' in nibble:
                hex_digits.append('X')
            else:
                val = int(nibble, 2)
                hex_digits.append(hex(val)[2:])
        return "0x" + ''.join(hex_digits)
    else:  # dec
        val = int(bits_str, 2)
        return str(val)


# 3-valued evaluation functions

def tv_and(a, b):
    """3-valued AND."""
    if a == 0 or b == 0:
        return 0
    if a == 'X' or b == 'X':
        return 'X'
    return 1

def tv_or(a, b):
    """3-valued OR."""
    if a == 1 or b == 1:
        return 1
    if a == 'X' or b == 'X':
        return 'X'
    return 0

def tv_xor(a, b):
    """3-valued XOR."""
    if a == 'X' or b == 'X':
        return 'X'
    return a ^ b

def tv_not(a):
    """3-valued NOT."""
    if a == 'X':
        return 'X'
    return 1 - a

def tv_buf(a):
    """3-valued BUF."""
    return a

def tv_mux(sel, a, b):
    """3-valued MUX."""
    if sel == 0:
        return b
    if sel == 1:
        return a
    # sel == 'X'
    if a == b:
        return a
    return 'X'

def tv_eq(bits_a, bits_b):
    """3-valued EQ - returns a single bit value."""
    if len(bits_a) != len(bits_b):
        raise WidthMismatchError("EQ", len(bits_a), len(bits_b))

    result = 1
    for a, b in zip(bits_a, bits_b):
        if a == 'X' or b == 'X':
            # If one is X but the other is definite, they might still be equal
            if a != b and a != 'X' and b != 'X':
                return 0  # Definitely unequal
            result = 'X'
        elif a != b:
            return 0  # Definitely unequal
    return result

def tv_reduce_and(bits):
    """3-valued REDUCE_AND."""
    result = 1
    for b in bits:
        if b == 0:
            return 0
        if b == 'X':
            result = 'X'
    return result

def tv_reduce_or(bits):
    """3-valued REDUCE_OR."""
    result = 0
    for b in bits:
        if b == 1:
            return 1
        if b == 'X':
            result = 'X'
    return result

def tv_reduce_xor(bits):
    """3-valued REDUCE_XOR."""
    result = 0
    for b in bits:
        if b == 'X':
            return 'X'
        result ^= b
    return result


def evaluate_operator_3val(operator: str, operands: list[str], values: dict) -> list:
    """Evaluate an operator in 3-valued mode. Returns a list of bits."""
    if operator in ('NOT', 'BUF'):
        operand_bits = values[operands[0]]
        if operator == 'NOT':
            result = [tv_not(b) for b in operand_bits]
        else:
            result = [tv_buf(b) for b in operand_bits]
        return result

    elif operator in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
        # Get all operand bits
        operand_bits_list = [values[op] for op in operands]

        # Check all have same width
        widths = [len(bits) for bits in operand_bits_list]
        if len(set(widths)) != 1:
            raise WidthMismatchError(operator, widths[0], widths[1] if len(widths) > 1 else widths[0])

        width = widths[0]
        result = []
        for i in range(width):
            bits_at_pos = [bits[i] for bits in operand_bits_list]
            if operator == 'AND':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_and(val, b)
                result.append(val)
            elif operator == 'OR':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_or(val, b)
                result.append(val)
            elif operator == 'XOR':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_xor(val, b)
                result.append(val)
            elif operator == 'NAND':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_and(val, b)
                result.append(tv_not(val))
            elif operator == 'NOR':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_or(val, b)
                result.append(tv_not(val))
            elif operator == 'XNOR':
                val = bits_at_pos[0]
                for b in bits_at_pos[1:]:
                    val = tv_xor(val, b)
                result.append(tv_not(val))
        return result

    elif operator == 'MUX':
        sel_bits = values[operands[0]]
        a_bits = values[operands[1]]
        b_bits = values[operands[2]]

        if not (len(sel_bits) == len(a_bits) == len(b_bits)):
            raise WidthMismatchError("MUX", len(sel_bits), len(a_bits))

        result = []
        for i, (sel, a, b) in enumerate(zip(sel_bits, a_bits, b_bits)):
            result.append(tv_mux(sel, a, b))
        return result

    elif operator == 'EQ':
        a_bits = values[operands[0]]
        b_bits = values[operands[1]]
        result_bit = tv_eq(a_bits, b_bits)
        return [result_bit]

    elif operator == 'REDUCE_AND':
        bits = values[operands[0]]
        result = tv_reduce_and(bits)
        return [result]

    elif operator == 'REDUCE_OR':
        bits = values[operands[0]]
        result = tv_reduce_or(bits)
        return [result]

    elif operator == 'REDUCE_XOR':
        bits = values[operands[0]]
        result = tv_reduce_xor(bits)
        return [result]

    else:
        raise ValueError(f"Unknown operator: {operator}")


def topological_sort_evaluate_3val(circuit: Circuit, input_values: dict[str, list]) -> dict[str, list]:
    """Evaluate circuit in topological order using 3-valued logic."""
    heapq = __import__('heapq')

    values = dict(input_values)

    # Build dependency graph
    graph = {name: set() for name in circuit.wire_names + circuit.output_names}
    for lhs, assignment in circuit.assignments.items():
        for operand in assignment.operands:
            if operand in graph:
                graph[lhs].add(operand)

    # Build reverse graph: node -> nodes that depend on it
    reverse_graph = {node: set() for node in graph}
    for node in graph:
        for neighbor in graph.get(node, []):
            reverse_graph[neighbor].add(node)

    # Calculate dependency count
    dep_count = {node: 0 for node in graph}
    for node in graph:
        for dep in graph.get(node, []):
            if dep in dep_count:
                dep_count[node] += 1

    # Process in order
    ready = [node for node in graph if dep_count[node] == 0]
    heapq.heapify(ready)

    while ready:
        node = heapq.heappop(ready)

        # Evaluate this node
        assignment = circuit.assignments[node]
        values[node] = evaluate_operator_3val(assignment.operator, assignment.operands, values)

        # Update dependents
        for dependent in reverse_graph.get(node, []):
            dep_count[dependent] -= 1
            if dep_count[dependent] == 0:
                heapq.heappush(ready, dependent)

    return values


def cmd_eval(filename: str, set_values: list[str], default: Optional[int],
             allow_extra: bool, json_output: bool, mode: str, radix: str,
             format: str) -> int:
    """Execute the eval command."""
    try:
        circuit = parse_file(filename, format)

        # Check radix restriction for 3val mode
        if mode == '3val' and radix != 'bin':
            raise RadixNotAllowedIn3ValError()

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
                bits = parse_input_value(value_str, mode)
                input_values[name] = bits

        # Apply default to missing inputs
        if default is not None:
            default_bits = [default]
            for inp in circuit.input_names:
                if inp not in input_values:
                    input_values[inp] = default_bits

        # Check for missing required inputs
        for inp in circuit.input_names:
            if inp not in input_values:
                raise MissingInputError(inp)

        # Check input widths match declared widths
        for inp_info in circuit.inputs:
            if inp_info.name in input_values:
                provided_bits = input_values[inp_info.name]
                declared_width = inp_info.msb - inp_info.lsb + 1
                if len(provided_bits) != declared_width:
                    raise InputWidthMismatchError(
                        inp_info.name, declared_width, len(provided_bits)
                    )

        # Evaluate circuit
        if mode == '2val':
            # Convert 3val-style bit lists to integers for 2-valued evaluation
            int_input_values = {}
            for name, bits in input_values.items():
                # Check for X in 2val mode
                if 'X' in bits:
                    raise InputValueParseError(name, "X not allowed in 2val mode")
                # Convert bits to integer
                val = 0
                for b in bits:
                    val = (val << 1) | b
                int_input_values[name] = val
            values = topological_sort_evaluate(circuit, int_input_values)
            # Convert integer results back to bit lists
            bit_values = {}
            for name, val in values.items():
                # Get width from assignment or output declaration
                if name in circuit.output_names:
                    out_info = next(o for o in circuit.outputs if o.name == name)
                    width = out_info.msb - out_info.lsb + 1
                else:
                    width = 1
                bits = []
                for i in range(width):
                    bit = (val >> (width - 1 - i)) & 1
                    bits.append(bit)
                bit_values[name] = bits
        else:
            bit_values = topological_sort_evaluate_3val(circuit, input_values)

        # Get output values
        outputs_sorted = sorted(circuit.outputs, key=lambda x: x.name)
        output_values = []
        for o in outputs_sorted:
            bits = bit_values[o.name]
            width = o.msb - o.lsb + 1
            output_str = bits_to_output(bits, width, radix)
            output_values.append({
                "name": o.name,
                "msb": o.msb,
                "lsb": o.lsb,
                "value": output_str
            })

        if json_output:
            print(format_success_json(
                "eval",
                mode=mode,
                radix=radix,
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
        check_parser.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                                   help='Input format: auto (default), circ, json, or bench')

        check_args = check_parser.parse_args(args.args)

        # Use global --json flag if set, otherwise use local
        json_output = args.json or check_args.json

        exit_code = cmd_check(check_args.file, json_output, check_args.format)
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
        eval_parser.add_argument('--mode', choices=['2val', '3val'], default='2val',
                                  help='Evaluation mode: 2val (default) or 3val')
        eval_parser.add_argument('--radix', choices=['bin', 'hex', 'dec'], default='bin',
                                  help='Output radix: bin (default), hex, or dec')
        eval_parser.add_argument('--json', action='store_true',
                                  help='Output in JSON format')
        eval_parser.add_argument('--format', choices=['auto', 'circ', 'json', 'bench'], default='auto',
                                  help='Input format: auto (default), circ, json, or bench')

        eval_args = eval_parser.parse_args(args.args)

        # Use global --json flag if set, otherwise use local
        json_output = args.json or eval_args.json

        exit_code = cmd_eval(eval_args.file, eval_args.set_values, eval_args.default,
                              eval_args.allow_extra, json_output, eval_args.mode, eval_args.radix,
                              eval_args.format)
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
