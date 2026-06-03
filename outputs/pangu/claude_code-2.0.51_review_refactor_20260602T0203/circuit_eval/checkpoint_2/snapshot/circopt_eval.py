#!/usr/bin/env python3
"""circopt - Circuit Optimizer CLI tool."""

import argparse
import json
import os
import sys
from typing import Optional, Any
from dataclasses import dataclass, field
from enum import Enum


# =============================================================================
# Version
# =============================================================================
VERSION = "0.1.0"


# =============================================================================
# Error classes and exit codes
# =============================================================================
class ExitCode(Enum):
    SUCCESS = 0
    CLI_USAGE_ERROR = 1
    PARSE_ERROR = 2
    VALIDATION_ERROR = 3
    INTERNAL_ERROR = 4
    NON_EQUIVALENCE = 10


class CircError(Exception):
    """Base exception for all circuit-related errors."""

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def error_type(self) -> str:
        return self.__class__.__name__


class CliUsageError(CircError):
    pass


class CircFileNotFoundError(CircError):
    pass


class CircParseError(CircError):
    pass


class DeclarationAfterAssignmentError(CircError):
    pass


class DuplicateNameError(CircError):
    pass


class UndefinedNameError(CircError):
    pass


class UnassignedSignalError(CircError):
    pass


class InputAssignmentError(CircError):
    pass


class MultipleAssignmentError(CircError):
    pass


class ArityError(CircError):
    pass


class CycleError(CircError):
    pass


class InternalError(CircError):
    pass


class MissingInputError(CircError):
    pass


class UnknownInputError(CircError):
    pass


class InputValueParseError(CircError):
    pass


# =============================================================================
# JSON output helpers
# =============================================================================
def json_output(ok: bool, command: str, exit_code: Optional[int] = None,
                **kwargs) -> str:
    """Generate JSON output envelope."""
    data = {"ok": ok, "command": command}
    if exit_code is not None:
        data["exit_code"] = exit_code
    data.update(kwargs)
    return json.dumps(data, separators=(',', ':'))


def json_error(error: CircError, command: str) -> str:
    """Generate error JSON output."""
    return json_output(
        ok=False,
        command=command,
        exit_code=get_exit_code_for_json(error),
        error={
            "type": error.error_type(),
            "message": error.message,
            "file": error.file,
            "line": error.line,
            "col": error.col
        }
    )


def map_exit_code(error: CircError) -> ExitCode:
    """Map error type to exit code."""
    mapping = {
        CliUsageError: ExitCode.CLI_USAGE_ERROR,
        CircFileNotFoundError: ExitCode.CLI_USAGE_ERROR,
        CircParseError: ExitCode.PARSE_ERROR,
        DeclarationAfterAssignmentError: ExitCode.VALIDATION_ERROR,
        DuplicateNameError: ExitCode.VALIDATION_ERROR,
        UndefinedNameError: ExitCode.VALIDATION_ERROR,
        UnassignedSignalError: ExitCode.VALIDATION_ERROR,
        InputAssignmentError: ExitCode.VALIDATION_ERROR,
        MultipleAssignmentError: ExitCode.VALIDATION_ERROR,
        ArityError: ExitCode.VALIDATION_ERROR,
        CycleError: ExitCode.VALIDATION_ERROR,
        MissingInputError: ExitCode.CLI_USAGE_ERROR,
        UnknownInputError: ExitCode.CLI_USAGE_ERROR,
        InputValueParseError: ExitCode.PARSE_ERROR,
    }
    return mapping.get(type(error), ExitCode.INTERNAL_ERROR)


def get_exit_code_for_json(error: CircError) -> int:
    """Get exit code for JSON error output."""
    return map_exit_code(error).value


# =============================================================================
# Parser
# =============================================================================
@dataclass
class Signal:
    name: str
    is_input: bool = False
    is_output: bool = False
    is_wire: bool = False
    assigned: bool = False


@dataclass
class Assignment:
    lhs: str
    op: str
    args: list
    lineno: int


@dataclass
class Circuit:
    inputs: dict  # name -> Signal
    outputs: dict  # name -> Signal
    wires: dict  # name -> Signal
    assignments: list  # list of Assignment

    def __init__(self):
        self.inputs = {}
        self.outputs = {}
        self.wires = {}
        self.assignments = []

    def get_signal(self, name: str) -> Optional[Signal]:
        if name in self.inputs:
            return self.inputs[name]
        if name in self.outputs:
            return self.outputs[name]
        if name in self.wires:
            return self.wires[name]
        return None

    def all_signals(self):
        return {**self.inputs, **self.outputs, **self.wires}


