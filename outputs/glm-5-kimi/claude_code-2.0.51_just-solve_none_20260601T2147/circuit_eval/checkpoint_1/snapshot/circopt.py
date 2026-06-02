#!/usr/bin/env python3
"""Circuit optimizer CLI tool - Part 1 implementation."""

import sys
import json
import re
from dataclasses import dataclass, field
from typing import Optional, Any


VERSION = "1.0.0"


# =============================================================================
# Error Types
# =============================================================================

class CircError(Exception):
    """Base class for circuit errors."""
    def __init__(self, error_type: str, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self) -> dict:
        result = {
            "type": self.error_type,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "col": self.col
        }
        return result


class CliUsageError(CircError):
    def __init__(self, message: str):
        super().__init__("CliUsageError", message)


class FileNotFoundError_(CircError):
    def __init__(self, message: str, file: Optional[str] = None):
        super().__init__("FileNotFoundError", message, file=file)


class CircParseError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("CircParseError", message, file=file, line=line, col=col)


class DeclarationAfterAssignmentError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("DeclarationAfterAssignmentError", message, file=file, line=line, col=col)


class DuplicateNameError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("DuplicateNameError", message, file=file, line=line, col=col)


class UndefinedNameError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("UndefinedNameError", message, file=file, line=line, col=col)


class UnassignedSignalError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("UnassignedSignalError", message, file=file, line=line, col=col)


class InputAssignmentError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("InputAssignmentError", message, file=file, line=line, col=col)


class MultipleAssignmentError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("MultipleAssignmentError", message, file=file, line=line, col=col)


class ArityError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("ArityError", message, file=file, line=line, col=col)


class CycleError(CircError):
    def __init__(self, message: str, file: Optional[str] = None, line: Optional[int] = None, col: Optional[int] = None):
        super().__init__("CycleError", message, file=file, line=line, col=col)


# =============================================================================
# Exit Codes
# =============================================================================

EXIT_SUCCESS = 0
EXIT_CLI_USAGE = 1
EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4
EXIT_NON_EQUIVALENCE = 10


# =============================================================================
# Circuit Data Structures
# =============================================================================

@dataclass
class Signal:
    name: str
    msb: int = 0
    lsb: int = 0


@dataclass
class Assignment:
    lhs: str
    rhs: Any  # Expression
    line: int
    col: int


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: list = field(default_factory=list)

    def get_all_names(self) -> set:
        names = set()
        for sig in self.inputs:
            names.add(sig.name)
        for sig in self.outputs:
            names.add(sig.name)
        for sig in self.wires:
            names.add(sig.name)
        return names


# =============================================================================
# Expression AST
# =============================================================================

@dataclass
class Identifier:
    name: str


@dataclass
class Literal:
    value: str  # '0' or '1'


@dataclass
class Call:
    operator: str
    args: list


# =============================================================================
# Parser
# =============================================================================

