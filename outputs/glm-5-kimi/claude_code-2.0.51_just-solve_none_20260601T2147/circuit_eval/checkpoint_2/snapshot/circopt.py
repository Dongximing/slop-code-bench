#!/usr/bin/env python3
"""Circuit optimizer CLI tool."""

import sys
import json
import re
from dataclasses import dataclass, field
from typing import Any

VERSION = "1.0.0"

EXIT_SUCCESS = 0
EXIT_CLI_USAGE = 1
EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4
EXIT_NON_EQUIVALENCE = 10

EXIT_MISSING_INPUT = 1
EXIT_UNKNOWN_INPUT = 1
EXIT_INPUT_VALUE_PARSE = 2

VALIDATION_ERRORS = {
    "DeclarationAfterAssignmentError", "DuplicateNameError", "UndefinedNameError",
    "UnassignedSignalError", "InputAssignmentError", "MultipleAssignmentError",
    "ArityError", "CycleError"
}


class CircError(Exception):
    def __init__(self, error_type: str, message: str, file: str = None, line: int = None, col: int = None):
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message, "file": self.file, "line": self.line, "col": self.col}


class EvalError(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type
        self.message = message

    def to_dict(self) -> dict:
        return {"type": self.error_type, "message": self.message}


@dataclass
class Signal:
    name: str
    msb: int = 0
    lsb: int = 0


@dataclass
class Assignment:
    lhs: str
    rhs: Any
    line: int
    col: int


@dataclass
class Circuit:
    inputs: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    wires: list = field(default_factory=list)
    assignments: list = field(default_factory=list)

    def get_all_names(self) -> set:
        return {sig.name for sig in self.inputs + self.outputs + self.wires}

    def name_to_signal(self) -> dict:
        return {sig.name: sig for sig in self.inputs + self.outputs + self.wires}


@dataclass
class Identifier:
    name: str


@dataclass
class Literal:
    value: str


@dataclass
class Call:
    operator: str
    args: list


OPERATOR_ARITY = {
    'NOT': (1, 1), 'BUF': (1, 1),
    'AND': (2, None), 'OR': (2, None), 'XOR': (2, None),
    'NAND': (2, None), 'NOR': (2, None), 'XNOR': (2, None)
}


class Parser:
    def __init__(self, content: str, filename: str):
        self.filename = filename
        self.lines = content.split('\n')

    def parse(self) -> Circuit:
        circuit = Circuit()
        in_declarations = True

        for line_num, raw_line in enumerate(self.lines, 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith('#'):
                continue

            if '[' in stripped or ']' in stripped:
                bracket_pos = stripped.find('[') if '[' in stripped else stripped.find(']')
                raise CircError("CircParseError", "Arrays (brackets) are not supported in scalar .circ format",
                               self.filename, line_num, bracket_pos + 1)

            if self._is_declaration(stripped):
                if not in_declarations:
                    raise CircError("DeclarationAfterAssignmentError", "Declaration after assignment is not allowed",
                                   self.filename, line_num, 1)
                self._parse_declaration(stripped, circuit, line_num)
            elif '=' in stripped:
                in_declarations = False
                self._parse_assignment(stripped, circuit, line_num, raw_line)
            else:
                raise CircError("CircParseError", f"Invalid syntax: {stripped}", self.filename, line_num, 1)

        return circuit

    def _is_declaration(self, line: str) -> bool:
        if '=' in line:
            return False
        parts = line.split()
        return parts and parts[0].upper() in ('INPUT', 'OUTPUT', 'WIRE')

    def _parse_declaration(self, line: str, circuit: Circuit, line_num: int):
        parts = line.split()
        if not parts:
            return

        keyword = parts[0].upper()
        names = parts[1:]
        target_map = {'INPUT': circuit.inputs, 'OUTPUT': circuit.outputs, 'WIRE': circuit.wires}
        if keyword not in target_map:
            return
        target = target_map[keyword]

        existing_names = circuit.get_all_names()
        for name in names:
            if not self._is_valid_identifier(name):
                raise CircError("CircParseError", f"Invalid identifier: {name}", self.filename, line_num, line.find(name) + 1)
            if name in existing_names:
                raise CircError("DuplicateNameError", f"Duplicate name: {name}", self.filename, line_num, line.find(name) + 1)
            target.append(Signal(name))
            existing_names.add(name)

    def _is_valid_identifier(self, name: str) -> bool:
        return bool(name) and not name[0].isdigit() and all(c.isalnum() or c == '_' for c in name)

    def _parse_assignment(self, line: str, circuit: Circuit, line_num: int, raw_line: str):
        eq_pos = line.find('=')
        lhs = line[:eq_pos].strip()
        rhs = line[eq_pos + 1:].strip()

        if not lhs:
            raise CircError("CircParseError", "Missing left-hand side in assignment", self.filename, line_num, 1)
        if not self._is_valid_identifier(lhs):
            raise CircError("CircParseError", f"Invalid identifier on left-hand side: {lhs}", self.filename, line_num, 1)

        rhs_expr, _ = self._parse_expression(rhs, line_num, eq_pos + 2)
        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, raw_line.find(lhs) + 1))

    def _parse_expression(self, text: str, line_num: int, start_col: int) -> tuple:
        text = text.strip()
        if not text:
            raise CircError("CircParseError", "Empty expression", self.filename, line_num, start_col)
        if text == 'X':
            raise CircError("CircParseError", "Literal 'X' values are not allowed in .circ files", self.filename, line_num, start_col)
        if text in ('0', '1'):
            return Literal(text), len(text)
        if self._is_valid_identifier(text) and '(' not in text:
            return Identifier(text), len(text)

        match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\(', text)
        if match:
            op_name = match.group(1).upper()
            op_start = match.end()

            paren_count = 1
            i = op_start
            while i < len(text) and paren_count > 0:
                if text[i] == '(':
                    paren_count += 1
                elif text[i] == ')':
                    paren_count -= 1
                i += 1

            if paren_count != 0:
                raise CircError("CircParseError", "Unmatched parenthesis in expression", self.filename, line_num, start_col)

            args_text = text[op_start:i-1]
            args = self._parse_arguments(args_text, line_num, start_col + op_start)
            return Call(op_name, args), i

        raise CircError("CircParseError", f"Invalid expression: {text}", self.filename, line_num, start_col)

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


class Validator:
    def __init__(self, circuit: Circuit, filename: str):
        self.circuit = circuit
        self.filename = filename
        self._signal_type_map = None

    @property
    def signal_types(self) -> dict:
        if self._signal_type_map is None:
            self._signal_type_map = {}
            for sig in self.circuit.inputs:
                self._signal_type_map[sig.name] = 'input'
            for sig in self.circuit.outputs:
                self._signal_type_map[sig.name] = 'output'
            for sig in self.circuit.wires:
                self._signal_type_map[sig.name] = 'wire'
        return self._signal_type_map

    def validate(self):
        self._check_assignments()
        self._check_cycles()

    def _check_assignments(self):
        assigned = set()
        all_names = self.circuit.get_all_names()

        for asn in self.circuit.assignments:
            if asn.lhs not in all_names:
                raise CircError("UndefinedNameError", f"Undefined signal: {asn.lhs}", self.filename, asn.line, asn.col)
            if self.signal_types.get(asn.lhs) == 'input':
                raise CircError("InputAssignmentError", f"Cannot assign to input: {asn.lhs}", self.filename, asn.line, asn.col)
            if asn.lhs in assigned:
                raise CircError("MultipleAssignmentError", f"Signal assigned multiple times: {asn.lhs}", self.filename, asn.line, asn.col)
            assigned.add(asn.lhs)
            self._check_expression(asn.rhs, asn.line)

        # Check outputs and wires are assigned
        for sig in self.circuit.outputs + self.circuit.wires:
            if sig.name not in assigned:
                kind = "Output" if self.signal_types.get(sig.name) == 'output' else "Wire"
                raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", self.filename)

    def _check_expression(self, expr, line_num: int):
        if isinstance(expr, Identifier):
            if expr.name not in self.circuit.get_all_names():
                raise CircError("UndefinedNameError", f"Undefined signal: {expr.name}", self.filename, line_num)
        elif isinstance(expr, Call):
            self._check_operator_arity(expr, line_num)
            for arg in expr.args:
                self._check_expression(arg, line_num)

    def _check_operator_arity(self, call: Call, line_num: int):
        op = call.operator
        num_args = len(call.args)

        if op not in OPERATOR_ARITY:
            raise CircError("UndefinedNameError", f"Unknown operator: {op}", self.filename, line_num)

        min_args, max_args = OPERATOR_ARITY[op]
        if num_args < min_args or (max_args and num_args > max_args):
            req = f"exactly {min_args}" if max_args == min_args else f"at least {min_args}"
            raise CircError("ArityError", f"Operator {op} requires {req} argument{'s' if min_args > 1 else ''}, got {num_args}", self.filename, line_num)

    def _check_cycles(self):
        dependencies = {sig.name: set() for sig in self.circuit.inputs}
        for asn in self.circuit.assignments:
            dependencies[asn.lhs] = self._get_dependencies(asn.rhs)

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self.circuit.get_all_names()}

        def dfs(node: str, path: list):
            color[node] = GRAY
            path.append(node)
            for dep in dependencies.get(node, set()):
                if color[dep] == GRAY:
                    cycle_start = path.index(dep)
                    return path[cycle_start:] + [dep]
                if color[dep] == WHITE:
                    result = dfs(dep, path)
                    if result:
                        return result
            path.pop()
            color[node] = BLACK
            return None

        for node in self.circuit.get_all_names():
            if color[node] == WHITE:
                cycle = dfs(node, [])
                if cycle:
                    raise CircError("CycleError", f"Cycle detected: {' -> '.join(cycle)}", self.filename)

    def _get_dependencies(self, expr) -> set:
        if isinstance(expr, Identifier):
            return {expr.name}
        if isinstance(expr, Call):
            deps = set()
            for arg in expr.args:
                deps.update(self._get_dependencies(arg))
            return deps
        return set()


