#!/usr/bin/env python3
"""Circuit optimizer and validator for .circ files."""

import argparse
import json
import sys
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

__version__ = "0.1.0"


# =============================================================================
# Error Classes
# =============================================================================

class CircError(Exception):
    """Base exception for all circuit-related errors."""

    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col

    def exit_code(self) -> int:
        raise NotImplementedError

    def error_type(self) -> str:
        raise NotImplementedError


class CliUsageError(CircError):
    """CLI usage error - exit code 1."""

    def exit_code(self) -> int:
        return 1

    def error_type(self) -> str:
        return "CliUsageError"


class CircParseError(CircError):
    """Parse error in circuit file - exit code 2."""

    def exit_code(self) -> int:
        return 2

    def error_type(self) -> str:
        return "CircParseError"


class DeclarationAfterAssignmentError(CircError):
    """Declaration after assignment - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "DeclarationAfterAssignmentError"


class DuplicateNameError(CircError):
    """Duplicate name definition - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "DuplicateNameError"


class UndefinedNameError(CircError):
    """Undefined name used - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "UndefinedNameError"


class UnassignedSignalError(CircError):
    """Signal never assigned - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "UnassignedSignalError"


class InputAssignmentError(CircError):
    """Trying to assign to input - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "InputAssignmentError"


class MultipleAssignmentError(CircError):
    """Signal assigned multiple times - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "MultipleAssignmentError"


class ArityError(CircError):
    """Wrong operator arity - exit code 3."""

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "ArityError"


class CycleError(CircError):
    """Cycle detected in assignment graph - exit code 3."""

    def __init__(self, message: str, cycle_path: List[str],
                 file: Optional[str] = None, line: Optional[int] = None,
                 col: Optional[int] = None):
        super().__init__(message, file, line, col)
        self.cycle_path = cycle_path

    def exit_code(self) -> int:
        return 3

    def error_type(self) -> str:
        return "CycleError"


# =============================================================================
# Parser
# =============================================================================

@dataclass
class Signal:
    """Represents a signal declaration."""
    name: str
    signal_type: str  # 'input', 'output', 'wire'


@dataclass
class Expr:
    """Base expression class."""


@dataclass
class Identifier(Expr):
    name: str


@dataclass
class Literal(Expr):
    value: bool  # True for 1, False for 0


@dataclass
class Call(Expr):
    op: str
    args: List[Expr]


@dataclass
class Assignment:
    """Represents an assignment statement."""
    lhs: str
    expr: Expr
    line_num: int


@dataclass
class Circuit:
    """Represents a parsed circuit."""
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    wires: List[str] = field(default_factory=list)
    assignments: List[Assignment] = field(default_factory=list)
    all_names: Dict[str, str] = field(default_factory=dict)  # name -> type