SUPPORTED_OPS = {
    "NOT": 1,
    "BUF": 1,
    "AND": ">=",
    "OR": ">=",
    "XOR": ">=",
    "NAND": ">=",
    "NOR": ">=",
    "XNOR": ">=",
}


def parse_circ(content: str, filename: str) -> Circuit:
    """Parse .circ file content into a Circuit object."""
    lines = content.splitlines()
    circuit = Circuit()
    seen_assignment = False

    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1

        # Trim whitespace
        stripped = line.strip()

        # Skip empty lines and comments
        if not stripped or stripped.startswith("#"):
            continue

        # Remove inline comments
        if "#" in stripped:
            # Find the first # that's not inside parentheses
            paren_depth = 0
            j = 0
            while j < len(stripped):
                if stripped[j] == '(':
                    paren_depth += 1
                elif stripped[j] == ')':
                    paren_depth -= 1
                elif stripped[j] == '#' and paren_depth == 0:
                    break
                j += 1
            stripped = stripped[:j].rstrip()

        if not stripped:
            continue

        # Check if this is an assignment
        if "=" in stripped:
            seen_assignment = True

            parts = stripped.split("=", 1)
            if len(parts) != 2:
                raise CircParseError(
                    "Invalid assignment syntax",
                    file=filename, line=i, col=stripped.find("=") + 1
                )

            lhs = parts[0].strip()
            expr = parts[1].strip()

            # Parse the expression FIRST, then validate
            if expr[0].isalpha() or expr[0] == "_":
                # Could be an identifier or a function call
                paren_idx = expr.find("(")
                if paren_idx == -1:
                    # Just an identifier
                    op = None
                    args = [expr]
                else:
                    # Function call - must end with )
                    if not expr.endswith(")"):
                        raise CircParseError(
                            "Missing closing parenthesis",
                            file=filename, line=i, col=len(lhs) + 2
                        )
                    op = expr[:paren_idx].upper()
                    if op not in SUPPORTED_OPS:
                        raise CircParseError(
                            f"Unknown operator '{op}'",
                            file=filename, line=i, col=len(lhs) + 2
                        )
                    args_part = expr[paren_idx + 1:-1].strip()
                    if not args_part:
                        raise CircParseError(
                            f"Operator '{op}' requires arguments",
                            file=filename, line=i, col=len(lhs) + 2 + paren_idx
                        )
                    args = [a.strip() for a in args_part.split(",")]
            else:
                # Literal or other
                if expr == "0" or expr == "1":
                    args = [expr]
                else:
                    raise CircParseError(
                        f"Invalid expression '{expr}'",
                        file=filename, line=i, col=len(lhs) + 2
                    )

            # Check for forbidden X literal
            if "X" in args:
                raise CircParseError(
                    "Literal X is forbidden",
                    file=filename, line=i, col=len(lhs) + 2
                )

            if op:
                # Check arity
                expected = SUPPORTED_OPS[op]
                if expected == ">=":
                    if len(args) < 2:
                        raise ArityError(
                            f"Operator '{op}' requires at least 2 arguments, got {len(args)}",
                            file=filename, line=i, col=len(lhs) + 2 + paren_idx
                        )
                else:
                    if len(args) != expected:
                        raise ArityError(
                            f"Operator '{op}' requires exactly {expected} arguments, got {len(args)}",
                            file=filename, line=i, col=len(lhs) + 2 + paren_idx
                        )

            circuit.assignments.append(Assignment(lhs=lhs, op=op, args=args,
                                                  lineno=i))
        else:
            # Declaration line
            if seen_assignment:
                raise DeclarationAfterAssignmentError(
                    "Declarations must appear before assignments",
                    file=filename, line=i, col=1
                )

            words = stripped.split()
            if not words:
                continue

            decl_type = words[0].lower()
            names = words[1:]

            if not names:
                continue

            if decl_type == "input":
                for name in names:
                    if name in circuit.all_signals():
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'",
                            file=filename, line=i, col=1
                        )
                    signal = Signal(name=name, is_input=True)
                    circuit.inputs[name] = signal

            elif decl_type == "output":
                for name in names:
                    if name in circuit.all_signals():
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'",
                            file=filename, line=i, col=1
                        )
                    signal = Signal(name=name, is_output=True)
                    circuit.outputs[name] = signal

            elif decl_type == "wire":
                for name in names:
                    if name in circuit.all_signals():
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'",
                            file=filename, line=i, col=1
                        )
                    signal = Signal(name=name, is_wire=True)
                    circuit.wires[name] = signal

            else:
                raise CircParseError(
                    f"Unknown declaration type '{decl_type}'",
                    file=filename, line=i, col=1
                )

    return circuit


