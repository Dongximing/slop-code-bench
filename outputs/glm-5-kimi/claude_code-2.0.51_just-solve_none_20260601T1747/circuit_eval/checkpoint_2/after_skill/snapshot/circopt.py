#!/usr/bin/env python3
"""circopt - Circuit optimizer and equivalence checker."""

import sys
import json
import os
import re
from collections import defaultdict

VERSION = "1.0.0"

# ─── Exit codes ───────────────────────────────────────────────────────────────

EXIT_SUCCESS = 0
EXIT_CLI_USAGE = 1
EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INTERNAL_ERROR = 4
EXIT_NON_EQUIVALENCE = 10

# ─── Operator definitions ────────────────────────────────────────────────────

OPERATORS = {
    "NOT": (1, 1),    # exactly 1
    "BUF": (1, 1),    # exactly 1
    "AND": (2, None),  # >= 2
    "OR": (2, None),
    "XOR": (2, None),
    "NAND": (2, None),
    "NOR": (2, None),
    "XNOR": (2, None),
}

# ─── Error classes ────────────────────────────────────────────────────────────

class CircError(Exception):
    """Base error for circuit operations."""
    exit_code = EXIT_INTERNAL_ERROR

    def __init__(self, message, file=None, line=None, col=None):
        self.message = message
        self.file = file
        self.line = line
        self.col = col
        super().__init__(message)

    def to_dict(self):
        return {
            "type": self.__class__.__name__,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "col": self.col,
        }


class CliUsageError(CircError):
    exit_code = EXIT_CLI_USAGE


class FileNotFoundError_(CircError):
    exit_code = EXIT_CLI_USAGE


class CircParseError(CircError):
    exit_code = EXIT_PARSE_ERROR


class DeclarationAfterAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class DuplicateNameError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class UndefinedNameError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class UnassignedSignalError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class InputAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class MultipleAssignmentError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class ArityError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class CycleError(CircError):
    exit_code = EXIT_VALIDATION_ERROR


class MissingInputError(CircError):
    exit_code = EXIT_CLI_USAGE


class UnknownInputError(CircError):
    exit_code = EXIT_CLI_USAGE


class InputValueParseError(CircError):
    exit_code = EXIT_PARSE_ERROR


# ─── Tokenizer / Parser ──────────────────────────────────────────────────────

IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
LITERAL_RE = re.compile(r'[01]')


class Token:
    __slots__ = ('kind', 'value', 'line', 'col')

    def __init__(self, kind, value, line, col):
        self.kind = kind
        self.value = value
        self.line = line
        self.col = col

    def __repr__(self):
        return f"Token({self.kind}, {self.value!r}, L{self.line}:{self.col})"


def tokenize(text, filename="<stdin>"):
    """Tokenize .circ source text into a list of Tokens."""
    tokens = []
    lines = text.split('\n')
    filtered_lines = []  # (line_number, line_text) - skip blanks and comments

    for i, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if stripped == '' or stripped.startswith('#'):
            continue
        filtered_lines.append((i, raw_line))

    for line_num, raw_line in filtered_lines:
        line_text = raw_line
        pos = 0
        length = len(line_text)

        while pos < length:
            ch = line_text[pos]

            if ch in ' \t':
                pos += 1
                continue

            col = pos + 1  # 1-based column

            if ch == '=':
                tokens.append(Token('eq', '=', line_num, col))
                pos += 1
            elif ch == '(':
                tokens.append(Token('lparen', '(', line_num, col))
                pos += 1
            elif ch == ')':
                tokens.append(Token('rparen', ')', line_num, col))
                pos += 1
            elif ch == ',':
                tokens.append(Token('comma', ',', line_num, col))
                pos += 1
            elif ch == '[' or ch == ']':
                raise CircParseError(
                    f"Brackets are not supported in scalar circuits",
                    file=filename, line=line_num, col=col,
                )
            elif ch in '01':
                tokens.append(Token('lit', ch, line_num, col))
                pos += 1
            elif ch.isalpha() or ch == '_':
                m = IDENT_RE.match(line_text, pos)
                if m:
                    tokens.append(Token('ident', m.group(), line_num, col))
                    pos = m.end()
                else:
                    raise CircParseError(
                        f"Unexpected character '{ch}'",
                        file=filename, line=line_num, col=col,
                    )
            else:
                raise CircParseError(
                    f"Unexpected character '{ch}'",
                    file=filename, line=line_num, col=col,
                )

    return tokens