class Evaluator:
    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self.values = {}

    def evaluate(self, inputs: dict) -> dict:
        self.values = dict(inputs)

        input_names = {sig.name for sig in self.circuit.inputs}

        for asn in self.circuit.assignments:
            self.values[asn.lhs] = self._eval_expr(asn.rhs)

        return {sig.name: self.values[sig.name] for sig in self.circuit.outputs}

    def _eval_expr(self, expr) -> int:
        if isinstance(expr, Literal):
            return int(expr.value)
        if isinstance(expr, Identifier):
            return self.values[expr.name]
        if isinstance(expr, Call):
            args = [self._eval_expr(arg) for arg in expr.args]
            return self._apply_operator(expr.operator, args)
        raise RuntimeError(f"Unknown expression type: {type(expr)}")

    def _apply_operator(self, op: str, args: list) -> int:
        if op == 'NOT':
            return 1 - args[0]
        if op == 'BUF':
            return args[0]
        if op == 'AND':
            result = 1
            for a in args:
                result &= a
            return result
        if op == 'OR':
            result = 0
            for a in args:
                result |= a
            return result
        if op == 'XOR':
            result = 0
            for a in args:
                result ^= a
            return result
        if op == 'NAND':
            and_result = 1
            for a in args:
                and_result &= a
            return 1 - and_result
        if op == 'NOR':
            or_result = 0
            for a in args:
                or_result |= a
            return 1 - or_result
        if op == 'XNOR':
            xor_result = 0
            for a in args:
                xor_result ^= a
            return 1 - xor_result
        raise RuntimeError(f"Unknown operator: {op}")