# =============================================================================
# Validator
# =============================================================================
def validate_circuit(circuit: Circuit, filename: str) -> None:
    """Validate a parsed circuit."""
    # Track assignments
    assigned_signals = set()

    for assign in circuit.assignments:
        lhs = assign.lhs

        # Check LHS is declared
        signal = circuit.get_signal(lhs)
        if signal is None:
            raise UndefinedNameError(
                f"Undefined signal '{lhs}'",
                file=filename, line=assign.lineno
            )

        # Check LHS is not an input
        if signal.is_input:
            raise InputAssignmentError(
                f"Cannot assign to input '{lhs}'",
                file=filename, line=assign.lineno
            )

        # Check multiple assignment
        if lhs in assigned_signals:
            raise MultipleAssignmentError(
                f"Multiple assignment to '{lhs}'",
                file=filename, line=assign.lineno
            )
        assigned_signals.add(lhs)

        # Check args are defined
        for arg in assign.args:
            # Literals are always valid
            if arg in ("0", "1"):
                continue
            if circuit.get_signal(arg) is None:
                raise UndefinedNameError(
                    f"Undefined signal '{arg}'",
                    file=filename, line=assign.lineno
                )

    # Check all signals are assigned
    for signal in circuit.all_signals().values():
        if signal.is_input:
            continue
        if not signal.assigned and signal.name not in assigned_signals:
            raise UnassignedSignalError(
                f"Unassigned signal '{signal.name}'",
                file=filename
            )

    # Build dependency graph and check for cycles
    graph = {name: set() for name in circuit.all_signals() if not circuit.get_signal(name).is_input}
    for assign in circuit.assignments:
        for arg in assign.args:
            if arg in circuit.all_signals():
                graph[assign.lhs].add(arg)

    # Detect cycles using DFS
    visited = set()
    rec_stack = set()
    cycle_path = []

    def dfs(node: str) -> Optional[list]:
        visited.add(node)
        rec_stack.add(node)
        cycle_path.append(node)

        for dep in graph.get(node, []):
            if dep not in visited:
                result = dfs(dep)
                if result:
                    return result
            elif dep in rec_stack:
                # Found cycle, extract path
                start_idx = cycle_path.index(dep)
                return cycle_path[start_idx:] + [dep]

        cycle_path.pop()
        rec_stack.remove(node)
        return None

    for node in graph:
        if node not in visited:
            cycle = dfs(node)
            if cycle:
                cycle_msg = " -> ".join(cycle)
                raise CycleError(
                    f"Cycle detected: {cycle_msg}",
                    file=filename
                )