# ─── Expression AST ──────────────────────────────────────────────────────────

class ExprIdent:
    __slots__ = ('name', 'line', 'col')
    def __init__(self, name, line, col):
        self.name = name
        self.line = line
        self.col = col


class ExprLit:
    __slots__ = ('value', 'line', 'col')
    def __init__(self, value, line, col):
        self.value = value
        self.line = line
        self.col = col


class ExprCall:
    __slots__ = ('op', 'args', 'line', 'col')
    def __init__(self, op, args, line, col):
        self.op = op
        self.args = args
        self.line = line
        self.col = col


# ─── Parser ──────────────────────────────────────────────────────────────────

class Parser:
    def __init__(self, tokens, filename):
        self.tokens = tokens
        self.filename = filename
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.peek()
        if tok is None or tok.kind != kind:
            last_tok = self.tokens[self.pos - 1] if self.pos > 0 else None
            if tok:
                raise CircParseError(
                    f"Expected '{kind}' but got '{tok.kind}'",
                    file=self.filename, line=tok.line, col=tok.col,
                )
            elif last_tok:
                raise CircParseError(
                    f"Unexpected end of file",
                    file=self.filename, line=last_tok.line,
                    col=last_tok.col + len(str(last_tok.value)),
                )
            else:
                raise CircParseError(
                    f"Unexpected end of file",
                    file=self.filename, line=1, col=1,
                )
        return self.advance()

    def at_end(self):
        return self.pos >= len(self.tokens)

    def parse(self):
        """Parse the entire token stream into declarations and assignments."""
        inputs = []
        outputs = []
        wires = []
        assignments = []
        has_assignment = False

        while not self.at_end():
            tok = self.peek()

            # Check for assignment: ident '=' ...
            if self.pos + 1 < len(self.tokens) and self.tokens[self.pos + 1].kind == 'eq':
                has_assignment = True
                lhs = tok.value
                lhs_line = tok.line
                lhs_col = tok.col
                self.advance()  # consume ident
                self.advance()  # consume '='
                expr = self._parse_expr()
                assignments.append((lhs, expr, lhs_line, lhs_col))
            else:
                # Must be a declaration
                if has_assignment:
                    raise DeclarationAfterAssignmentError(
                        f"Declaration after assignment is not allowed",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                kw = tok.value
                kw_upper = kw.upper()

                if kw_upper not in ('INPUT', 'OUTPUT', 'WIRE'):
                    raise CircParseError(
                        f"Expected declaration keyword or assignment, got '{kw}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                self.advance()  # consume keyword

                # Collect names until we hit something that isn't an ident
                # or is a keyword (INPUT, OUTPUT, WIRE)
                KEYWORDS = {'INPUT', 'OUTPUT', 'WIRE'}
                names = []
                while not self.at_end():
                    nxt = self.peek()
                    if nxt.kind != 'ident':
                        break
                    # If this looks like a keyword, stop collecting names
                    if nxt.value.upper() in KEYWORDS:
                        break
                    # Check if next token after this ident is '=' (assignment)
                    if (self.pos + 1 < len(self.tokens) and
                            self.tokens[self.pos + 1].kind == 'eq'):
                        break
                    name_tok = self.advance()
                    names.append((name_tok.value, name_tok.line, name_tok.col))

                if not names:
                    raise CircParseError(
                        f"Expected at least one name after '{kw}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                if kw_upper == 'INPUT':
                    inputs.extend(names)
                elif kw_upper == 'OUTPUT':
                    outputs.extend(names)
                elif kw_upper == 'WIRE':
                    wires.extend(names)

        return inputs, outputs, wires, assignments

    def _parse_expr(self):
        tok = self.peek()
        if tok is None:
            raise CircParseError(
                "Unexpected end of file in expression",
                file=self.filename, line=1, col=1,
            )

        if tok.kind == 'lit':
            self.advance()
            return ExprLit(tok.value, tok.line, tok.col)

        if tok.kind == 'ident':
            # Could be a plain identifier or a function call
            self.advance()

            # Check for function call: ident '(' args ')'
            nxt = self.peek()
            if nxt is not None and nxt.kind == 'lparen':
                op_name = tok.value.upper()

                if op_name not in OPERATORS:
                    raise CircParseError(
                        f"Unknown operator '{tok.value}'",
                        file=self.filename, line=tok.line, col=tok.col,
                    )

                self.advance()  # consume '('
                args = []

                # Check for empty args
                nxt = self.peek()
                if nxt is not None and nxt.kind == 'rparen':
                    self.advance()  # consume ')'
                    self._check_arity(op_name, len(args), tok.line, tok.col)
                    return ExprCall(op_name, args, tok.line, tok.col)

                args.append(self._parse_expr())

                while not self.at_end():
                    nxt = self.peek()
                    if nxt.kind == 'comma':
                        self.advance()  # consume ','
                        args.append(self._parse_expr())
                    elif nxt.kind == 'rparen':
                        self.advance()  # consume ')'
                        break
                    else:
                        raise CircParseError(
                            f"Expected ',' or ')' in function call",
                            file=self.filename, line=nxt.line, col=nxt.col,
                        )
                else:
                    # Loop ended without finding closing paren (EOF)
                    last_arg = args[-1] if args else None
                    if last_arg:
                        raise CircParseError(
                            f"Unclosed function call - missing ')'",
                            file=self.filename, line=last_arg.line,
                            col=last_arg.col + 1,
                        )
                    else:
                        raise CircParseError(
                            f"Unclosed function call - missing ')'",
                            file=self.filename, line=tok.line, col=tok.col,
                        )

                self._check_arity(op_name, len(args), tok.line, tok.col)
                return ExprCall(op_name, args, tok.line, tok.col)
            else:
                # Plain identifier
                name_upper = tok.value.upper()
                # Check for literal X (unknown)
                if name_upper == 'X':
                    raise CircParseError(
                        f"Literal 'X' (unknown/don't-care) is not allowed",
                        file=self.filename, line=tok.line, col=tok.col,
                    )
                return ExprIdent(tok.value, tok.line, tok.col)

        raise CircParseError(
            f"Unexpected token '{tok.value}' in expression",
            file=self.filename, line=tok.line, col=tok.col,
        )

    def _check_arity(self, op, nargs, line, col):
        min_arity, max_arity = OPERATORS[op]
        if nargs < min_arity:
            raise ArityError(
                f"Operator '{op}' requires at least {min_arity} argument(s), got {nargs}",
                file=self.filename, line=line, col=col,
            )
        if max_arity is not None and nargs > max_arity:
            raise ArityError(
                f"Operator '{op}' requires exactly {max_arity} argument(s), got {nargs}",
                file=self.filename, line=line, col=col,
            )


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_circuit(inputs, outputs, wires, assignments, filename):
    """Validate a parsed circuit. Returns sorted inputs/outputs for output."""

    # Collect all declared names
    all_names = {}  # name -> (type, line, col)

    for name, line, col in inputs:
        if name in all_names:
            existing_type, eline, ecol = all_names[name]
            raise DuplicateNameError(
                f"Duplicate name '{name}' (first declared as {existing_type})",
                file=filename, line=line, col=col,
            )
        all_names[name] = ('input', line, col)

    for name, line, col in outputs:
        if name in all_names:
            existing_type, eline, ecol = all_names[name]
            raise DuplicateNameError(
                f"Duplicate name '{name}' (first declared as {existing_type})",
                file=filename, line=line, col=col,
            )
        all_names[name] = ('output', line, col)

    for name, line, col in wires:
        if name in all_names:
            existing_type, eline, ecol = all_names[name]
            raise DuplicateNameError(
                f"Duplicate name '{name}' (first declared as {existing_type})",
                file=filename, line=line, col=col,
            )
        all_names[name] = ('wire', line, col)

    input_set = {name for name, _, _ in inputs}
    output_set = {name for name, _, _ in outputs}
    wire_set = {name for name, _, _ in wires}
    assignable = output_set | wire_set

    # Check assignments
    assigned = {}  # name -> (line, col) of assignment

    for lhs, expr, lhs_line, lhs_col in assignments:
        # LHS must be a declared wire or output
        if lhs not in all_names:
            raise UndefinedNameError(
                f"Name '{lhs}' is not declared",
                file=filename, line=lhs_line, col=lhs_col,
            )

        # LHS must not be an input
        if lhs in input_set:
            raise InputAssignmentError(
                f"Cannot assign to input '{lhs}'",
                file=filename, line=lhs_line, col=lhs_col,
            )

        # LHS must not be assigned multiple times
        if lhs in assigned:
            prev_line, prev_col = assigned[lhs]
            raise MultipleAssignmentError(
                f"Signal '{lhs}' is assigned multiple times",
                file=filename, line=lhs_line, col=lhs_col,
            )

        assigned[lhs] = (lhs_line, lhs_col)

        # Validate expression references
        _validate_expr(expr, all_names, filename)

    # Check all outputs and wires are assigned
    for name, line, col in outputs:
        if name not in assigned:
            raise UnassignedSignalError(
                f"Output '{name}' is never assigned",
                file=filename, line=line, col=col,
            )

    for name, line, col in wires:
        if name not in assigned:
            raise UnassignedSignalError(
                f"Wire '{name}' is never assigned",
                file=filename, line=line, col=col,
            )

    # Check for cycles using dependency graph
    _check_cycles(assignments, all_names, filename)

    # Build sorted output
    sorted_inputs = sorted(
        [{"name": name, "msb": 0, "lsb": 0} for name, _, _ in inputs],
        key=lambda x: x["name"],
    )
    sorted_outputs = sorted(
        [{"name": name, "msb": 0, "lsb": 0} for name, _, _ in outputs],
        key=lambda x: x["name"],
    )

    return sorted_inputs, sorted_outputs


def _validate_expr(expr, all_names, filename):
    """Validate that all identifiers in an expression are declared."""
    if isinstance(expr, ExprIdent):
        if expr.name not in all_names:
            raise UndefinedNameError(
                f"Name '{expr.name}' is not declared",
                file=filename, line=expr.line, col=expr.col,
            )
    elif isinstance(expr, ExprCall):
        for arg in expr.args:
            _validate_expr(arg, all_names, filename)


def _check_cycles(assignments, all_names, filename):
    """Detect cycles in the assignment dependency graph."""
    # Build adjacency: for each LHS, what does it depend on?
    deps = {}  # lhs -> set of dependency names

    def _collect_deps(expr, dep_set):
        if isinstance(expr, ExprIdent):
            dep_set.add(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                _collect_deps(arg, dep_set)

    for lhs, expr, lhs_line, lhs_col in assignments:
        dep_set = set()
        _collect_deps(expr, dep_set)
        deps[lhs] = dep_set

    # DFS-based cycle detection
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in deps}
    path = []

    def dfs(node):
        color[node] = GRAY
        path.append(node)
        for neighbor in deps.get(node, set()):
            if neighbor not in deps:
                # Dependency on input or literal - not in assignment graph
                continue
            if color[neighbor] == GRAY:
                # Found cycle - build cycle path
                cycle_start = path.index(neighbor)
                cycle_path = path[cycle_start:] + [neighbor]
                cycle_str = ' -> '.join(cycle_path)
                raise CycleError(
                    f"Dependency cycle detected: {cycle_str}",
                    file=filename, line=None, col=None,
                )
            if color[neighbor] == WHITE:
                dfs(neighbor)
        path.pop()
        color[node] = BLACK

    for node in deps:
        if color[node] == WHITE:
            dfs(node)


# ─── JSON output helpers ─────────────────────────────────────────────────────

def json_success(command, **kwargs):
    result = {"ok": True, "command": command}
    result.update(kwargs)
    return json.dumps(result, separators=(',', ':'))


def json_error(command, error):
    err_dict = error.to_dict()
    err_dict.setdefault("file", None)
    err_dict.setdefault("line", None)
    err_dict.setdefault("col", None)
    return json.dumps({
        "ok": False,
        "command": command,
        "exit_code": error.exit_code,
        "error": err_dict,
    }, separators=(',', ':'))


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_check(filepath, use_json):
    """Parse and validate a .circ file."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError_(
            f"File not found: {filepath}",
            file=filepath,
        )

    with open(filepath, 'r') as f:
        text = f.read()

    tokens = tokenize(text, filepath)

    parser = Parser(tokens, filepath)
    inputs, outputs, wires, assignments = parser.parse()

    sorted_inputs, sorted_outputs = validate_circuit(
        inputs, outputs, wires, assignments, filepath,
    )

    if use_json:
        print(json_success(
            "check",
            format="circ",
            inputs=sorted_inputs,
            outputs=sorted_outputs,
        ))
    else:
        print("Circuit is valid.")
        print(f"  Inputs: {', '.join(i['name'] for i in sorted_inputs)}")
        print(f"  Outputs: {', '.join(o['name'] for o in sorted_outputs)}")


# ─── Boolean evaluation ──────────────────────────────────────────────────────

def _bool_op(op, args):
    """Evaluate a Boolean operator on a list of 0/1 integer values. Returns 0 or 1."""
    if op == "NOT":
        return 1 - args[0]
    elif op == "BUF":
        return args[0]
    elif op == "AND":
        return 1 if all(a == 1 for a in args) else 0
    elif op == "OR":
        return 1 if any(a == 1 for a in args) else 0
    elif op == "XOR":
        result = 0
        for a in args:
            result ^= a
        return result
    elif op == "NAND":
        return 0 if all(a == 1 for a in args) else 1
    elif op == "NOR":
        return 0 if any(a == 1 for a in args) else 1
    elif op == "XNOR":
        result = 1
        for a in args:
            result ^= a
        return result
    else:
        raise CircError(f"Unknown operator '{op}'")


def _eval_expr(expr, values):
    """Evaluate an expression AST given a dict of signal name -> 0/1 value."""
    if isinstance(expr, ExprLit):
        return int(expr.value)
    elif isinstance(expr, ExprIdent):
        return values[expr.name]
    elif isinstance(expr, ExprCall):
        arg_vals = [_eval_expr(arg, values) for arg in expr.args]
        return _bool_op(expr.op, arg_vals)
    else:
        raise CircError("Unknown expression type")


def _topo_sort_assignments(assignments):
    """Topologically sort assignments based on dependencies."""
    # Build dependency graph
    deps = {}  # lhs -> set of names it depends on
    lhs_list = []

    def _collect_deps(expr, dep_set):
        if isinstance(expr, ExprIdent):
            dep_set.add(expr.name)
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                _collect_deps(arg, dep_set)

    for lhs, expr, lhs_line, lhs_col in assignments:
        dep_set = set()
        _collect_deps(expr, dep_set)
        deps[lhs] = dep_set
        lhs_list.append(lhs)

    # Topological sort (Kahn's algorithm)
    in_degree = {lhs: 0 for lhs in lhs_list}
    # adjacency: from dependency -> list of dependents
    adj = defaultdict(list)
    for lhs in lhs_list:
        for dep in deps[lhs]:
            if dep in deps:  # only track dependencies on other assigned signals
                adj[dep].append(lhs)
                in_degree[lhs] += 1

    queue = [lhs for lhs in lhs_list if in_degree[lhs] == 0]
    queue.sort()  # deterministic ordering
    sorted_lhs = []

    while queue:
        node = queue.pop(0)
        sorted_lhs.append(node)
        for neighbor in sorted(adj[node]):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)
        queue.sort()

    # Build assignment map: lhs -> (expr, line, col)
    assign_map = {}
    for lhs, expr, lhs_line, lhs_col in assignments:
        assign_map[lhs] = (expr, lhs_line, lhs_col)

    return [(lhs, assign_map[lhs][0], assign_map[lhs][1], assign_map[lhs][2])
            for lhs in sorted_lhs]


def cmd_eval(filepath, set_pairs, default_val, allow_extra, use_json):
    """Evaluate a circuit with given inputs."""
    if not os.path.isfile(filepath):
        raise FileNotFoundError_(
            f"File not found: {filepath}",
            file=filepath,
        )

    with open(filepath, 'r') as f:
        text = f.read()

    tokens = tokenize(text, filepath)

    parser = Parser(tokens, filepath)
    inputs, outputs, wires, assignments = parser.parse()

    sorted_inputs, sorted_outputs = validate_circuit(
        inputs, outputs, wires, assignments, filepath,
    )

    # Build input name set
    input_names = {name for name, _, _ in inputs}
    output_names = {name for name, _, _ in outputs}

    # Parse --set values
    input_values = {}
    for name, value_str in set_pairs:
        # Check for unknown inputs
        if name not in input_names and not allow_extra:
            raise UnknownInputError(
                f"Unknown input '{name}'",
                file=filepath,
            )
        # Parse value
        if value_str not in ('0', '1'):
            raise InputValueParseError(
                f"Invalid input value '{value_str}' for '{name}': must be 0 or 1",
                file=filepath,
            )
        if name in input_names:
            input_values[name] = int(value_str)

    # Check for missing inputs
    missing = input_names - set(input_values.keys())
    if missing:
        if default_val is not None:
            for name in missing:
                input_values[name] = default_val
        else:
            missing_sorted = sorted(missing)
            raise MissingInputError(
                f"Missing input(s): {', '.join(missing_sorted)}",
                file=filepath,
            )

    # Topologically sort assignments for evaluation order
    sorted_assignments = _topo_sort_assignments(assignments)

    # Evaluate
    values = dict(input_values)
    for lhs, expr, lhs_line, lhs_col in sorted_assignments:
        values[lhs] = _eval_expr(expr, values)

    # Collect output results sorted by name
    output_results = []
    for name in sorted(output_names):
        output_results.append({"name": name, "msb": 0, "lsb": 0, "value": str(values[name])})

    if use_json:
        print(json_success(
            "eval",
            mode="2val",
            radix="bin",
            outputs=output_results,
        ))
    else:
        for item in output_results:
            print(f"{item['name']}={item['value']}")


# ─── Main CLI ────────────────────────────────────────────────────────────────

def _parse_eval_args(cmd_args):
    """Parse arguments for the eval command.
    Returns (filepath, set_pairs, default_val, allow_extra, use_json).
    """
    filepath = None
    set_pairs = []
    default_val = None
    allow_extra = False
    use_json = False

    i = 0
    while i < len(cmd_args):
        arg = cmd_args[i]

        if arg == '--json':
            use_json = True
            i += 1
        elif arg == '--set':
            if i + 1 >= len(cmd_args):
                raise CliUsageError("--set requires an argument of the form name=value")
            pair = cmd_args[i + 1]
            if '=' not in pair:
                raise CliUsageError(f"--set argument must be of the form name=value, got '{pair}'")
            name, value = pair.split('=', 1)
            set_pairs.append((name, value))
            i += 2
        elif arg == '--default':
            if i + 1 >= len(cmd_args):
                raise CliUsageError("--default requires an argument (0 or 1)")
            val_str = cmd_args[i + 1]
            if val_str not in ('0', '1'):
                raise CliUsageError(f"--default argument must be 0 or 1, got '{val_str}'")
            default_val = int(val_str)
            i += 2
        elif arg == '--allow-extra':
            allow_extra = True
            i += 1
        elif arg.startswith('--'):
            raise CliUsageError(f"Unknown option '{arg}'")
        else:
            # Positional argument - should be the file
            if filepath is None:
                filepath = arg
            else:
                raise CliUsageError(f"Unexpected argument '{arg}'")
            i += 1

    if filepath is None:
        raise CliUsageError("eval command requires a file argument.")

    return filepath, set_pairs, default_val, allow_extra, use_json


def main():
    args = sys.argv[1:]

    # Check for --help
    if '--help' in args:
        # Always plain text for help
        print_help()
        sys.exit(EXIT_SUCCESS)

    # Check for --version
    if '--version' in args:
        use_json = '--json' in args
        if use_json:
            print(json.dumps({
                "ok": True,
                "command": "__version__",
                "version": VERSION,
            }, separators=(',', ':')))
        else:
            print(VERSION)
        sys.exit(EXIT_SUCCESS)

    # Check for --json flag globally
    use_json = '--json' in args

    # Filter out --json from args for command parsing
    cmd_args = [a for a in args if a != '--json']

    if not cmd_args:
        # No command specified
        err = CliUsageError("No command specified. Use --help for usage information.")
        if use_json:
            print(json_error("__cli__", err))
        else:
            print(f"Error: {err.message}", file=sys.stderr)
        sys.exit(EXIT_CLI_USAGE)

    command = cmd_args[0]

    if command == 'check':
        if len(cmd_args) < 2:
            err = CliUsageError("check command requires a file argument.")
            if use_json:
                print(json_error("check", err))
            else:
                print(f"Error: {err.message}", file=sys.stderr)
            sys.exit(EXIT_CLI_USAGE)

        filepath = cmd_args[1]
        try:
            cmd_check(filepath, use_json)
        except CircError as e:
            if use_json:
                print(json_error("check", e))
            else:
                loc = ""
                if e.file:
                    loc += f" in {e.file}"
                if e.line is not None:
                    loc += f" at line {e.line}"
                if e.col is not None:
                    loc += f", col {e.col}"
                print(f"Error: {e.message}{loc}", file=sys.stderr)
            sys.exit(e.exit_code)
        except Exception as e:
            err = CircError(f"Internal error: {e}")
            if use_json:
                print(json_error("check", err))
            else:
                print(f"Internal error: {e}", file=sys.stderr)
            sys.exit(EXIT_INTERNAL_ERROR)

    elif command == 'eval':
        try:
            filepath, set_pairs, default_val, allow_extra, eval_json = _parse_eval_args(cmd_args[1:])
            # Global --json overrides eval-specific json detection
            actual_json = use_json or eval_json
            cmd_eval(filepath, set_pairs, default_val, allow_extra, actual_json)
        except CliUsageError as e:
            if use_json:
                print(json_error("eval", e))
            else:
                print(f"Error: {e.message}", file=sys.stderr)
            sys.exit(e.exit_code)
        except CircError as e:
            if use_json:
                print(json_error("eval", e))
            else:
                loc = ""
                if e.file:
                    loc += f" in {e.file}"
                if e.line is not None:
                    loc += f" at line {e.line}"
                if e.col is not None:
                    loc += f", col {e.col}"
                print(f"Error: {e.message}{loc}", file=sys.stderr)
            sys.exit(e.exit_code)
        except Exception as e:
            err = CircError(f"Internal error: {e}")
            if use_json:
                print(json_error("eval", err))
            else:
                print(f"Internal error: {e}", file=sys.stderr)
            sys.exit(EXIT_INTERNAL_ERROR)

    else:
        err = CliUsageError(f"Unknown command '{command}'. Use --help for usage information.")
        if use_json:
            print(json_error("__cli__", err))
        else:
            print(f"Error: {err.message}", file=sys.stderr)
        sys.exit(EXIT_CLI_USAGE)


def print_help():
    print("""Usage: circopt.py <command> [options] [arguments]

Global flags:
  --help       Print this help message and exit
  --version    Print version and exit
  --json       Output results as JSON

Commands:
  check <file.circ>                      Parse and validate a circuit file
  eval <file.circ> [options]             Evaluate circuit with given inputs
    --set name=value                     Set input value (can be repeated)
    --default 0|1                        Default value for missing inputs
    --allow-extra                        Allow unknown input names
    --json                               Output results as JSON""")


if __name__ == '__main__':
    main()