class Parser:
    """Parser for .circ files."""

    # Supported operators and their arities
    OPERATORS = {
        'NOT': 1,
        'BUF': 1,
        'AND': (2, float('inf')),
        'OR': (2, float('inf')),
        'XOR': (2, float('inf')),
        'NAND': (2, float('inf')),
        'NOR': (2, float('inf')),
        'XNOR': (2, float('inf')),
    }

    def __init__(self, file: str, content: str):
        self.file = file
        self.lines = content.split('\n')
        self.line_num = 0
        self.declaration_phase = True
        self.all_names: Dict[str, str] = {}  # name -> type ('input', 'output', 'wire')

    def parse(self) -> Circuit:
        """Parse the entire circuit file."""
        circuit = Circuit()

        for self.line_num in range(len(self.lines)):
            line = self.lines[self.line_num]

            # Strip comments and whitespace
            if '#' in line:
                line = line.split('#', 1)[0]
            line = line.strip()

            # Skip comments and empty lines
            if not line:
                continue

            # Check for declaration - only if still in declaration phase
            if self.declaration_phase:
                declared = self._parse_declaration(line, circuit)
                if declared:
                    continue

            # Check if this looks like a declaration but we're past declaration phase
            if not self.declaration_phase and self._is_declaration(line):
                raise DeclarationAfterAssignmentError(
                    f"Declaration '{line.split()[0]}' appears after assignments have begun",
                    self.file, self.line_num + 1, 1
                )

            # If not a declaration (or declaration phase ended), must be assignment
            assignment = self._parse_assignment(line)
            if assignment:
                circuit.assignments.append(assignment)
            else:
                # If we were still in declaration phase and this isn't valid, it's a parse error
                raise CircParseError(
                    f"Expected declaration or assignment, found: {line}",
                    self.file, self.line_num + 1, 1
                )

            # End declaration phase after first non-declaration
            if self.declaration_phase:
                self.declaration_phase = False

        # Copy all_names from parser to circuit
        circuit.all_names = self.all_names.copy()
        return circuit

    def _parse_declaration(self, line: str, circuit: Circuit) -> bool:
        """Parse a declaration line. Returns True if it was a declaration."""
        line_lower = line.lower()

        for decl_type in ['input', 'output', 'wire']:
            # Check for 'wire' without arguments (valid - declares no wires)
            if decl_type == 'wire' and line_lower == 'wire':
                return True
            if line_lower.startswith(decl_type + ' '):
                names = line[len(decl_type):].strip().split()
                for name in names:
                    if name in self.all_names:
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'",
                            self.file, self.line_num + 1,
                            line.find(name) + 1
                        )
                    self.all_names[name] = decl_type

                if decl_type == 'input':
                    circuit.inputs.extend(names)
                elif decl_type == 'output':
                    circuit.outputs.extend(names)
                else:  # wire
                    circuit.wires.extend(names)

                return True

        return False

    def _parse_assignment(self, line: str) -> Optional[Assignment]:
        """Parse an assignment line."""
        if '=' not in line:
            return None

        lhs_part, rhs_part = line.split('=', 1)
        lhs = lhs_part.strip()
        rhs = rhs_part.strip()

        # Remove trailing semicolon if present
        rhs = rhs.rstrip(';').strip()

        expr = self._parse_expression(rhs)
        return Assignment(lhs=lhs, expr=expr, line_num=self.line_num + 1)

    def _parse_expression(self, expr_str: str) -> Expr:
        """Parse an expression string into an Expr object."""
        expr_str = expr_str.strip()

        # Check for literal 0 or 1
        if expr_str == '0':
            return Literal(False)
        if expr_str == '1':
            return Literal(True)

        # Check for literal X (forbidden)
        if expr_str.upper() == 'X':
            raise CircParseError(
                f"Literal 'X' is forbidden",
                self.file, self.line_num + 1, 1
            )

        # Check for identifier
        if self._is_valid_identifier(expr_str):
            return Identifier(expr_str)

        # Check for function call
        if '(' in expr_str and expr_str.endswith(')'):
            return self._parse_call(expr_str)

        raise CircParseError(
            f"Invalid expression: {expr_str}",
            self.file, self.line_num + 1, 1
        )

    def _is_valid_identifier(self, s: str) -> bool:
        """Check if string is a valid identifier."""
        if not s:
            return False
        if s[0].isdigit():
            return False
        return all(c.isalnum() or c == '_' for c in s)

    def _is_declaration(self, line: str) -> bool:
        """Check if a line looks like a declaration."""
        line_lower = line.lower()
        for decl_type in ['input', 'output', 'wire']:
            if line_lower == decl_type or line_lower.startswith(decl_type + ' '):
                return True
        return False

    def _parse_call(self, call_str: str) -> Call:
        """Parse a function call like OP(e1, e2, ...)."""
        # Find the opening parenthesis
        paren_idx = call_str.find('(')
        if paren_idx == -1:
            raise CircParseError(
                f"Invalid call: missing '('",
                self.file, self.line_num + 1, 1
            )

        op_name = call_str[:paren_idx].upper()

        # Check if operator is supported
        if op_name not in self.OPERATORS:
            raise CircParseError(
                f"Unknown operator '{op_name}'",
                self.file, self.line_num + 1, 1
            )

        # Parse arguments
        args_str = call_str[paren_idx + 1:-1].strip()  # Remove outer parentheses

        if not args_str:
            args = []
        else:
            args = []
            current = ""
            paren_depth = 0

            for i, c in enumerate(args_str):
                if c == '(':
                    paren_depth += 1
                    current += c
                elif c == ')':
                    paren_depth -= 1
                    current += c
                elif c == ',' and paren_depth == 0:
                    arg_expr = self._parse_expression(current.strip())
                    args.append(arg_expr)
                    current = ""
                else:
                    current += c

            if current.strip():
                arg_expr = self._parse_expression(current.strip())
                args.append(arg_expr)

        # Check arity
        expected_arity = self.OPERATORS[op_name]
        if isinstance(expected_arity, int):
            if len(args) != expected_arity:
                raise ArityError(
                    f"Operator '{op_name}' expects {expected_arity} arguments, "
                    f"got {len(args)}",
                    self.file, self.line_num + 1, 1
                )
        else:
            min_args, max_args = expected_arity
            if len(args) < min_args:
                raise ArityError(
                    f"Operator '{op_name}' expects at least {min_args} arguments, "
                    f"got {len(args)}",
                    self.file, self.line_num + 1, 1
                )

        return Call(op=op_name, args=args)