# =============================================================================
# 2-Valued Evaluator
# =============================================================================
def eval_circuit_2val(circuit: Circuit, inputs: dict[str, int],
                      default: int = 0, allow_extra: bool = False) -> dict[str, int]:
    """
    Evaluate circuit with 2-valued logic (0/1).

    Args:
        circuit: The parsed circuit
        inputs: Dict mapping input names to their values (0 or 1)
        default: Default value for unspecified inputs if allow_extra is True
        allow_extra: Whether to allow extra unspecified inputs

    Returns:
        Dict mapping output names to their evaluated values

    Raises:
        MissingInputError: A required input is not provided
        UnknownInputError: An unexpected input is provided (when allow_extra=False)
        InputValueParseError: An input value is not 0 or 1
    """
    # Validate input values
    for name, value in inputs.items():
        if value not in (0, 1):
            raise InputValueParseError(
                f"Input '{name}' value must be 0 or 1, got '{value}'"
            )

    # Check for unknown inputs (inputs not declared)
    if not allow_extra:
        for name in inputs:
            if name not in circuit.inputs:
                raise UnknownInputError(
                    f"Unknown input '{name}'"
                )

    # Check for missing inputs
    for input_name in circuit.inputs:
        if input_name not in inputs:
            raise MissingInputError(
                f"Missing input '{input_name}'"
            )

    # Initialize signal values
    values: dict[str, int] = {}

    # Set input values (and use default for extra if allowed)
    for name in circuit.inputs:
        values[name] = inputs.get(name, default)

    for name, value in inputs.items():
        if name not in circuit.inputs:
            values[name] = value

    # Define Boolean operations
    def evaluate_op(op: str, args: list) -> int:
        arg_values = [values[arg] for arg in args]

        if op == "NOT":
            return 1 - arg_values[0]
        elif op == "BUF":
            return arg_values[0]
        elif op == "AND":
            result = 1
            for v in arg_values:
                result &= v
            return result
        elif op == "OR":
            result = 0
            for v in arg_values:
                result |= v
            return result
        elif op == "XOR":
            result = 0
            for v in arg_values:
                result ^= v
            return result
        elif op == "NAND":
            result = 1
            for v in arg_values:
                result &= v
            return 1 - result
        elif op == "NOR":
            result = 0
            for v in arg_values:
                result |= v
            return 1 - result
        elif op == "XNOR":
            result = 0
            for v in arg_values:
                result ^= v
            return 1 - result
        else:
            raise InternalError(f"Unknown operator '{op}'")

    # Evaluate assignments in order (they are already in dependency order due to validation)
    for assign in circuit.assignments:
        values[assign.lhs] = evaluate_op(assign.op, assign.args)

    # Collect outputs
    outputs = {}
    for name in sorted(circuit.outputs.keys()):
        outputs[name] = values[name]

    return outputs


