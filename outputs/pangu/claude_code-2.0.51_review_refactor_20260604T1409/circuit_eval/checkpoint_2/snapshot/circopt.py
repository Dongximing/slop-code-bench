#!/usr/bin/env python3
"""
Circuit optimizer CLI - Part 1: check command for scalar .circ files
"""

import sys
import re
import json
from typing import Optional, List, Dict, Any, Tuple, Set


VERSION = "0.1.0"


# =============================================================================
# Error Classes
# =============================================================================

class CircuitError(Exception):
    """Base error for circuit-related errors."""
    def __init__(self, message: str, file: Optional[str] = None,
                 line: Optional[int] = None, col: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.file = file
        self.line = line
        self.col = col


class CliUsageError(CircuitError):
    exit_code = 1


class FileNotFoundError(CircuitError):
    exit_code = 1

    def __init__(self, message: str, filepath: str):
        super().__init__(message, file=filepath)


class CircParseError(CircuitError):
    exit_code = 2


class DeclarationAfterAssignmentError(CircuitError):
    exit_code = 3


class DuplicateNameError(CircuitError):
    exit_code = 3


class UndefinedNameError(CircuitError):
    exit_code = 3


class UnassignedSignalError(CircuitError):
    exit_code = 3


class InputAssignmentError(CircuitError):
    exit_code = 3


class MultipleAssignmentError(CircuitError):
    exit_code = 3


class MissingInputError(CircuitError):
    exit_code = 1


class UnknownInputError(CircuitError):
    exit_code = 1


class InputValueParseError(CircuitError):
    exit_code = 2


class ArityError(CircuitError):
    exit_code = 3


class CycleError(CircuitError):
    exit_code = 3


# =============================================================================
# Parser
# =============================================================================

def tokenize_line(line: str, line_num: int) -> List[Tuple[str, int]]:
    """Tokenize a single line.

    Returns list of (token_type, token_value) tuples.
    The column position is the actual column (1-based) in the source file.
    """
    tokens = []
    pos = 0
    length = len(line)

    # Operator patterns (case-insensitive)
    operators = {'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'NOT', 'BUF'}

    while pos < length:
        col_pos = pos + 1  # 1-based column

        # Skip whitespace
        if line[pos] in ' \t':
            pos += 1
            continue

        # Comment
        if line[pos] == '#':
            break  # Rest of line is comment, ignore

        # Identifiers (letters, underscores, digits after first char)
        if re.match(r'[a-zA-Z_]', line[pos:]):
            match = re.match(r'[a-zA-Z_][a-zA-Z0-9_]*', line[pos:])
            if match:
                token = match.group()
                upper_token = token.upper()
                if upper_token in operators:
                    tokens.append(('OP', upper_token, col_pos))
                elif upper_token in {'INPUT', 'OUTPUT', 'WIRE'}:
                    tokens.append(('KW', upper_token, col_pos))
                else:
                    tokens.append(('ID', token, col_pos))
                pos += len(token)
                continue

        # Literals: 0, 1, X
        if line[pos] in '01X':
            if line[pos] == 'X':
                raise CircParseError(
                    f"Literal X is forbidden",
                    file=None,
                    line=line_num,
                    col=col_pos
                )
            tokens.append(('LITERAL', line[pos], col_pos))
            pos += 1
            continue

        # Parentheses and comma
        if line[pos] in '(),':
            tokens.append((line[pos], line[pos], col_pos))
            pos += 1
            continue

        # Equals sign
        if line[pos] == '=':
            tokens.append(('EQ', '=', col_pos))
            pos += 1
            continue

        # Unknown character
        raise CircParseError(
            f"Unexpected character: {line[pos]}",
            file=None,
            line=line_num,
            col=col_pos
        )

    return tokens


def parse_circ_file(filepath: str) -> Tuple[List[Tuple], List[Tuple]]:
    """Parse a .circ file and return (declarations, assignments).

    Returns:
        declarations: List of (type, [names]) tuples
        assignments: List of (lhs, (op, [args])) tuples
    """
    with open(filepath, 'r') as f:
        lines = f.readlines()

    declarations = []
    assignments = []
    seen_assignment = False

    for line_num, line in enumerate(lines, 1):
        line = line.rstrip('\n\r')

        # Skip empty lines and comments
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#'):
            continue

        try:
            tokens = tokenize_line(line, line_num)
        except CircParseError as e:
            if e.file is None:
                e.file = filepath
            raise

        if not tokens:
            continue

        # Check if it's a declaration
        first_token_type = tokens[0][0]
        first_token_value = tokens[0][1]

        if first_token_type == 'KW' and first_token_value in {'INPUT', 'OUTPUT', 'WIRE'}:
            # Declaration line
            if seen_assignment:
                raise DeclarationAfterAssignmentError(
                    f"Declaration found after assignment",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            decl_type = first_token_value
            names = []

            for tok_type, tok_val, _ in tokens[1:]:
                if tok_type == 'ID':
                    names.append(tok_val)

            if not names:
                if decl_type != 'WIRE':
                    raise CircParseError(
                        f"Expected identifier after {decl_type}",
                        file=filepath,
                        line=line_num,
                        col=1
                    )
                # Empty wire declaration is allowed

            declarations.append((decl_type, names))

        elif first_token_type == 'ID':
            # Assignment line
            seen_assignment = True

            lhs = first_token_value

            # Parse rest of line: = OP( ... )
            pos = 1
            if pos >= len(tokens):
                raise CircParseError(
                    f"Missing '=' in assignment",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            if tokens[pos][0] != 'EQ':
                raise CircParseError(
                    f"Expected '=' after '{lhs}'",
                    file=filepath,
                    line=line_num,
                    col=tokens[pos][2]
                )
            pos += 1

            if pos >= len(tokens):
                raise CircParseError(
                    f"Missing expression after '='",
                    file=filepath,
                    line=line_num,
                    col=1
                )

            # Parse operator and arguments
            if tokens[pos][0] != 'OP':
                raise CircParseError(
                    f"Expected operator",
                    file=filepath,
                    line=line_num,
                    col=tokens[pos][2]
                )

            op = tokens[pos][1]
            pos += 1

            if pos >= len(tokens) or tokens[pos][0] != '(':
                raise CircParseError(
                    f"Expected '(' after operator",
                    file=filepath,
                    line=line_num,
                    col=tokens[pos][2] if pos < len(tokens) else 1
                )
            pos += 1

            # Parse arguments
            closed = False
            args = []
            while pos < len(tokens):
                if tokens[pos][0] in ('ID', 'LITERAL'):
                    args.append(tokens[pos][1])
                    pos += 1
                elif tokens[pos][0] == ',':
                    pos += 1
                elif tokens[pos][0] == ')':
                    pos += 1
                    closed = True
                    break
                else:
                    raise CircParseError(
                        f"Unexpected token in expression",
                        file=filepath,
                        line=line_num,
                        col=tokens[pos][2]
                    )

            # Check that parentheses are closed
            if not closed:
                raise CircParseError(
                    f"Missing closing parenthesis",
                    file=filepath,
                    line=line_num,
                    col=tokens[-1][2] if tokens else 1
                )

            # Check for leftover tokens (non-whitespace, non-comment)
            while pos < len(tokens):
                raise CircParseError(
                    f"Unexpected token: {tokens[pos][1]}",
                    file=filepath,
                    line=line_num,
                    col=tokens[pos][2]
                )

            assignments.append((lhs, (op, args)))

        else:
            raise CircParseError(
                f"Expected keyword (input/output/wire) or identifier",
                file=filepath,
                line=line_num,
                col=1
            )

    return declarations, assignments


def validate_circ_file(filepath: str, declarations: List[Tuple],
                       assignments: List[Tuple]) -> Dict[str, Any]:
    """Validate declarations and assignments."""

    # Track all declared signals
    inputs: Set[str] = set()
    outputs: Set[str] = set()
    wires: Set[str] = set()
    all_signals: Dict[str, str] = {}  # name -> type ('input', 'output', 'wire')

    # Operator arities
    op_arity = {
        'NOT': 1,
        'BUF': 1,
        'AND': 2,  # minimum
        'OR': 2,
        'XOR': 2,
        'NAND': 2,
        'NOR': 2,
        'XNOR': 2,
    }

    # Process declarations
    for decl_type, names in declarations:
        decl_type_lower = decl_type.lower()
        for name in names:
            if name in all_signals:
                raise DuplicateNameError(
                    f"Duplicate signal name: {name}",
                    file=filepath
                )
            all_signals[name] = decl_type_lower
            if decl_type_lower == 'input':
                inputs.add(name)
            elif decl_type_lower == 'output':
                outputs.add(name)
            elif decl_type_lower == 'wire':
                wires.add(name)

    # Track assignments
    assigned: Dict[str, Tuple[str, int]] = {}  # name -> (op, line_num)

    # Build dependency graph for cycle detection
    graph: Dict[str, List[str]] = {}

    for i, (lhs, (op, args)) in enumerate(assignments):
        line_num = i + 1

        # Check LHS is valid
        if lhs not in all_signals:
            raise UndefinedNameError(
                f"Undefined signal: {lhs}",
                file=filepath
            )

        signal_type = all_signals[lhs]
        if signal_type == 'input':
            raise InputAssignmentError(
                f"Cannot assign to input: {lhs}",
                file=filepath
            )

        # Check for multiple assignment
        if lhs in assigned:
            raise MultipleAssignmentError(
                f"Multiple assignment to: {lhs}",
                file=filepath
            )

        assigned[lhs] = (op, line_num)

        # Check operand arity
        op_upper = op.upper()
        if op_upper in op_arity:
            min_arity = op_arity[op_upper]
            if len(args) < min_arity:
                raise ArityError(
                    f"Operator {op} requires at least {min_arity} operands, got {len(args)}",
                    file=filepath
                )
        else:
            raise ArityError(
                f"Unknown operator: {op}",
                file=filepath
            )

        # Check all operands are defined
        graph[lhs] = []
        for arg in args:
            if arg in all_signals:
                graph[lhs].append(arg)
            elif arg in ('0', '1'):
                pass  # literals are fine
            else:
                raise UndefinedNameError(
                    f"Undefined signal: {arg}",
                    file=filepath
                )

    # Check for unassigned signals (only wires and outputs)
    for name, sig_type in all_signals.items():
        if sig_type in ('wire', 'output'):
            if name not in assigned:
                raise UnassignedSignalError(
                    f"Unassigned signal: {name}",
                    file=filepath
                )

    # Check for cycles
    def find_cycle() -> Optional[List[str]]:
        """Find a cycle in the assignment graph."""
        visited: Set[str] = set()
        rec_stack: List[str] = []
        path: Dict[str, List[str]] = {}

        def dfs(node: str) -> Optional[List[str]]:
            visited.add(node)
            rec_stack.append(node)

            for neighbor in graph.get(node, []):
                if neighbor not in visited:
                    result = dfs(neighbor)
                    if result:
                        return result
                elif neighbor in rec_stack:
                    # Found a cycle
                    start_idx = rec_stack.index(neighbor)
                    return rec_stack[start_idx:] + [node]

            rec_stack.pop()
            return None

        for node in graph:
            if node not in visited:
                cycle = dfs(node)
                if cycle:
                    return cycle
        return None

    cycle = find_cycle()
    if cycle:
        cycle_path = " -> ".join(cycle)
        raise CycleError(
            f"Cycle detected: {cycle_path}",
            file=filepath
        )

    # Return success info
    sorted_inputs = sorted(inputs)
    sorted_outputs = sorted(outputs)

    return {
        'inputs': [{'name': n, 'msb': 0, 'lsb': 0} for n in sorted_inputs],
        'outputs': [{'name': n, 'msb': 0, 'lsb': 0} for n in sorted_outputs],
    }


# =============================================================================
# CLI
# =============================================================================

def format_error_json(error: CircuitError, command: str) -> str:
    """Format error as JSON envelope."""
    error_type = type(error).__name__
    result = {
        'ok': False,
        'command': command,
        'exit_code': error.exit_code,
        'error': {
            'type': error_type,
            'message': error.message,
            'file': error.file,
            'line': error.line,
            'col': error.col,
        }
    }
    return json.dumps(result)


def format_version_json() -> str:
    """Format version output as JSON."""
    return json.dumps({
        'ok': True,
        'command': '__version__',
        'version': VERSION,
    })


def cmd_check(filepath: str, json_output: bool) -> int:
    """Execute check command."""
    # Check file exists first
    import os
    if not os.path.exists(filepath):
        if json_output:
            print(format_error_json(
                FileNotFoundError(f"File not found: {filepath}", filepath),
                'check'
            ))
        else:
            print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    try:
        declarations, assignments = parse_circ_file(filepath)
        result = validate_circ_file(filepath, declarations, assignments)

        if json_output:
            output = {
                'ok': True,
                'command': 'check',
                'format': 'circ',
                'inputs': result['inputs'],
                'outputs': result['outputs'],
            }
            print(json.dumps(output))
        else:
            print(f"Inputs: {', '.join(i['name'] for i in result['inputs'])}")
            print(f"Outputs: {', '.join(o['name'] for o in result['outputs'])}")

        return 0

    except CircuitError as e:
        if json_output:
            print(format_error_json(e, 'check'))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return e.exit_code


# =============================================================================
# 2-Valued Evaluation
# =============================================================================

def evaluate_operator(op: str, args: List[str], values: Dict[str, int]) -> int:
    """Evaluate an operator with given argument values.

    Args:
        op: Operator name (NOT, BUF, AND, OR, XOR, NAND, NOR, XNOR)
        args: List of argument names or literal values
        values: Dictionary mapping signal names to their integer values (0 or 1)

    Returns:
        The result as an integer (0 or 1)
    """
    # Get operand values
    operand_values = []
    for arg in args:
        if arg in ('0', '1'):
            operand_values.append(int(arg))
        else:
            operand_values.append(values[arg])

    # Apply operator
    op_upper = op.upper()

    if op_upper == 'NOT':
        return 1 - operand_values[0]
    elif op_upper == 'BUF':
        return operand_values[0]
    elif op_upper == 'AND':
        result = 1
        for v in operand_values:
            result &= v
        return result
    elif op_upper == 'OR':
        result = 0
        for v in operand_values:
            result |= v
        return result
    elif op_upper == 'XOR':
        result = 0
        for v in operand_values:
            result ^= v
        return result
    elif op_upper == 'NAND':
        result = 1
        for v in operand_values:
            result &= v
        return 1 - result
    elif op_upper == 'NOR':
        result = 0
        for v in operand_values:
            result |= v
        return 1 - result
    elif op_upper == 'XNOR':
        result = 0
        for v in operand_values:
            result ^= v
        return 1 - result
    else:
        raise ValueError(f"Unknown operator: {op}")


def cmd_eval(filepath: str, set_inputs: List[str], default: Optional[int],
             allow_extra: bool, json_output: bool) -> int:
    """Execute eval command.

    Args:
        filepath: Path to .circ file
        set_inputs: List of "name=value" strings for input assignments
        default: Default value for unspecified inputs (None means error if missing)
        allow_extra: Whether to allow extra inputs not declared in the file
        json_output: Whether to output in JSON format

    Returns:
        Exit code (0 for success)
    """
    import os

    # Check file exists
    if not os.path.exists(filepath):
        if json_output:
            print(json.dumps({
                'ok': False,
                'command': 'eval',
                'exit_code': 1,
                'error': {
                    'type': 'FileNotFoundError',
                    'message': f"File not found: {filepath}",
                    'file': filepath,
                    'line': None,
                    'col': None,
                }
            }))
        else:
            print(f"Error: File not found: {filepath}", file=sys.stderr)
        return 1

    try:
        declarations, assignments = parse_circ_file(filepath)
        result = validate_circ_file(filepath, declarations, assignments)
    except CircuitError as e:
        if json_output:
            print(format_error_json(e, 'eval'))
        else:
            print(f"Error: {e.message}", file=sys.stderr)
        return e.exit_code

    # Parse input assignments
    input_values = {}
    for assignment in set_inputs:
        if '=' not in assignment:
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 2,
                    'error': {
                        'type': 'InputValueParseError',
                        'message': f"Invalid input format: {assignment}. Expected name=value",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Invalid input format: {assignment}. Expected name=value", file=sys.stderr)
            return 2

        name, value_str = assignment.split('=', 1)
        name = name.strip()
        value_str = value_str.strip()

        # Parse value
        if value_str not in ('0', '1'):
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 2,
                    'error': {
                        'type': 'InputValueParseError',
                        'message': f"Invalid value: {value_str}. Must be 0 or 1",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Invalid value: {value_str}. Must be 0 or 1", file=sys.stderr)
            return 2

        input_values[name] = int(value_str)

    # Get declared input names
    declared_inputs = set(result['inputs'][i]['name'] for i in range(len(result['inputs'])))

    # Check for unknown inputs (unless allow_extra is set)
    for name in input_values:
        if name not in declared_inputs and not allow_extra:
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 1,
                    'error': {
                        'type': 'UnknownInputError',
                        'message': f"Unknown input: {name}",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Unknown input: {name}", file=sys.stderr)
            return 1

    # Check for missing inputs
    missing_inputs = declared_inputs - set(input_values.keys())
    if missing_inputs:
        if default is None:
            if json_output:
                print(json.dumps({
                    'ok': False,
                    'command': 'eval',
                    'exit_code': 1,
                    'error': {
                        'type': 'MissingInputError',
                        'message': f"Missing inputs: {', '.join(sorted(missing_inputs))}",
                        'file': None,
                        'line': None,
                        'col': None,
                    }
                }))
            else:
                print(f"Error: Missing inputs: {', '.join(sorted(missing_inputs))}", file=sys.stderr)
            return 1
        else:
            # Fill missing inputs with default value
            for name in missing_inputs:
                input_values[name] = default

    # Build a dependency graph for topological evaluation
    graph: Dict[str, List[str]] = {}
    for lhs, (op, args) in assignments:
        graph[lhs] = []
        for arg in args:
            if arg not in ('0', '1'):
                graph[lhs].append(arg)

    # Evaluate in topological order (inputs first, then dependencies)
    values = dict(input_values)  # Start with input values

    # Process assignments in order (assuming they're already in correct order)
    # We'll repeatedly evaluate until all outputs are computed
    # This handles cases where assignments are already in topological order
    for lhs, (op, args) in assignments:
        # Check if all dependencies are available
        all_deps_ready = True
        for arg in args:
            if arg not in values and arg not in ('0', '1'):
                all_deps_ready = False
                break

        if all_deps_ready:
            values[lhs] = evaluate_operator(op, args, values)

    # Get output values
    outputs = []
    for out_info in result['outputs']:
        out_name = out_info['name']
        if out_name in values:
            outputs.append({
                'name': out_name,
                'msb': out_info['msb'],
                'lsb': out_info['lsb'],
                'value': str(values[out_name])
            })
        else:
            # This shouldn't happen if circuit is valid
            outputs.append({
                'name': out_name,
                'msb': out_info['msb'],
                'lsb': out_info['lsb'],
                'value': '0'
            })

    # Sort outputs by name
    outputs.sort(key=lambda x: x['name'])

    # Output results
    if json_output:
        print(json.dumps({
            'ok': True,
            'command': 'eval',
            'mode': '2val',
            'radix': 'bin',
            'outputs': outputs
        }))
    else:
        for out in outputs:
            print(f"{out['name']}={out['value']}")

    return 0


def json_cli_error(command: str, error_type: str, message: str) -> None:
    """Print JSON error for CLI errors."""
    print(json.dumps({
        'ok': False,
        'command': command,
        'exit_code': 1,
        'error': {
            'type': error_type,
            'message': message,
            'file': None,
            'line': None,
            'col': None,
        }
    }))


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog='circopt',
        description='Circuit optimizer',
        add_help=False,
    )
    parser.add_argument('--help', action='store_true', default=False, dest='help_flag')
    parser.add_argument('--version', action='store_true', default=False, dest='version_flag')
    parser.add_argument('--json', action='store_true', default=False, dest='json_flag')

    # Parse known args first (global flags)
    namespace, remaining = parser.parse_known_args()

    # Check for help/version flags
    if namespace.help_flag:
        print("Usage: circopt [OPTIONS] <COMMAND> [ARGS]")
        print("")
        print("Options:")
        print("  --help, -h      Show this help message")
        print("  --version, -v   Show version information")
        print("  --json          Output as JSON")
        print("")
        print("Commands:")
        print("  check <file>    Parse and validate a .circ file")
        print("  eval <file>     Evaluate circuit with given inputs")
        return 0

    if namespace.version_flag:
        if namespace.json_flag:
            print(format_version_json())
        else:
            print(VERSION)
        return 0

    # Parse remaining args for command and file
    if not remaining:
        if namespace.json_flag:
            json_cli_error('__cli__', 'CliUsageError', 'No command specified')
        else:
            print("Error: No command specified", file=sys.stderr)
            print("Use --help for usage information", file=sys.stderr)
        return 1

    # The first non-option is the command
    command = remaining[0]

    # Find the file argument (second arg, or after -- if present)
    args_after_command = remaining[1:]
    file_arg = None

    for arg in args_after_command:
        if not arg.startswith('-'):
            file_arg = arg
            break

    # Handle commands
    if command == 'check':
        if file_arg is None:
            if namespace.json_flag:
                json_cli_error('check', 'CliUsageError', 'Missing required argument: <file>')
            else:
                print("Error: Missing required argument: <file>", file=sys.stderr)
            return 1
        return cmd_check(file_arg, namespace.json_flag)

    elif command == 'eval':
        if file_arg is None:
            if namespace.json_flag:
                json_cli_error('eval', 'CliUsageError', 'Missing required argument: <file>')
            else:
                print("Error: Missing required argument: <file>", file=sys.stderr)
            return 1

        # Parse eval-specific arguments
        set_inputs = []
        default = None
        allow_extra = False

        i = 0
        while i < len(args_after_command):
            arg = args_after_command[i]
            if arg == '--set' and i + 1 < len(args_after_command):
                set_inputs.append(args_after_command[i + 1])
                i += 2
            elif arg == '--default' and i + 1 < len(args_after_command):
                val = args_after_command[i + 1]
                if val == '0':
                    default = 0
                elif val == '1':
                    default = 1
                else:
                    if namespace.json_flag:
                        json_cli_error('eval', 'InputValueParseError', f"Invalid default value: {val}. Must be 0 or 1")
                    else:
                        print(f"Error: Invalid default value: {val}. Must be 0 or 1", file=sys.stderr)
                    return 2
                i += 2
            elif arg == '--allow-extra':
                allow_extra = True
                i += 1
            elif arg == file_arg:
                # Skip the file argument
                i += 1
            else:
                i += 1

        return cmd_eval(file_arg, set_inputs, default, allow_extra, namespace.json_flag)

    else:
        if namespace.json_flag:
            json_cli_error('__cli__', 'CliUsageError', f"Unknown command: {command}")
        else:
            print(f"Error: Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())