# =============================================================================
# Validator
# =============================================================================

class Validator:
    """Validates a parsed circuit."""

    def __init__(self, circuit: Circuit, file: str):
        self.circuit = circuit
        self.file = file

    def validate(self) -> None:
        """Run all validation checks."""
        self._check_assignments()
        self._check_unassigned()
        self._check_cycles()

    def _check_assignments(self) -> None:
        """Check that all assignments are valid."""
        assigned_signals = {}  # signal -> (line_num, expr)

        for assignment in self.circuit.assignments:
            lhs = assignment.lhs

            # Check if LHS is declared
            if lhs not in self.circuit.all_names:
                if lhs in self.circuit.inputs:
                    raise InputAssignmentError(
                        f"Cannot assign to input '{lhs}'",
                        self.file, assignment.line_num, 1
                    )
                else:
                    raise UndefinedNameError(
                        f"Undefined signal '{lhs}'",
                        self.file, assignment.line_num, 1
                    )

            # Check for multiple assignment
            if lhs in assigned_signals:
                prev_line = assigned_signals[lhs][0]
                raise MultipleAssignmentError(
                    f"Signal '{lhs}' assigned multiple times "
                    f"(first at line {prev_line}, also at line {assignment.line_num})",
                    self.file, assignment.line_num, 1
                )

            assigned_signals[lhs] = (assignment.line_num, assignment.expr)

            # Check that all names in expression are defined
            self._check_expr_names(assignment.expr, assignment.line_num)

    def _check_expr_names(self, expr: Expr, line_num: int) -> None:
        """Check that all identifiers in expression are defined."""
        if isinstance(expr, Identifier):
            if expr.name not in self.circuit.all_names:
                raise UndefinedNameError(
                    f"Undefined signal '{expr.name}'",
                    self.file, line_num, 1
                )
        elif isinstance(expr, Call):
            for arg in expr.args:
                self._check_expr_names(arg, line_num)
        elif isinstance(expr, Literal):
            pass  # Literals are always valid

    def _check_unassigned(self) -> None:
        """Check that all outputs and wires are assigned."""
        assigned = {a.lhs for a in self.circuit.assignments}

        # Check wires
        for wire in self.circuit.wires:
            if wire not in assigned:
                raise UnassignedSignalError(
                    f"Wire '{wire}' is never assigned",
                    self.file, None, None
                )

        # Check outputs
        for output in self.circuit.outputs:
            if output not in assigned:
                raise UnassignedSignalError(
                    f"Output '{output}' is never assigned",
                    self.file, None, None
                )

    def _check_cycles(self) -> None:
        """Check for cycles in the assignment graph."""
        # Build dependency graph
        graph: Dict[str, List[str]] = {}

        for assignment in self.circuit.assignments:
            lhs = assignment.lhs
            graph[lhs] = self._get_dependencies(assignment.expr)

        # Check for cycles using DFS
        visited = set()
        recursion_stack = set()
        path = []

        def dfs(node: str) -> Optional[List[str]]:
            """DFS to find cycle. Returns cycle path if found."""
            visited.add(node)
            recursion_stack.add(node)
            path.append(node)

            for dep in graph.get(node, []):
                if dep not in visited:
                    result = dfs(dep)
                    if result:
                        return result
                elif dep in recursion_stack:
                    # Found a cycle
                    cycle_start = path.index(dep)
                    cycle_path = path[cycle_start:] + [dep]
                    return cycle_path

            path.pop()
            recursion_stack.remove(node)
            return None

        for node in list(graph.keys()):
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    cycle_str = ' -> '.join(cycle)
                    raise CycleError(
                        f"Cycle detected: {cycle_str}",
                        cycle,
                        self.file, None, None
                    )

    def _get_dependencies(self, expr: Expr) -> List[str]:
        """Get all signal dependencies of an expression."""
        deps = []

        if isinstance(expr, Identifier):
            deps.append(expr.name)
        elif isinstance(expr, Call):
            for arg in expr.args:
                deps.extend(self._get_dependencies(arg))

        return deps


# =============================================================================
# CLI
# =============================================================================