def output_json(data: dict):
    print(json.dumps(data, separators=(',', ':')))


def output_error(error: CircError, command: str, json_mode: bool) -> int:
    if error.error_type == "CircParseError":
        exit_code = EXIT_PARSE_ERROR
    elif error.error_type in VALIDATION_ERRORS:
        exit_code = EXIT_VALIDATION_ERROR
    else:
        exit_code = EXIT_CLI_USAGE

    if json_mode:
        output_json({"ok": False, "command": command, "exit_code": exit_code, "error": error.to_dict()})
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


def output_eval_error(error: EvalError, command: str, json_mode: bool) -> int:
    if error.error_type == "MissingInputError":
        exit_code = EXIT_MISSING_INPUT
    elif error.error_type == "UnknownInputError":
        exit_code = EXIT_UNKNOWN_INPUT
    elif error.error_type == "InputValueParseError":
        exit_code = EXIT_INPUT_VALUE_PARSE
    else:
        exit_code = EXIT_CLI_USAGE

    if json_mode:
        output_json({"ok": False, "command": command, "exit_code": exit_code, "error": error.to_dict()})
    else:
        print(f"Error: {error.message}", file=sys.stderr)

    return exit_code


def check_command(args: list, json_mode: bool) -> int:
    if not args:
        return output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)

    filename = args[0]

    try:
        with open(filename, 'r') as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        return output_error(CircError("FileNotFoundError", f"Cannot read file: {filename}", filename), "check", json_mode)

    try:
        circuit = Parser(content, filename).parse()
        Validator(circuit, filename).validate()
    except CircError as e:
        return output_error(e, "check", json_mode)

    inputs = sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in circuit.inputs], key=lambda x: x['name'])
    outputs = sorted([{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in circuit.outputs], key=lambda x: x['name'])

    if json_mode:
        output_json({"ok": True, "command": "check", "format": "circ", "inputs": inputs, "outputs": outputs})
    else:
        print("Circuit is valid.")
        print(f"Inputs: {', '.join(s['name'] for s in inputs)}")
        print(f"Outputs: {', '.join(s['name'] for s in outputs)}")

    return EXIT_SUCCESS


def eval_command(args: list, json_mode: bool) -> int:
    if not args:
        return output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)

    filename = args[0]

    set_values = {}
    default_value = None
    allow_extra = False

    i = 1
    while i < len(args):
        arg = args[i]
        if arg == '--set':
            if i + 1 >= len(args):
                return output_error(CircError("CliUsageError", "--set requires a name=value argument"), "eval", json_mode)
            set_arg = args[i + 1]
            if '=' not in set_arg:
                return output_error(CircError("CliUsageError", f"Invalid --set format: {set_arg}, expected name=value"), "eval", json_mode)
            name, value = set_arg.split('=', 1)
            if value not in ('0', '1'):
                return output_eval_error(EvalError("InputValueParseError", f"Invalid input value for '{name}': '{value}', must be 0 or 1"), "eval", json_mode)
            set_values[name] = int(value)
            i += 2
        elif arg == '--default':
            if i + 1 >= len(args):
                return output_error(CircError("CliUsageError", "--default requires a value (0 or 1)"), "eval", json_mode)
            default_arg = args[i + 1]
            if default_arg not in ('0', '1'):
                return output_error(CircError("CliUsageError", f"Invalid --default value: {default_arg}, must be 0 or 1"), "eval", json_mode)
            default_value = int(default_arg)
            i += 2
        elif arg == '--allow-extra':
            allow_extra = True
            i += 1
        else:
            return output_error(CircError("CliUsageError", f"Unknown option: {arg}"), "eval", json_mode)

    try:
        with open(filename, 'r') as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        return output_error(CircError("FileNotFoundError", f"Cannot read file: {filename}", filename), "eval", json_mode)

    try:
        circuit = Parser(content, filename).parse()
        Validator(circuit, filename).validate()
    except CircError as e:
        return output_error(e, "eval", json_mode)

    input_names = {sig.name for sig in circuit.inputs}

    for name in set_values:
        if name not in input_names:
            if not allow_extra:
                return output_eval_error(EvalError("UnknownInputError", f"Unknown input: {name}"), "eval", json_mode)

    missing_inputs = input_names - set(set_values.keys())
    if missing_inputs and default_value is None:
        missing_list = sorted(missing_inputs)
        return output_eval_error(EvalError("MissingInputError", f"Missing input values for: {', '.join(missing_list)}"), "eval", json_mode)

    inputs = {}
    for sig in circuit.inputs:
        if sig.name in set_values:
            inputs[sig.name] = set_values[sig.name]
        elif default_value is not None:
            inputs[sig.name] = default_value

    try:
        evaluator = Evaluator(circuit)
        results = evaluator.evaluate(inputs)
    except Exception as e:
        return output_error(CircError("InternalError", str(e)), "eval", json_mode)

    sorted_outputs = sorted(results.items(), key=lambda x: x[0])

    if json_mode:
        output_list = []
        for name, value in sorted_outputs:
            output_list.append({"name": name, "msb": 0, "lsb": 0, "value": str(value)})
        output_json({"ok": True, "command": "eval", "mode": "2val", "radix": "bin", "outputs": output_list})
    else:
        for name, value in sorted_outputs:
            print(f"{name}={value}")

    return EXIT_SUCCESS


def main():
    args = sys.argv[1:]

    if '--help' in args:
        print("""Usage: circopt.py [OPTIONS] <COMMAND> [ARGS]

Options:
  --help     Show this help message and exit
  --version  Show version and exit
  --json     Output in JSON format

Commands:
  check      Validate a .circ circuit file
  eval       Evaluate a circuit with given inputs

Run 'circopt.py <COMMAND> --help' for more information on a command.""")
        return EXIT_SUCCESS

    if '--version' in args:
        json_mode = '--json' in args
        if json_mode:
            output_json({"ok": True, "command": "__version__", "version": VERSION})
        else:
            print(VERSION)
        return EXIT_SUCCESS

    json_mode = '--json' in args
    args = [a for a in args if a != '--json']

    if not args:
        return output_error(CircError("CliUsageError", "No command provided. Use --help for usage information."), "__cli__", json_mode)

    command, *command_args = args

    if command == 'check':
        return check_command(command_args, json_mode)
    if command == 'eval':
        return eval_command(command_args, json_mode)
    return output_error(CircError("CliUsageError", f"Unknown command: {command}"), "__cli__", json_mode)


if __name__ == '__main__':
    sys.exit(main())