class Parser:
    def __init__(self, content: str, filename: str):
        self.content = content
        self.filename = filename
        self.lines = content.split('\n')
        self.pos = 0
        self.line = 1
        self.col = 1

    def error(self, message: str, line: Optional[int] = None, col: Optional[int] = None) -> CircParseError:
        return CircParseError(message, file=self.filename, line=line or self.line, col=col or self.col)

    def parse(self) -> Circuit:
        circuit = Circuit()
        in_declarations = True

        line_num = 0
        for raw_line in self.lines:
            line_num += 1
            stripped = raw_line.strip()

            # Skip empty lines and comments
            if not stripped or stripped.startswith('#'):
                continue

            # Check for brackets (not supported in this part)
            if '[' in stripped or ']' in stripped:
                raise self.error("Arrays (brackets) are not supported in scalar .circ format", line_num, raw_line.find('[') if '[' in stripped else raw_line.find(']') + 1)

            # Check if it's a declaration or assignment
            if self._is_declaration(stripped):
                if not in_declarations:
                    raise DeclarationAfterAssignmentError(
                        f"Declaration after assignment is not allowed",
                        file=self.filename,
                        line=line_num,
                        col=1
                    )
                self._parse_declaration(stripped, circuit, line_num)
            elif '=' in stripped:
                in_declarations = False
                self._parse_assignment(stripped, circuit, line_num, raw_line)
            else:
                raise self.error(f"Invalid syntax: {stripped}", line_num, 1)

        return circuit

    def _is_declaration(self, line: str) -> bool:
        # A declaration line has a keyword at the start and NO '=' sign
        # This ensures "Input = t" is treated as an assignment, not declaration
        if '=' in line:
            return False
        parts = line.split()
        if not parts:
            return False
        keyword = parts[0].upper()
        return keyword in ('INPUT', 'OUTPUT', 'WIRE')

    def _parse_declaration(self, line: str, circuit: Circuit, line_num: int):
        parts = line.split()
        if not parts:
            return

        keyword = parts[0].upper()
        names = parts[1:]

        if keyword == 'INPUT':
            target = circuit.inputs
        elif keyword == 'OUTPUT':
            target = circuit.outputs
        elif keyword == 'WIRE':
            target = circuit.wires
        else:
            return

        existing_names = circuit.get_all_names()
        for name in names:
            if not self._is_valid_identifier(name):
                raise self.error(f"Invalid identifier: {name}", line_num, line.find(name) + 1)
            if name in existing_names:
                raise DuplicateNameError(
                    f"Duplicate name: {name}",
                    file=self.filename,
                    line=line_num,
                    col=line.find(name) + 1
                )
            target.append(Signal(name))
            existing_names.add(name)

    def _is_valid_identifier(self, name: str) -> bool:
        if not name:
            return False
        if name[0].isdigit():
            return False
        return all(c.isalnum() or c == '_' for c in name)

    def _parse_assignment(self, line: str, circuit: Circuit, line_num: int, raw_line: str):
        eq_pos = line.find('=')
        if eq_pos == -1:
            raise self.error(f"Invalid assignment syntax", line_num, 1)

        lhs = line[:eq_pos].strip()
        rhs = line[eq_pos + 1:].strip()

        if not lhs:
            raise self.error("Missing left-hand side in assignment", line_num, 1)

        if not self._is_valid_identifier(lhs):
            raise self.error(f"Invalid identifier on left-hand side: {lhs}", line_num, 1)

        # Find column of LHS in raw line
        lhs_col = raw_line.find(lhs) + 1

        # Parse RHS expression
        rhs_expr, _ = self._parse_expression(rhs, line_num, eq_pos + 2)

        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, lhs_col))

    def _parse_expression(self, text: str, line_num: int, start_col: int) -> tuple:
        text = text.strip()
        if not text:
            raise self.error("Empty expression", line_num, start_col)

        # Check for literal X (forbidden)
        if text == 'X':
            raise self.error("Literal 'X' values are not allowed in .circ files", line_num, start_col)

        # Check for literal 0 or 1
        if text == '0' or text == '1':
            return Literal(text), len(text)

        # Check for identifier (but not X as a standalone identifier if it looks like a literal context)
        if self._is_valid_identifier(text) and '(' not in text:
            # Check if it's the literal X
            if text == 'X':
                raise self.error("Literal 'X' values are not allowed in .circ files", line_num, start_col)
            return Identifier(text), len(text)

        # Check for function call
        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\(', text)
        if match:
            op_name = match.group(1).upper()
            op_start = match.end()

            # Find matching closing parenthesis
            paren_count = 1
            i = op_start
            while i < len(text) and paren_count > 0:
                if text[i] == '(':
                    paren_count += 1
                elif text[i] == ')':
                    paren_count -= 1
                i += 1

            if paren_count != 0:
                raise self.error(f"Unmatched parenthesis in expression", line_num, start_col)

            args_text = text[op_start:i-1]
            args = self._parse_arguments(args_text, line_num, start_col + op_start)

            return Call(op_name, args), i

        raise self.error(f"Invalid expression: {text}", line_num, start_col)

    def _parse_arguments(self, text: str, line_num: int, start_col: int) -> list:
        text = text.strip()
        if not text:
            return []

        args = []
        current = ""
        paren_depth = 0
        col = start_col

        for i, char in enumerate(text):
            if char == '(':
                paren_depth += 1
                current += char
            elif char == ')':
                paren_depth -= 1
                current += char
            elif char == ',' and paren_depth == 0:
                if current.strip():
                    expr, _ = self._parse_expression(current.strip(), line_num, col)
                    args.append(expr)
                current = ""
                col = start_col + i + 1
            else:
                current += char

        if current.strip():
            expr, _ = self._parse_expression(current.strip(), line_num, col)
            args.append(expr)

        return args


# =============================================================================
# Validator
# =============================================================================

class Validator:
    def __init__(self, circuit: Circuit, filename: str):
        self.circuit = circuit
        self.filename = filename

    def validate(self):
        self._check_assignments()
        self._check_all_assigned()
        self._check_cycles()

    def _get_signal_type(self, name: str) -> Optional[str]:
        for sig in self.circuit.inputs:
            if sig.name == name:
                return 'input'
        for sig in self.circuit.outputs:
            if sig.name == name:
                return 'output'
        for sig in self.circuit.wires:
            if sig.name == name:
                return 'wire'
        return None

    def _check_assignments(self):
        assigned = set()
        all_names = self.circuit.get_all_names()

        for assignment in self.circuit.assignments:
            lhs = assignment.lhs

            # Check if LHS is defined
            if lhs not in all_names:
                raise UndefinedNameError(
                    f"Undefined signal: {lhs}",
                    file=self.filename,
                    line=assignment.line,
                    col=assignment.col
                )

            # Check if LHS is an input
            signal_type = self._get_signal_type(lhs)
            if signal_type == 'input':
                raise InputAssignmentError(
                    f"Cannot assign to input: {lhs}",
                    file=self.filename,
                    line=assignment.line,
                    col=assignment.col
                )

            # Check for multiple assignment
            if lhs in assigned:
                raise MultipleAssignmentError(
                    f"Signal assigned multiple times: {lhs}",
                    file=self.filename,
                    line=assignment.line,
                    col=assignment.col
                )
            assigned.add(lhs)

            # Check RHS expressions
            self._check_expression(assignment.rhs, assignment.line)

    def _check_expression(self, expr, line_num: int):
        if isinstance(expr, Identifier):
            all_names = self.circuit.get_all_names()
            if expr.name not in all_names:
                raise UndefinedNameError(
                    f"Undefined signal: {expr.name}",
                    file=self.filename,
                    line=line_num,
                    col=None
                )
        elif isinstance(expr, Call):
            self._check_operator_arity(expr, line_num)
            for arg in expr.args:
                self._check_expression(arg, line_num)

    def _check_operator_arity(self, call: Call, line_num: int):
        op = call.operator
        num_args = len(call.args)

        # Operators with exactly 1 argument
        if op in ('NOT', 'BUF'):
            if num_args != 1:
                raise ArityError(
                    f"Operator {op} requires exactly 1 argument, got {num_args}",
                    file=self.filename,
                    line=line_num,
                    col=None
                )
        # Operators with >= 2 arguments
        elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            if num_args < 2:
                raise ArityError(
                    f"Operator {op} requires at least 2 arguments, got {num_args}",
                    file=self.filename,
                    line=line_num,
                    col=None
                )
        else:
            raise UndefinedNameError(
                f"Unknown operator: {op}",
                file=self.filename,
                line=line_num,
                col=None
            )

    def _check_all_assigned(self):
        assigned = {a.lhs for a in self.circuit.assignments}

        for sig in self.circuit.outputs:
            if sig.name not in assigned:
                raise UnassignedSignalError(
                    f"Output not assigned: {sig.name}",
                    file=self.filename,
                    line=None,
                    col=None
                )

        for sig in self.circuit.wires:
            if sig.name not in assigned:
                raise UnassignedSignalError(
                    f"Wire not assigned: {sig.name}",
                    file=self.filename,
                    line=None,
                    col=None
                )

    def _check_cycles(self):
        # Build dependency graph
        dependencies = {}
        all_signals = self.circuit.get_all_names()

        for sig in self.circuit.inputs:
            dependencies[sig.name] = set()

        for assignment in self.circuit.assignments:
            deps = self._get_dependencies(assignment.rhs)
            dependencies[assignment.lhs] = deps

        # Check for cycles using DFS
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in all_signals}

        def dfs(node: str, path: list) -> Optional[list]:
            color[node] = GRAY
            path.append(node)

            for dep in dependencies.get(node, set()):
                if color[dep] == GRAY:
                    # Found a cycle
                    cycle_start = path.index(dep)
                    return path[cycle_start:] + [dep]
                elif color[dep] == WHITE:
                    result = dfs(dep, path)
                    if result:
                        return result

            path.pop()
            color[node] = BLACK
            return None

        for node in all_signals:
            if color[node] == WHITE:
                cycle = dfs(node, [])
                if cycle:
                    cycle_str = ' -> '.join(cycle)
                    raise CycleError(
                        f"Cycle detected: {cycle_str}",
                        file=self.filename,
                        line=None,
                        col=None
                    )

    def _get_dependencies(self, expr) -> set:
        if isinstance(expr, Identifier):
            return {expr.name}
        elif isinstance(expr, Call):
            deps = set()
            for arg in expr.args:
                deps.update(self._get_dependencies(arg))
            return deps
        elif isinstance(expr, Literal):
            return set()
        return set()


