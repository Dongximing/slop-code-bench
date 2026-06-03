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


class WidthMismatchError(CircoptError):
    """Operand widths don't match operator requirements - exit code 3."""
    pass


class IndexOutOfBoundsError(CircoptError):
    """Index or slice bounds exceed signal width - exit code 3."""
    pass


class InputWidthMismatchError(CircoptError):
    """Runtime input value width doesn't match declaration - exit code 3."""
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
    width: int = 1  # Default width is 1 (scalar)
    msb: int = 0    # Most significant bit position
    lsb: int = 0    # Least significant bit position


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
class ExprLiteralVec(Expr):
    """Vector literal - for sized and unsized literals."""
    value: int  # The integer value
    width: int  # The width of the literal
    is_sized: bool  # True if sized literal (e.g., 8'hff), False if unsized (e.g., 0xff)
    msb: int = 0  # For sized: the declared msb, for unsized: width-1
    lsb: int = 0  # For sized: the declared lsb, for unsized: 0


@dataclass
class ExprBitIndex(Expr):
    """Bit index extraction - v[i]"""
    operand: Expr
    index: int
    width: int = 1  # Result is always width 1


@dataclass
class ExprSlice(Expr):
    """Bit slice - v[hi:lo]"""
    operand: Expr
    msb: int
    lsb: int
    width: int  # Result width = msb - lsb + 1


@dataclass
class ExprConcat(Expr):
    """Concatenation - {e1, e2, ..., ek}"""
    operands: List[Expr]
    width: int  # Sum of operand widths


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