# =============================================================================
# CLI Commands
# =============================================================================
def cmd_version(args: argparse.Namespace) -> int:
    """Handle --version command."""
    if args.json:
        output = json_output(
            ok=True,
            command="__version__",
            version=VERSION
        )
        print(output)
    else:
        print(VERSION)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Handle 'check' command."""
    try:
        # Check if file exists first
        if not os.path.exists(args.file):
            raise CircFileNotFoundError(
                f"File '{args.file}' not found",
                file=args.file
            )

        # Read file
        with open(args.file, 'r') as f:
            content = f.read()

        # Parse
        circuit = parse_circ(content, args.file)

        # Validate
        validate_circuit(circuit, args.file)

        # Success output
        if args.json:
            inputs = sorted(
                [{"name": s.name, "msb": 0, "lsb": 0} for s in circuit.inputs.values()],
                key=lambda x: x["name"]
            )
            outputs = sorted(
                [{"name": s.name, "msb": 0, "lsb": 0} for s in circuit.outputs.values()],
                key=lambda x: x["name"]
            )
            output = json_output(
                ok=True,
                command="check",
                format="circ",
                inputs=inputs,
                outputs=outputs
            )
            print(output)
        else:
            print(f"Circuit: {args.file}")
            print(f"  Inputs: {', '.join(sorted(circuit.inputs.keys()))}")
            print(f"  Outputs: {', '.join(sorted(circuit.outputs.keys()))}")
            print(f"  Wires: {', '.join(sorted(circuit.wires.keys()))}")

        return 0

    except CircError as e:
        if args.json:
            print(json_error(e, "check"))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
            if e.file:
                print(f"  File: {e.file}", file=sys.stderr)
            if e.line:
                print(f"  Line: {e.line}", file=sys.stderr)
            if e.col:
                print(f"  Column: {e.col}", file=sys.stderr)
        return map_exit_code(e).value

    except Exception as e:
        # Internal error
        err = InternalError(f"Unexpected error: {e}", file=args.file if hasattr(args, 'file') else None)
        if args.json:
            print(json_error(err, "check"))
        else:
            print(f"Internal error: {e}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR.value


def cmd_eval(args: argparse.Namespace) -> int:
    """Handle 'eval' command."""
    try:
        # Check if file exists first
        if not os.path.exists(args.file):
            raise CircFileNotFoundError(
                f"File '{args.file}' not found",
                file=args.file
            )

        # Read file
        with open(args.file, 'r') as f:
            content = f.read()

        # Parse
        circuit = parse_circ(content, args.file)

        # Validate
        validate_circuit(circuit, args.file)

        # Parse --set arguments
        inputs = {}
        if args.set:
            for item in args.set:
                if '=' not in item:
                    raise InputValueParseError(
                        f"Invalid --set format: '{item}' (expected NAME=VALUE)"
                    )
                name, value_str = item.split('=', 1)
                name = name.strip()
                try:
                    value = int(value_str)
                except ValueError:
                    raise InputValueParseError(
                        f"Invalid value '{value_str}' for input '{name}' (expected 0 or 1)"
                    )
                inputs[name] = value

        # Evaluate circuit
        outputs = eval_circuit_2val(
            circuit,
            inputs,
            default=args.default,
            allow_extra=args.allow_extra
        )

        # Output results
        if args.json:
            output_list = [
                {"name": name, "msb": 0, "lsb": 0, "value": str(value)}
                for name, value in outputs.items()
            ]
            print(json_output(
                ok=True,
                command="eval",
                mode="2val",
                radix="bin",
                outputs=output_list
            ))
        else:
            for name, value in outputs.items():
                print(f"{name}={value}")

        return 0

    except CircError as e:
        if args.json:
            print(json_error(e, "eval"))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
            if e.file:
                print(f"  File: {e.file}", file=sys.stderr)
            if e.line:
                print(f"  Line: {e.line}", file=sys.stderr)
            if e.col:
                print(f"  Column: {e.col}", file=sys.stderr)
        return map_exit_code(e).value

    except Exception as e:
        # Internal error
        err = InternalError(f"Unexpected error: {e}", file=args.file if hasattr(args, 'file') else None)
        if args.json:
            print(json_error(err, "eval"))
        else:
            print(f"Internal error: {e}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR.value


# =============================================================================
# Main CLI
# =============================================================================
def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog="circopt",
        description="Circuit optimizer tool",
        add_help=False
    )

    # Global flags
    parser.add_argument("--version", action="store_true",
                        help="Print version and exit")
    parser.add_argument("--help", action="store_true",
                        help="Print help and exit")
    parser.add_argument("--json", action="store_true",
                        help="Output in JSON format")

    # Subcommands
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # check command
    check_parser = subparsers.add_parser(
        "check",
        help="Check a .circ file for validity",
        add_help=False
    )
    check_parser.add_argument("file", help=".circ file to check")
    check_parser.add_argument("--json", action="store_true",
                              help="Output in JSON format")
    check_parser.add_argument("--help", action="store_true",
                              help="Show help for check command")

    # eval command
    eval_parser = subparsers.add_parser(
        "eval",
        help="Evaluate a circuit with given inputs",
        add_help=False
    )
    eval_parser.add_argument("file", help=".circ file to evaluate")
    eval_parser.add_argument("--set", dest="set", action="append", metavar="NAME=VALUE",
                             help="Set input value (can be used multiple times)")
    eval_parser.add_argument("--default", type=int, choices=[0, 1], default=0,
                             help="Default value for unspecified inputs (0 or 1)")
    eval_parser.add_argument("--allow-extra", action="store_true",
                             help="Allow extra inputs not declared in circuit")
    eval_parser.add_argument("--json", action="store_true",
                             help="Output in JSON format")
    eval_parser.add_argument("--help", action="store_true",
                             help="Show help for eval command")

    return parser


def print_help(parser: argparse.ArgumentParser, json_output_flag: bool) -> None:
    """Print help text."""
    if json_output_flag:
        # JSON help is still plain text per spec
        pass
    parser.print_help()


def check_command(json_flag: bool, file_arg: Optional[str], argv: list) -> int:
    """Handle the check command with proper argument parsing."""
    if file_arg is None:
        if json_flag:
            print(json_error(
                CliUsageError("Missing file argument for check command"),
                "__cli__"
            ))
        else:
            print("Error: Missing file argument for check command", file=sys.stderr)
        return ExitCode.CLI_USAGE_ERROR.value

    class CheckArgs:
        def __init__(self, file, json):
            self.file = file
            self.json = json

    check_args = CheckArgs(file=file_arg, json=json_flag)
    return cmd_check(check_args)


def main() -> int:
    """Main entry point."""
    # First pass: detect global flags before command
    global_json = False
    global_help = False
    global_version = False
    command = None
    command_file = None
    command_json = False
    command_help = False

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]

        if arg == "--json":
            global_json = True
        elif arg == "--help":
            global_help = True
        elif arg == "--version":
            global_version = True
        elif arg == "check" and command is None:
            command = "check"
        elif arg == "eval" and command is None:
            command = "eval"
        elif command == "check" and command_file is None and not arg.startswith("-"):
            command_file = arg
        elif command == "eval" and command_file is None and not arg.startswith("-"):
            command_file = arg
        elif command == "check" and arg == "--json":
            command_json = True
        elif command == "check" and arg == "--help":
            command_help = True
        elif command == "eval" and arg == "--json":
            command_json = True
        elif command == "eval" and arg == "--help":
            command_help = True
        elif command is None:
            # Unknown command
            if global_json:
                print(json_error(
                    CliUsageError(f"Unknown command: {arg}"),
                    "__cli__"
                ))
            else:
                print(f"Error: Unknown command '{arg}'", file=sys.stderr)
            return ExitCode.CLI_USAGE_ERROR.value
        elif arg.startswith("-"):
            # Unknown flag
            if global_json:
                print(json_error(
                    CliUsageError(f"Unknown flag: {arg}"),
                    "__cli__"
                ))
            else:
                print(f"Error: Unknown flag: {arg}", file=sys.stderr)
            return ExitCode.CLI_USAGE_ERROR.value

        i += 1

    # Handle global --help
    if global_help:
        if global_json:
            # JSON help is still plain text per spec
            pass
        print("usage: circopt [--version] [--help] [--json] <command> ...")
        print("")
        print("Circuit optimizer tool")
        print("")
        print("positional arguments:")
        print("  <command>")
        print("    check    Check a .circ file for validity")
        print("    eval     Evaluate a circuit with given inputs")
        print("")
        print("options:")
        print("  --version  Print version and exit")
        print("  --help     Print help and exit")
        print("  --json     Output in JSON format")
        return 0

    # Handle global --version
    if global_version:
        class Args:
            def __init__(self, json):
                self.json = json
        return cmd_version(Args(json=global_json))

    # Handle check command
    if command == "check":
        if command_help:
            print("Usage: circopt check <file.circ> [--json]")
            print("")
            print("Check a .circ file for validity")
            print("")
            print("Positional arguments:")
            print("  file     .circ file to check")
            print("")
            print("Options:")
            print("  --json   Output in JSON format")
            print("  --help   Show this message and exit")
            return 0

        return check_command(global_json or command_json, command_file, sys.argv)

    # Handle eval command
    if command == "eval":
        if command_help:
            print("Usage: circopt eval <file.circ> [--set name=value ...] [--default 0|1] [--allow-extra] [--json]")
            print("")
            print("Evaluate a circuit with given inputs. Inputs must be 0 or 1.")
            print("")
            print("Positional arguments:")
            print("  file     .circ file to evaluate")
            print("")
            print("Options:")
            print("  --set NAME=VALUE   Set input value (can be used multiple times)")
            print("  --default 0|1      Default value for unspecified inputs (0 or 1)")
            print("  --allow-extra      Allow extra inputs not declared in circuit")
            print("  --json             Output in JSON format")
            print("  --help             Show this message and exit")
            return 0

        # Parse eval command arguments manually
        eval_args = {
            'file': command_file,
            'set': [],
            'default': 0,
            'allow_extra': False,
            'json': global_json or command_json
        }

        i = 1
        while i < len(sys.argv):
            arg = sys.argv[i]

            if arg == "eval":
                pass
            elif arg == "--set" and i + 1 < len(sys.argv):
                eval_args['set'].append(sys.argv[i + 1])
                i += 1
            elif arg == "--default" and i + 1 < len(sys.argv):
                eval_args['default'] = int(sys.argv[i + 1])
                i += 1
            elif arg == "--allow-extra":
                eval_args['allow_extra'] = True
            elif arg == "--json":
                pass
            elif arg == "--help":
                pass
            elif arg == command_file:
                pass

            i += 1

        class EvalArgs:
            def __init__(self, file, set_list, default, allow_extra, json):
                self.file = file
                self.set = set_list if set_list else None
                self.default = default
                self.allow_extra = allow_extra
                self.json = json

        eval_args_obj = EvalArgs(
            file=eval_args['file'],
            set_list=eval_args['set'],
            default=eval_args['default'],
            allow_extra=eval_args['allow_extra'],
            json=eval_args['json']
        )

        return cmd_eval(eval_args_obj)

    # No command specified
    if global_json:
        print(json_error(
            CliUsageError("No command specified"),
            "__cli__"
        ))
    else:
        print("Error: No command specified", file=sys.stderr)
        print("Try 'circopt --help' for more information.", file=sys.stderr)
    return ExitCode.CLI_USAGE_ERROR.value


if __name__ == "__main__":
    sys.exit(main())