# =============================================================================
# CLI Implementation
# =============================================================================

def output_json(data: dict):
    print(json.dumps(data, separators=(',', ':')))


def output_error(error: CircError, command: str, json_mode: bool) -> int:
    exit_code = EXIT_CLI_USAGE
    if isinstance(error, CircParseError):
        exit_code = EXIT_PARSE_ERROR
    elif isinstance(error, (DeclarationAfterAssignmentError, DuplicateNameError,
                            UndefinedNameError, UnassignedSignalError,
                            InputAssignmentError, MultipleAssignmentError,
                            ArityError, CycleError)):
        exit_code = EXIT_VALIDATION_ERROR

    if json_mode:
        output_json({
            "ok": False,
            "command": command,
            "exit_code": exit_code,
            "error": error.to_dict()
        })
    else:
        loc = ""
        if error.file:
            loc = f"{error.file}:"
            if error.line:
                loc += f"{error.line}:"
                if error.col:
                    loc += f"{error.col}:"
            loc += " "
        print(f"{loc}Error: {error.message}", file=sys.stderr)

    return exit_code


def check_command(args: list, json_mode: bool) -> int:
    if not args:
        return output_error(
            CliUsageError("Missing required argument: <file.circ>"),
            "__cli__",
            json_mode
        )

    filename = args[0]

    try:
        with open(filename, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        return output_error(
            FileNotFoundError_(f"File not found: {filename}", file=filename),
            "check",
            json_mode
        )
    except IOError as e:
        return output_error(
            FileNotFoundError_(f"Cannot read file: {filename}", file=filename),
            "check",
            json_mode
        )

    # Parse
    try:
        parser = Parser(content, filename)
        circuit = parser.parse()
    except CircError as e:
        return output_error(e, "check", json_mode)

    # Validate
    try:
        validator = Validator(circuit, filename)
        validator.validate()
    except CircError as e:
        return output_error(e, "check", json_mode)

    # Output result
    inputs = sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in circuit.inputs], key=lambda x: x['name'])
    outputs = sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in circuit.outputs], key=lambda x: x['name'])

    if json_mode:
        output_json({
            "ok": True,
            "command": "check",
            "format": "circ",
            "inputs": inputs,
            "outputs": outputs
        })
    else:
        print("Circuit is valid.")
        print(f"Inputs: {', '.join(s['name'] for s in inputs)}")
        print(f"Outputs: {', '.join(s['name'] for s in outputs)}")

    return EXIT_SUCCESS


def main():
    args = sys.argv[1:]

    # Check for --help
    if '--help' in args:
        print("""Usage: circopt.py [OPTIONS] <COMMAND> [ARGS]

Options:
  --help     Show this help message and exit
  --version  Show version and exit
  --json     Output in JSON format

Commands:
  check      Validate a .circ circuit file

Run 'circopt.py <COMMAND> --help' for more information on a command.""")
        return EXIT_SUCCESS

    # Check for --version
    if '--version' in args:
        json_mode = '--json' in args
        if json_mode:
            output_json({"ok": True, "command": "__version__", "version": VERSION})
        else:
            print(VERSION)
        return EXIT_SUCCESS

    # Check for --json
    json_mode = '--json' in args
    args = [a for a in args if a != '--json']

    # No command provided
    if not args:
        return output_error(
            CliUsageError("No command provided. Use --help for usage information."),
            "__cli__",
            json_mode
        )

    command = args[0]
    command_args = args[1:]

    if command == 'check':
        return check_command(command_args, json_mode)
    else:
        return output_error(
            CliUsageError(f"Unknown command: {command}"),
            "__cli__",
            json_mode
        )


if __name__ == '__main__':
    sys.exit(main())