def output_json(ok: bool, command: str, exit_code: Optional[int] = None,
                error: Optional[Dict[str, Any]] = None, **extra) -> str:
    """Generate JSON output."""
    result = {"ok": ok, "command": command}

    if not ok:
        result["exit_code"] = exit_code
        result["error"] = error
    else:
        result.update(extra)

    return json.dumps(result)



def handle_check(args: argparse.Namespace) -> int:
    """Handle the 'check' command."""
    try:
        with open(args.file, 'r') as f:
            content = f.read()

        parser = Parser(args.file, content)
        circuit = parser.parse()

        validator = Validator(circuit, args.file)
        validator.validate()

        if args.json:
            result = output_json(
                True, "check",
                format="circ",
                inputs=[{"name": name, "msb": 0, "lsb": 0}
                        for name in sorted(circuit.inputs)],
                outputs=[{"name": name, "msb": 0, "lsb": 0}
                         for name in sorted(circuit.outputs)]
            )
            print(result)
        else:
            # Plain output - not required by spec but let's provide something
            print(f"Circuit '{args.file}' is valid.")
            print(f"Inputs: {', '.join(sorted(circuit.inputs))}")
            print(f"Outputs: {', '.join(sorted(circuit.outputs))}")
            print(f"Wires: {', '.join(sorted(circuit.wires))}")

        return 0

    except CircError as e:
        if args.json:
            error_info = {
                "type": e.error_type(),
                "message": e.message,
                "file": e.file,
                "line": e.line,
                "col": e.col
            }
            result = output_json(
                False, "check",
                exit_code=e.exit_code(),
                error=error_info
            )
            print(result)
        else:
            if e.file:
                print(f"Error: {e.message}", file=sys.stderr)
                if e.line is not None:
                    print(f"  at {e.file}:{e.line}", file=sys.stderr)
            else:
                print(f"Error: {e.message}", file=sys.stderr)

        return e.exit_code()


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    # First, check for --help or --version without needing command parsing
    json_mode = False
    args = argv or sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--help':
            # Show help - plain text (even if --json is present, spec says plain text)
            parser = argparse.ArgumentParser(
                prog='circopt',
                description='Circuit optimizer and validator for .circ files'
            )
            subparsers = parser.add_subparsers(dest='command')

            check_parser = subparsers.add_parser('check',
                                                 help='Validate a .circ file')
            check_parser.add_argument('file', help='Circuit file to check')
            check_parser.add_argument('--json', action='store_true')

            parser.print_help()
            return 0
        elif args[i] == '--version':
            # Check if --json appears anywhere
            if '--json' in args:
                result = output_json(True, "__version__", version=__version__)
                print(result)
            else:
                print(__version__)
            return 0
        elif args[i] == '--json':
            json_mode = True
        i += 1

    # Parse command
    # Skip past any global flags (though we only have --json as global)
    command_args = [a for a in args if a not in ['--json']]

    if not command_args:
        # No command provided
        if json_mode:
            result = output_json(
                False, "__cli__",
                exit_code=1,
                error={
                    "type": "CliUsageError",
                    "message": "No command specified",
                    "file": None,
                    "line": None,
                    "col": None
                }
            )
            print(result)
        else:
            print("Usage: circopt.py [OPTIONS] COMMAND ...", file=sys.stderr)
            print("Try 'circopt.py --help' for more information.", file=sys.stderr)
        return 1

    command = command_args[0]

    if command == 'check':
        # Parse check command arguments
        check_parser = argparse.ArgumentParser(prog='circopt check',
                                               add_help=False)
        check_parser.add_argument('file', help='Circuit file to check')
        check_parser.add_argument('--json', action='store_true')

        # Filter out global --json
        remaining = [a for a in args[1:] if a != '--json']

        check_args = check_parser.parse_args(remaining)
        # If --json is present after the command, override global json_mode
        check_args.json = json_mode or getattr(check_args, 'json', False)
        return handle_check(check_args)
    else:
        # Unknown command
        # Check if --json is present anywhere (global or after command)
        use_json = json_mode or '--json' in args
        if use_json:
            result = output_json(
                False, "__cli__",
                exit_code=1,
                error={
                    "type": "CliUsageError",
                    "message": f"Unknown command '{command}'",
                    "file": None,
                    "line": None,
                    "col": None
                }
            )
            print(result)
        else:
            print(f"circopt: '{command}' is not a circopt command.", file=sys.stderr)
            print("Try 'circopt.py --help' for more information.", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