# Operator arities (all operators with call syntax)
OPERATOR_ARITIES = {
    'NOT': 1,
    'BUF': 1,
    'AND': 2,   # minimum
    'OR': 2,
    'XOR': 2,
    'NAND': 2,
    'NOR': 2,
    'XNOR': 2,
    'MUX': 3,
    'ITE': 3,
    'REDUCE_AND': 1,
    'REDUCE_OR': 1,
    'REDUCE_XOR': 1,
    'EQ': 2,
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

        # Store signal metadata with widths
        self.signal_widths: Dict[str, int] = {}  # name -> width
        self.signal_msb: Dict[str, int] = {}     # name -> msb
        self.signal_lsb: Dict[str, int] = {}     # name -> lsb
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

    def parse_name_list(self) -> List[tuple]:
        """Parse a space-separated list of names, handling vector declarations.
        Returns list of (name, width, msb, lsb) tuples."""
        names = []
        while True:
            self.skip_whitespace()
            if self.peek() is None:
                break
            name = self.get_identifier()
            if not name:
                break

            # Check for vector declaration [msb:lsb]
            width = 1
            msb = 0
            lsb = 0

            if self.peek() == '[':
                self.get_char()  # consume '['
                self.skip_whitespace()

                # Parse msb
                msb_str = []
                while self.pos < self.len:
                    c = self.peek()
                    if c and c.isdigit():
                        msb_str.append(self.get_char())
                    else:
                        break
                if not msb_str:
                    raise self.error("Expected msb in vector declaration")
                msb = int(''.join(msb_str))

                self.skip_whitespace()
                if self.peek() != ':':
                    raise self.error("Expected ':' in vector declaration")
                self.get_char()  # consume ':'
                self.skip_whitespace()

                # Parse lsb
                lsb_str = []
                while self.pos < self.len:
                    c = self.peek()
                    if c and c.isdigit():
                        lsb_str.append(self.get_char())
                    else:
                        break
                if not lsb_str:
                    raise self.error("Expected lsb in vector declaration")
                lsb = int(''.join(lsb_str))

                self.skip_whitespace()
                if self.peek() != ']':
                    raise self.error("Expected ']' in vector declaration")
                self.get_char()  # consume ']'

                if msb < lsb:
                    raise self.error("MSB must be >= LSB in vector declaration")
                if lsb < 0:
                    raise self.error("LSB must be >= 0 in vector declaration")

                width = msb - lsb + 1

            names.append((name, width, msb, lsb))
            self.skip_whitespace()

        return names

    def parse_expr(self) -> Expr:
        """Parse an expression."""
        self.skip_whitespace()

        # Check for concatenation { e1, e2, ..., ek }
        if self.peek() == '{':
            self.get_char()  # consume '{'
            operands = self.parse_expr_list_in_braces()
            self.skip_whitespace()
            if self.peek() != '}':
                raise self.error("Expected '}' for concatenation")
            self.get_char()  # consume '}'

            # Calculate total width
            total_width = 0
            for op in operands:
                if isinstance(op, ExprLiteral):
                    total_width += 1
                elif isinstance(op, ExprLiteralVec):
                    total_width += op.width
                elif isinstance(op, ExprIdentifier):
                    total_width += self.signal_widths.get(op.name, 1)
                elif isinstance(op, ExprBitIndex):
                    total_width += 1
                elif isinstance(op, ExprSlice):
                    total_width += op.width
                elif isinstance(op, ExprConcat):
                    total_width += op.width
                else:
                    # For function calls, we can't determine width at parse time
                    # Will be checked during validation
                    total_width += 1  # conservative estimate

            return ExprConcat(operands, total_width)

        # Check for identifier
        peek_char = self.peek()
        if peek_char and (peek_char.isalpha() or peek_char == '_'):
            ident = self.get_identifier()

            # Check if it's a function call (must be before checking for [)
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

            # Check for bit index or slice
            if self.peek() == '[':
                self.get_char()  # consume '['
                self.skip_whitespace()

                # Parse msb (or index for single index)
                msb_str = []
                while self.pos < self.len:
                    c = self.peek()
                    if c and c.isdigit():
                        msb_str.append(self.get_char())
                    else:
                        break
                if not msb_str:
                    raise self.error("Expected index in bit/slice selection")
                msb = int(''.join(msb_str))

                lsb = msb  # For bit index, lsb equals msb

                if self.peek() == ':':
                    # Slice syntax [hi:lo]
                    self.get_char()  # consume ':'
                    self.skip_whitespace()

                    lo_str = []
                    while self.pos < self.len:
                        c = self.peek()
                        if c and c.isdigit():
                            lo_str.append(self.get_char())
                        else:
                            break
                    if not lo_str:
                        raise self.error("Expected lo in slice selection")
                    lsb = int(''.join(lo_str))

                    self.skip_whitespace()
                    if self.peek() != ']':
                        raise self.error("Expected ']' in slice selection")
                    self.get_char()  # consume ']'

                    width = msb - lsb + 1
                    return ExprSlice(ExprIdentifier(ident), msb, lsb, width)

                else:
                    # Bit index [i]
                    self.skip_whitespace()
                    if self.peek() != ']':
                        raise self.error("Expected ']' in bit index")
                    self.get_char()  # consume ']'
                    return ExprBitIndex(ExprIdentifier(ident), msb)

            return ExprIdentifier(ident)

        # Check for sized literals (width'bvalue or width'hvalue or width'dvalue)
        if peek_char and peek_char.isdigit():
            width_str = []
            while self.pos < self.len:
                c = self.peek()
                if c and c.isdigit():
                    width_str.append(self.get_char())
                else:
                    break

            if width_str:
                self.skip_whitespace()
                if self.peek() == "'":
                    self.get_char()  # consume "'"
                    self.skip_whitespace()

                    radix_char = self.peek()
                    if radix_char in ('b', 'h', 'd'):
                        self.get_char()  # consume radix
                        self.skip_whitespace()

                        # Parse the value
                        value_str = []
                        while self.pos < self.len:
                            c = self.peek()
                            if c:
                                if c in '0123456789abcdefABCDEF':
                                    value_str.append(self.get_char())
                                elif c == '_':
                                    self.get_char()  # skip underscores
                                else:
                                    break
                            else:
                                break

                        if not value_str:
                            raise self.error("Expected value in sized literal")

                        value_str_clean = ''.join(value_str)
                        width = int(''.join(width_str))

                        if radix_char == 'b':
                            value = int(value_str_clean, 2)
                        elif radix_char == 'h':
                            value = int(value_str_clean, 16)
                        else:  # 'd'
                            value = int(value_str_clean, 10)

                        # Check if value fits in the specified width
                        max_val = (1 << width) - 1
                        if value > max_val:
                            raise self.error(
                                f"Value {value} exceeds width {width} (max {max_val})"
                            )

                        return ExprLiteralVec(value, width, True, width - 1, 0)
                    else:
                        # Not a sized literal, might be part of unsized literal
                        # Put back what we've read
                        self.pos -= len(width_str)
                        self.line_num -= sum(1 for c in width_str if c == '\n')
                        # This is messy, let's simplify

                        # Rewind to start and treat as unsized literal with implicit width 1
                        # Actually just treat as a scalar literal 0 or 1 if it's single digit
                        if len(width_str) == 1:
                            digit = width_str[0]
                            if digit == '0':
                                return ExprLiteral(False)
                            elif digit == '1':
                                return ExprLiteral(True)

                        raise self.error("Expected sized literal format")

        # Check for unsized literals (0b... or 0x...)
        if peek_char == '0':
            char0 = self.get_char()
            next_char = self.peek()
            if next_char == 'b':
                # Binary literal
                self.get_char()  # consume 'b'
                self.skip_whitespace()

                value_str = []
                has_digits = False
                while self.pos < self.len:
                    c = self.peek()
                    if c:
                        if c in '01':
                            value_str.append(self.get_char())
                            has_digits = True
                        elif c == '_':
                            self.get_char()  # skip underscores
                        else:
                            break
                    else:
                        break

                if not has_digits:
                    raise self.error("Expected at least one digit after '0b'")

                value = int(''.join(value_str), 2)
                width = len(value_str)
                return ExprLiteralVec(value, width, False, width - 1, 0)

            elif next_char == 'x' or next_char == 'X':
                # Hex literal (but X is forbidden in .circ files per spec)
                self.get_char()  # consume 'x' or 'X'
                # Actually the spec says literal 'X' is forbidden, but 0x is hex
                # Let me re-read: "literal X values are still forbidden in .circ files"
                # This means 0x0123... is fine, just standalone X is forbidden

                value_str = []
                has_digits = False
                while self.pos < self.len:
                    c = self.peek()
                    if c:
                        if c in '0123456789abcdefABCDEF':
                            value_str.append(self.get_char())
                            has_digits = True
                        elif c == '_':
                            self.get_char()  # skip underscores
                        else:
                            break
                    else:
                        break

                if not has_digits:
                    raise self.error("Expected at least one digit after '0x'")

                value = int(''.join(value_str), 16)
                # Each hex digit is 4 bits
                width = len(value_str) * 4
                return ExprLiteralVec(value, width, False, width - 1, 0)

        # Check for literal 0 or 1 (scalar)
        if peek_char and peek_char.isdigit():
            digit = self.get_char()
            if digit == '0':
                return ExprLiteral(False)
            elif digit == '1':
                return ExprLiteral(True)

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

    def parse_expr_list_in_braces(self) -> List[Expr]:
        """Parse a comma-separated list of expressions within braces for concatenation."""
        exprs = []
        self.skip_whitespace()

        if self.peek() == '}':
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

    def parse_line(self) -> Optional[Tuple]:
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
                names = self.parse_name_list()
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

                for name_info in names:
                    # name_info is a tuple (name, width, msb, lsb)
                    name = name_info[0]
                    width = name_info[1]
                    msb = name_info[2]
                    lsb = name_info[3]

                    # Check for duplicates
                    if name in self.all_names:
                        raise DuplicateNameError(
                            f"Duplicate name '{name}'"
                        )

                    self.all_names.add(name)
                    self.signal_widths[name] = width
                    self.signal_msb[name] = msb
                    self.signal_lsb[name] = lsb

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

        # Store signal widths in parse result for evaluator
        return {
            'inputs': self.inputs,
            'outputs': self.outputs,
            'wires': self.wires,
            'assignments': self.assignments,
            'signal_widths': self.signal_widths,
            'signal_msb': self.signal_msb,
            'signal_lsb': self.signal_lsb
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

        # Store signal metadata with widths
        self.signal_widths = parse_result.get('signal_widths', {})
        self.signal_msb = parse_result.get('signal_msb', {})
        self.signal_lsb = parse_result.get('signal_lsb', {})

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

        # Check width constraints (new for Part 3)
        self._check_width_constraints()

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
        elif isinstance(expr, ExprBitIndex):
            self._check_rhs_defined(expr.operand)
        elif isinstance(expr, ExprSlice):
            self._check_rhs_defined(expr.operand)
        elif isinstance(expr, ExprConcat):
            for arg in expr.operands:
                self._check_rhs_defined(arg)
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
        elif isinstance(expr, ExprBitIndex):
            deps.extend(self._collect_deps(expr.operand))
        elif isinstance(expr, ExprSlice):
            deps.extend(self._collect_deps(expr.operand))
        elif isinstance(expr, ExprConcat):
            for arg in expr.operands:
                deps.extend(self._collect_deps(arg))
        elif isinstance(expr, ExprCall):
            for arg in expr.args:
                deps.extend(self._collect_deps(arg))
        return deps

    def _check_width_constraints(self):
        """Check width constraints for all expressions."""
        for assign in self.assignments:
            lhs = assign.lhs
            expected_width = self.signal_widths.get(lhs, 1)
            actual_width = self._get_expr_width(assign.expr)

            if actual_width != expected_width:
                raise WidthMismatchError(
                    f"Width mismatch for '{lhs}': expected {expected_width}, got {actual_width}"
                )

    def _get_expr_width(self, expr: Expr) -> int:
        """Get the width of an expression."""
        if isinstance(expr, ExprIdentifier):
            return self.signal_widths.get(expr.name, 1)
        elif isinstance(expr, ExprLiteral):
            return 1
        elif isinstance(expr, ExprLiteralVec):
            return expr.width
        elif isinstance(expr, ExprBitIndex):
            # Get width of the operand and check index bounds
            operand_width = self._get_expr_width(expr.operand)
            index = expr.index

            # Check if index is within bounds (index should be in range [0, width-1])
            if index < 0 or index >= operand_width:
                raise IndexOutOfBoundsError(
                    f"Index {index} out of bounds for signal of width {operand_width}"
                )
            return 1  # Bit index always results in width 1

        elif isinstance(expr, ExprSlice):
            # Get width of the operand and check slice bounds
            operand_width = self._get_expr_width(expr.operand)
            msb = expr.msb
            lsb = expr.lsb

            # Check if slice is within bounds
            if msb < 0 or msb >= operand_width:
                raise IndexOutOfBoundsError(
                    f"MSB {msb} out of bounds for signal of width {operand_width}"
                )
            if lsb < 0 or lsb >= operand_width:
                raise IndexOutOfBoundsError(
                    f"LSB {lsb} out of bounds for signal of width {operand_width}"
                )
            if msb < lsb:
                raise IndexOutOfBoundsError(
                    f"MSB {msb} must be >= LSB {lsb}"
                )

            return msb - lsb + 1

        elif isinstance(expr, ExprConcat):
            total_width = 0
            for operand in expr.operands:
                total_width += self._get_expr_width(operand)
            return total_width

        elif isinstance(expr, ExprCall):
            op = expr.op
            args = expr.args

            # Get widths of all arguments
            arg_widths = [self._get_expr_width(arg) for arg in args]

            if op in ('NOT', 'BUF', 'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
                # Unary operators: result width = operand width
                if arg_widths[0] < 1:
                    raise WidthMismatchError(
                        f"Operator '{op}' requires operand width >= 1"
                    )
                return arg_widths[0]

            elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR', 'EQ'):
                # Binary operators: all operands must have same width
                if len(arg_widths) < 2:
                    raise ArityError(f"Operator '{op}' requires at least 2 arguments")
                first_width = arg_widths[0]
                for i, w in enumerate(arg_widths):
                    if w != first_width:
                        raise WidthMismatchError(
                            f"Operator '{op}': operand {i+1} has width {w}, expected {first_width}"
                        )
                if op == 'EQ':
                    return 1  # EQ always returns 1 bit
                return first_width

            elif op in ('MUX', 'ITE'):
                # MUX/ITE: sel must be width 1, a and b must have same width
                if len(arg_widths) != 3:
                    raise ArityError(f"Operator '{op}' requires exactly 3 arguments")
                if arg_widths[0] != 1:
                    raise WidthMismatchError(
                        f"Operator '{op}': selector must have width 1, got {arg_widths[0]}"
                    )
                if arg_widths[1] != arg_widths[2]:
                    raise WidthMismatchError(
                        f"Operator '{op}': operands a and b must have same width (got {arg_widths[1]} and {arg_widths[2]})"
                    )
                return arg_widths[1]  # Result width = width of a

        raise ValueError(f"Unknown expression type: {type(expr)}")


# =============================================================================
# Bit Vector Evaluator (2-valued Boolean)
# =============================================================================

class BitVecEvaluator:
    """Evaluate a circuit with vector bit operations (2-valued Boolean logic)."""

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
        self.signal_widths = parse_result.get('signal_widths', {})

        # Build evaluation graph
        self._build_graph()

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
        elif isinstance(expr, ExprBitIndex):
            deps.extend(self._collect_deps(expr.operand))
        elif isinstance(expr, ExprSlice):
            deps.extend(self._collect_deps(expr.operand))
        elif isinstance(expr, ExprConcat):
            for arg in expr.operands:
                deps.extend(self._collect_deps(arg))
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
            input_values: Dictionary mapping input names to literal values
            allow_extra: If True, ignore extra inputs instead of erroring

        Returns:
            Dictionary mapping output names to integer values

        Raises:
            MissingInputError: A required input is not provided
            UnknownInputError: An unknown input is provided
            InputWidthMismatchError: Input value width doesn't match declaration
        """
        # Parse input values
        parsed_values: Dict[str, int] = {}

        # Validate inputs
        for name in input_values:
            if name not in self.inputs:
                if allow_extra:
                    continue
                raise UnknownInputError(f"Unknown input '{name}'")

            val_str = input_values[name]
            width = self.signal_widths.get(name, 1)
            val = self._parse_literal(val_str, width)
            parsed_values[name] = val

        # Set default values for unspecified inputs
        for inp in self.inputs:
            if inp not in parsed_values:
                parsed_values[inp] = 0 if self.default else (1 if self.signal_widths.get(inp, 1) == 1 else 0)

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
                results[output] = parsed_values[output]
            else:
                raise MissingInputError(f"Output '{output}' was not computed")

        return results

    def _parse_literal(self, val_str: str, expected_width: int) -> int:
        """Parse a literal string value and check width."""
        val_str = val_str.strip()
        width = 1

        if val_str.startswith('0b') or val_str.startswith('0B'):
            # Binary literal
            val_str = val_str[2:]
            # Remove underscores
            val_str = val_str.replace('_', '')
            if not val_str:
                raise InputValueParseError(f"Invalid binary literal: {val_str}")
            width = len(val_str)
            value = int(val_str, 2)
        elif val_str.startswith('0x') or val_str.startswith('0X'):
            # Hex literal
            val_str = val_str[2:]
            # Remove underscores
            val_str = val_str.replace('_', '')
            if not val_str:
                raise InputValueParseError(f"Invalid hex literal: {val_str}")
            width = len(val_str) * 4
            value = int(val_str, 16)
        else:
            # Scalar or sized literal
            val_str = val_str.replace('_', '')
            if "'" in val_str:
                # Sized literal e.g., 8'b11001010, 8'hff, 8'd200
                parts = val_str.split("'")
                if len(parts) != 2:
                    raise InputValueParseError(f"Invalid sized literal: {val_str}")
                try:
                    width = int(parts[0])
                except ValueError:
                    raise InputValueParseError(f"Invalid width in literal: {val_str}")
                value_part = parts[1]
                radix = value_part[0]
                value_str = value_part[1:]

                if radix == 'b':
                    value = int(value_str, 2)
                elif radix == 'h':
                    value = int(value_str, 16)
                elif radix == 'd':
                    value = int(value_str, 10)
                else:
                    raise InputValueParseError(f"Unknown radix in literal: {val_str}")
            elif val_str in ('0', '1'):
                width = 1
                value = int(val_str)
            else:
                raise InputValueParseError(f"Invalid literal: {val_str}")

        # Check width matches expected
        if width != expected_width:
            raise InputWidthMismatchError(
                f"Input '{name}': width {width} doesn't match expected {expected_width}"
            )

        return value

    def _eval_expr(self, expr: Expr, values: Dict[str, int]) -> int:
        """Evaluate an expression with given signal values."""
        if isinstance(expr, ExprIdentifier):
            return values[expr.name]
        elif isinstance(expr, ExprLiteral):
            return 1 if expr.value else 0
        elif isinstance(expr, ExprLiteralVec):
            return expr.value
        elif isinstance(expr, ExprBitIndex):
            operand_val = self._eval_expr(expr.operand, values)
            operand_width = self.signal_widths.get(str(expr.operand.name), 1) if isinstance(expr.operand, ExprIdentifier) else None
            if operand_width is None:
                # Get width from evaluated operand
                operand_width = operand_val.bit_length()
                if operand_width == 0:
                    operand_width = 1
            # Extract bit at index
            if expr.index >= operand_width:
                return 0  # Out of bounds bits are 0
            return (operand_val >> expr.index) & 1
        elif isinstance(expr, ExprSlice):
            operand_val = self._eval_expr(expr.operand, values)
            width = expr.msb - expr.lsb + 1
            mask = ((1 << width) - 1) << expr.lsb
            return (operand_val & mask) >> expr.lsb
        elif isinstance(expr, ExprConcat):
            result = 0
            total_width = 0
            for operand in reversed(expr.operands):  # First operand is MSB
                val = self._eval_expr(operand, values)
                width = self.signal_widths.get(str(operand.name), 1) if isinstance(operand, ExprIdentifier) else None
                if width is None:
                    if isinstance(operand, ExprLiteralVec):
                        width = operand.width
                    elif isinstance(operand, ExprLiteral):
                        width = 1
                    else:
                        # Estimate width
                        width = val.bit_length()
                        if width == 0:
                            width = 1
                result = (result << width) | val
                total_width += width
            return result
        elif isinstance(expr, ExprCall):
            op = expr.op
            arg_values = [self._eval_expr(arg, values) for arg in expr.args]

            if op == 'NOT':
                # Bitwise NOT
                width = self.signal_widths.get(str(expr.args[0].name), 1) if isinstance(expr.args[0], ExprIdentifier) else None
                if width is None:
                    if isinstance(expr.args[0], ExprLiteralVec):
                        width = expr.args[0].width
                    else:
                        width = arg_values[0].bit_length()
                        if width == 0:
                            width = 1
                mask = (1 << width) - 1
                return (~arg_values[0]) & mask

            elif op == 'BUF':
                return arg_values[0]

            elif op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
                # Bitwise operations
                result = arg_values[0]
                for val in arg_values[1:]:
                    if op == 'AND':
                        result &= val
                    elif op == 'OR':
                        result |= val
                    elif op == 'XOR':
                        result ^= val
                    elif op == 'NAND':
                        result &= val
                    elif op == 'NOR':
                        result |= val
                    elif op == 'XNOR':
                        result ^= val
                if op in ('NAND', 'NOR'):
                    # Invert at the end
                    width = arg_values[0].bit_length()
                    if width == 0:
                        width = 1
                    mask = (1 << width) - 1
                    result = (~result) & mask
                elif op == 'XNOR':
                    # Invert at the end
                    width = arg_values[0].bit_length()
                    if width == 0:
                        width = 1
                    mask = (1 << width) - 1
                    result = (~result) & mask
                return result

            elif op == 'MUX' or op == 'ITE':
                sel, a, b = arg_values
                return a if sel else b

            elif op == 'REDUCE_AND':
                val = arg_values[0]
                # AND all bits
                width = val.bit_length()
                if width == 0:
                    width = 1
                mask = (1 << width) - 1
                return 1 if (val & mask) == mask else 0

            elif op == 'REDUCE_OR':
                val = arg_values[0]
                return 1 if val != 0 else 0

            elif op == 'REDUCE_XOR':
                val = arg_values[0]
                # XOR all bits
                result = 0
                while val:
                    result ^= val & 1
                    val >>= 1
                return result

            elif op == 'EQ':
                # Equality: returns 1 if all bits equal, 0 otherwise
                a, b = arg_values
                return 1 if a == b else 0

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