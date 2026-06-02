#!/usr/bin/env python3
"""Circuit optimizer CLI tool - Part 3: Vectors, richer expressions, and --radix."""

import sys
import json
import re
from dataclasses import dataclass, field
from typing import Union, Optional, Tuple, List

VERSION = "1.0.0"

EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INPUT_VALUE_ERROR = 2

VALIDATION_ERRORS = {
    "DeclarationAfterAssignmentError", "DuplicateNameError", "UndefinedNameError",
    "UnassignedSignalError", "InputAssignmentError", "MultipleAssignmentError",
    "ArityError", "CycleError", "WidthMismatchError", "IndexOutOfBoundsError"
}

ERROR_EXIT_CODES = {
    "CircParseError": EXIT_PARSE_ERROR,
    "MissingInputError": 1,
    "UnknownInputError": 1,
    "InputValueParseError": EXIT_INPUT_VALUE_ERROR,
    "InputWidthMismatchError": EXIT_VALIDATION_ERROR,
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

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


# AST Expression types
@dataclass
class Identifier:
    name: str


@dataclass
class Literal:
    value: int          # Integer value
    width: int          # Bit width


@dataclass
class Index:
    """Single bit extraction: expr[index]"""
    expr: 'Expr'
    index: int


@dataclass
class Slice:
    """Bit slice: expr[hi:lo]"""
    expr: 'Expr'
    hi: int
    lo: int


@dataclass
class Concat:
    """Concatenation: {e1, e2, ..., ek} - e1 is MSB"""
    parts: list


@dataclass
class Call:
    operator: str
    args: list


Expr = Union[Identifier, Literal, Index, Slice, Concat, Call]


@dataclass
class Assignment:
    lhs: str
    rhs: Expr
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

    def get_signal(self, name: str) -> Optional[Signal]:
        for sig in self.inputs + self.outputs + self.wires:
            if sig.name == name:
                return sig
        return None


# Operator definitions
# (min_args, max_args) where None means unlimited
OPERATOR_ARITY = {
    'NOT': (1, 1), 'BUF': (1, 1),
    'AND': (2, None), 'OR': (2, None), 'XOR': (2, None),
    'NAND': (2, None), 'NOR': (2, None), 'XNOR': (2, None),
    'MUX': (3, 3), 'ITE': (3, 3),
    'REDUCE_AND': (1, 1), 'REDUCE_OR': (1, 1), 'REDUCE_XOR': (1, 1),
    'EQ': (2, 2),
}


def parse_literal(text: str, filename: str = None, line: int = None, col: int = None) -> Tuple[int, int]:
    """
    Parse a literal value. Returns (value, width).
    Raises CircError on invalid literal.
    """
    original = text
    text = text.replace('_', '')  # Remove underscores

    if text == 'X':
        raise CircError("CircParseError", "Literal 'X' values are not allowed in .circ files", filename, line, col)

    # Unsized scalar literals
    if text in ('0', '1'):
        return int(text), 1

    # Unsized binary: 0b... (must check before sized literals to avoid matching "0b..." as sized)
    if text.lower().startswith('0b'):
        digits = text[2:]
        if not digits:
            raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
        if not re.match(r'^[01]+$', digits):
            raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
        return int(digits, 2), len(digits)

    # Unsized hex: 0x...
    if text.lower().startswith('0x'):
        digits = text[2:]
        if not digits:
            raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
        if not re.match(r'^[0-9a-fA-F]+$', digits):
            raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
        value = int(digits, 16)
        width = len(digits) * 4
        return value, width

    # Sized literals: N'b..., N'h..., N'd... (with apostrophe separator)
    sized_match = re.match(r"^(\d+)'([bhd])([0-9a-fA-F_]+)$", text, re.IGNORECASE)
    if sized_match:
        width = int(sized_match.group(1))
        base = sized_match.group(2).lower()
        digits = sized_match.group(3).replace('_', '')

        if width <= 0:
            raise CircError("CircParseError", f"Invalid literal width: {original}", filename, line, col)

        try:
            if base == 'b':
                if not re.match(r'^[01]+$', digits):
                    raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
                value = int(digits, 2)
            elif base == 'h':
                if not re.match(r'^[0-9a-fA-F]+$', digits):
                    raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
                value = int(digits, 16)
            else:  # base == 'd'
                if not re.match(r'^[0-9]+$', digits):
                    raise CircError("CircParseError", f"Invalid decimal literal: {original}", filename, line, col)
                value = int(digits, 10)
        except ValueError:
            raise CircError("CircParseError", f"Invalid literal: {original}", filename, line, col)

        # Check value fits in width
        max_val = (1 << width) - 1
        if value > max_val:
            raise CircError("CircParseError", f"Sized decimal value {value} exceeds width {width}", filename, line, col)

        return value, width

    raise CircError("CircParseError", f"Invalid literal: {original}", filename, line, col)


def format_value(value: int, width: int, radix: str) -> str:
    """Format a value for output according to radix."""
    if width == 1:
        # Scalar outputs are always 0 or 1
        return str(value & 1)

    if radix == 'bin':
        bits = bin(value)[2:].zfill(width)
        return f"0b{bits}"
    elif radix == 'hex':
        hex_width = (width + 3) // 4
        hex_val = hex(value)[2:].zfill(hex_width)
        return f"0x{hex_val}"
    else:  # dec
        return str(value)


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
        parts = line.split()
        return '=' not in line and bool(parts) and parts[0].upper() in ('INPUT', 'OUTPUT', 'WIRE')

    def _parse_declaration(self, line: str, circuit: Circuit, line_num: int):
        parts = line.split()
        if not parts:
            return

        keyword = parts[0].upper()
        target_map = {'INPUT': circuit.inputs, 'OUTPUT': circuit.outputs, 'WIRE': circuit.wires}
        if keyword not in target_map:
            return
        target = target_map[keyword]

        existing_names = circuit.get_all_names()

        # Parse signal declarations - each part can be a name or name[msb:lsb]
        i = 1
        while i < len(parts):
            part = parts[i]
            # Check for vector declaration: name[msb:lsb]
            vec_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\[(\d+):(\d+)\]$', part)
            if vec_match:
                name = vec_match.group(1)
                msb = int(vec_match.group(2))
                lsb = int(vec_match.group(3))

                if msb < lsb:
                    raise CircError("CircParseError", f"Invalid vector range: msb ({msb}) must be >= lsb ({lsb})",
                                   self.filename, line_num, line.find(part) + 1)
                if lsb < 0:
                    raise CircError("CircParseError", f"Invalid vector range: lsb ({lsb}) must be >= 0",
                                   self.filename, line_num, line.find(part) + 1)

                if name in existing_names:
                    raise CircError("DuplicateNameError", f"Duplicate name: {name}",
                                   self.filename, line_num, line.find(name) + 1)
                target.append(Signal(name, msb, lsb))
                existing_names.add(name)
            else:
                # Scalar signal
                if not self._is_valid_identifier(part):
                    raise CircError("CircParseError", f"Invalid identifier: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                if part in existing_names:
                    raise CircError("DuplicateNameError", f"Duplicate name: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                target.append(Signal(part, 0, 0))
                existing_names.add(part)
            i += 1

    def _is_valid_identifier(self, name: str) -> bool:
        return bool(name) and not name[0].isdigit() and all(c.isalnum() or c == '_' for c in name)

    def _parse_assignment(self, line: str, circuit: Circuit, line_num: int, raw_line: str):
        eq_pos = line.find('=')
        lhs = line[:eq_pos].strip()
        rhs = line[eq_pos + 1:].strip()

        if not lhs:
            raise CircError("CircParseError", "Missing left-hand side in assignment", self.filename, line_num, 1)

        # LHS can be a simple identifier or identifier with index/slice
        lhs_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)$', lhs)
        if not lhs_match:
            raise CircError("CircParseError", f"Invalid left-hand side: {lhs}", self.filename, line_num, 1)

        rhs_expr, _ = self._parse_expression(rhs, line_num, eq_pos + 2)
        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, raw_line.find(lhs) + 1))

    def _parse_expression(self, text: str, line_num: int, start_col: int) -> Tuple[Expr, int]:
        """Parse expression, returns (expr, chars_consumed)."""
        text = text.strip()
        if not text:
            raise CircError("CircParseError", "Empty expression", self.filename, line_num, start_col)

        # Try concatenation first: {e1, e2, ...}
        if text.startswith('{'):
            return self._parse_concatenation(text, line_num, start_col)

        # Try to parse as a complete expression with optional index/slice
        expr, consumed = self._parse_primary_expr(text, line_num, start_col)

        # Check for index or slice suffix
        remaining = text[consumed:].strip()
        while remaining.startswith('['):
            bracket_end = remaining.find(']')
            if bracket_end == -1:
                raise CircError("CircParseError", "Unmatched bracket in index/slice", self.filename, line_num, start_col + consumed)

            bracket_content = remaining[1:bracket_end]

            if ':' in bracket_content:
                # Slice
                parts = bracket_content.split(':')
                if len(parts) != 2:
                    raise CircError("CircParseError", f"Invalid slice: {bracket_content}", self.filename, line_num, start_col + consumed)
                try:
                    hi = int(parts[0].strip())
                    lo = int(parts[1].strip())
                except ValueError:
                    raise CircError("CircParseError", f"Invalid slice indices: {bracket_content}", self.filename, line_num, start_col + consumed)
                expr = Slice(expr, hi, lo)
            else:
                # Index
                try:
                    idx = int(bracket_content.strip())
                except ValueError:
                    raise CircError("CircParseError", f"Invalid index: {bracket_content}", self.filename, line_num, start_col + consumed)
                expr = Index(expr, idx)

            consumed += bracket_end + 1
            remaining = text[consumed:].strip()

        return expr, consumed

    def _try_match_literal(self, text: str):
        """Try to match a literal at the start of text. Returns matched text or None."""
        # Sized literal: N'b..., N'h..., N'd... (e.g., 8'b00001111, 8'hff, 8'd200)
        m = re.match(r"^(\d+'[bhd])", text, re.IGNORECASE)
        if m:
            prefix = m.group(1).lower()
            rest = text[len(m.group(1)):]
            if prefix.endswith('b'):
                digits_m = re.match(r'^([01_]+)', rest)
            elif prefix.endswith('h'):
                digits_m = re.match(r'^([0-9a-fA-F_]+)', rest)
            else:  # d
                digits_m = re.match(r'^([0-9_]+)', rest)
            if digits_m:
                full = m.group(1) + digits_m.group(1)
                # Check next char is not alphanumeric/underscore
                end_pos = len(full)
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return full
                return None

        # Unsized binary: 0b...
        if text.lower().startswith('0b') and len(text) > 2:
            m = re.match(r'^(0b[01_]+)', text, re.IGNORECASE)
            if m:
                end_pos = len(m.group(1))
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return m.group(1)
            return None

        # Unsized hex: 0x...
        if text.lower().startswith('0x') and len(text) > 2:
            m = re.match(r'^(0x[0-9a-fA-F_]+)', text, re.IGNORECASE)
            if m:
                end_pos = len(m.group(1))
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return m.group(1)
            return None

        # Scalar 0 or 1
        if text and text[0] in ('0', '1'):
            if len(text) == 1 or not text[1].isalnum() and text[1] != '_':
                return text[0]

        return None

    def _parse_primary_expr(self, text: str, line_num: int, start_col: int) -> Tuple[Expr, int]:
        """Parse a primary expression (literal, identifier, or call)."""
        text = text.strip()
        if not text:
            raise CircError("CircParseError", "Empty expression", self.filename, line_num, start_col)

        # Check for concatenation
        if text.startswith('{'):
            return self._parse_concatenation(text, line_num, start_col)

        # Check for parenthesized expression
        if text.startswith('('):
            # Find matching paren
            depth = 1
            i = 1
            while i < len(text) and depth > 0:
                if text[i] == '(':
                    depth += 1
                elif text[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                raise CircError("CircParseError", "Unmatched parenthesis", self.filename, line_num, start_col)
            inner = text[1:i-1]
            expr, _ = self._parse_expression(inner, line_num, start_col + 1)
            return expr, i

        # Check for literal - try patterns separately to avoid regex engine issues
        lit_text = self._try_match_literal(text)
        if lit_text is not None:
            value, width = parse_literal(lit_text, self.filename, line_num, start_col)
            return Literal(value, width), len(lit_text)

        # Check for call: IDENTIFIER(...)
        call_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\(', text)
        if call_match:
            op_name = call_match.group(1).upper()
            op_start = call_match.end()

            # Find matching closing paren
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

        # Check for identifier
        id_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)', text)
        if id_match:
            name = id_match.group(1)
            return Identifier(name), len(name)

        raise CircError("CircParseError", f"Invalid expression: {text[:50]}", self.filename, line_num, start_col)

    def _parse_concatenation(self, text: str, line_num: int, start_col: int) -> Tuple[Concat, int]:
        """Parse concatenation: {e1, e2, ...}"""
        if not text.startswith('{'):
            raise CircError("CircParseError", "Expected '{' for concatenation", self.filename, line_num, start_col)

        # Find matching closing brace
        depth = 1
        i = 1
        while i < len(text) and depth > 0:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1

        if depth != 0:
            raise CircError("CircParseError", "Unmatched brace in concatenation", self.filename, line_num, start_col)

        inner = text[1:i-1]
        parts = []
        current = ""
        brace_depth = 0
        paren_depth = 0

        for ch in inner:
            if ch == '{':
                brace_depth += 1
                current += ch
            elif ch == '}':
                brace_depth -= 1
                current += ch
            elif ch == '(':
                paren_depth += 1
                current += ch
            elif ch == ')':
                paren_depth -= 1
                current += ch
            elif ch == ',' and brace_depth == 0 and paren_depth == 0:
                if current.strip():
                    expr, _ = self._parse_expression(current.strip(), line_num, start_col + 1 + len(parts))
                    parts.append(expr)
                current = ""
            else:
                current += ch

        if current.strip():
            expr, _ = self._parse_expression(current.strip(), line_num, start_col + 1 + len(parts))
            parts.append(expr)

        if not parts:
            raise CircError("CircParseError", "Empty concatenation", self.filename, line_num, start_col)

        return Concat(parts), i

    def _parse_arguments(self, text: str, line_num: int, start_col: int) -> list:
        text = text.strip()
        if not text:
            return []

        args = []
        current = ""
        paren_depth = 0
        brace_depth = 0

        for i, char in enumerate(text):
            if char == '(':
                paren_depth += 1
                current += char
            elif char == ')':
                paren_depth -= 1
                current += char
            elif char == '{':
                brace_depth += 1
                current += char
            elif char == '}':
                brace_depth -= 1
                current += char
            elif char == ',' and paren_depth == 0 and brace_depth == 0:
                if current.strip():
                    expr, _ = self._parse_expression(current.strip(), line_num, start_col + i - len(current))
                    args.append(expr)
                current = ""
            else:
                current += char

        if current.strip():
            expr, _ = self._parse_expression(current.strip(), line_num, start_col + len(text) - len(current))
            args.append(expr)

        return args


class Validator:
    def __init__(self, circuit: Circuit, filename: str):
        self.circuit = circuit
        self.filename = filename
        self.signal_types = {sig.name: 'input' for sig in circuit.inputs}
        self.signal_types.update({sig.name: 'output' for sig in circuit.outputs})
        self.signal_types.update({sig.name: 'wire' for sig in circuit.wires})
        self.signal_info = {sig.name: sig for sig in circuit.inputs + circuit.outputs + circuit.wires}

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

            # Validate RHS expression and compute width
            self._check_and_infer_width(asn.rhs, asn.line)

        for sig in self.circuit.outputs + self.circuit.wires:
            if sig.name not in assigned:
                kind = "Output" if self.signal_types.get(sig.name) == 'output' else "Wire"
                raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", self.filename)

    def _check_and_infer_width(self, expr, line_num: int) -> int:
        """Check expression validity and return its width."""
        if isinstance(expr, Literal):
            return expr.width

        if isinstance(expr, Identifier):
            if expr.name not in self.circuit.get_all_names():
                raise CircError("UndefinedNameError", f"Undefined signal: {expr.name}", self.filename, line_num)
            sig = self.signal_info[expr.name]
            return sig.width

        if isinstance(expr, Index):
            base_width = self._check_and_infer_width(expr.expr, line_num)
            # Check index is in bounds
            sig = self._get_base_signal(expr.expr)
            if sig:
                if expr.index < sig.lsb or expr.index > sig.msb:
                    raise CircError("IndexOutOfBoundsError",
                                   f"Index {expr.index} out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                                   self.filename, line_num)
            else:
                # For non-signal expressions, check against computed width
                if expr.index < 0 or expr.index >= base_width:
                    raise CircError("IndexOutOfBoundsError",
                                   f"Index {expr.index} out of bounds for expression of width {base_width}",
                                   self.filename, line_num)
            return 1

        if isinstance(expr, Slice):
            base_width = self._check_and_infer_width(expr.expr, line_num)
            if expr.hi < expr.lo:
                raise CircError("CircParseError", f"Invalid slice: hi ({expr.hi}) < lo ({expr.lo})",
                               self.filename, line_num)
            # Check slice bounds
            sig = self._get_base_signal(expr.expr)
            if sig:
                if expr.lo < sig.lsb or expr.hi > sig.msb:
                    raise CircError("IndexOutOfBoundsError",
                                   f"Slice [{expr.hi}:{expr.lo}] out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                                   self.filename, line_num)
            else:
                # For non-signal expressions
                if expr.lo < 0 or expr.hi >= base_width:
                    raise CircError("IndexOutOfBoundsError",
                                   f"Slice [{expr.hi}:{expr.lo}] out of bounds for expression of width {base_width}",
                                   self.filename, line_num)
            return expr.hi - expr.lo + 1

        if isinstance(expr, Concat):
            total_width = 0
            for part in expr.parts:
                total_width += self._check_and_infer_width(part, line_num)
            return total_width

        if isinstance(expr, Call):
            self._check_operator_arity(expr, line_num)
            return self._check_operator_width(expr, line_num)

        raise RuntimeError(f"Unknown expression type: {type(expr)}")

    def _get_base_signal(self, expr) -> Optional[Signal]:
        """Get the base signal for an expression, if any."""
        if isinstance(expr, Identifier):
            return self.signal_info.get(expr.name)
        return None

    def _check_operator_arity(self, call: Call, line_num: int):
        op = call.operator
        num_args = len(call.args)

        if op not in OPERATOR_ARITY:
            raise CircError("UndefinedNameError", f"Unknown operator: {op}", self.filename, line_num)

        min_args, max_args = OPERATOR_ARITY[op]
        if num_args < min_args or (max_args and num_args > max_args):
            if max_args == min_args:
                req = f"exactly {min_args}"
            elif max_args is None:
                req = f"at least {min_args}"
            else:
                req = f"between {min_args} and {max_args}"
            plural = "s" if min_args > 1 or (max_args and max_args > 1) else ""
            raise CircError("ArityError", f"Operator {op} requires {req} argument{plural}, got {num_args}", self.filename, line_num)

    def _check_operator_width(self, call: Call, line_num: int) -> int:
        """Check width constraints for operators and return result width."""
        op = call.operator
        arg_widths = [self._check_and_infer_width(arg, line_num) for arg in call.args]

        if op in ('NOT', 'BUF'):
            return arg_widths[0]

        if op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            # All operands must have same width
            for i, w in enumerate(arg_widths[1:], 1):
                if w != arg_widths[0]:
                    raise CircError("WidthMismatchError",
                                   f"Width mismatch in {op}: operand 0 has width {arg_widths[0]}, operand {i} has width {w}",
                                   self.filename, line_num)
            return arg_widths[0]

        if op in ('MUX', 'ITE'):
            # sel must have width 1, a and b must have same width
            if arg_widths[0] != 1:
                raise CircError("WidthMismatchError",
                               f"Selector in {op} must have width 1, got width {arg_widths[0]}",
                               self.filename, line_num)
            if arg_widths[1] != arg_widths[2]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in {op}: operand 1 has width {arg_widths[1]}, operand 2 has width {arg_widths[2]}",
                               self.filename, line_num)
            return arg_widths[1]

        if op in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'):
            # Operand width must be >= 1 (always true for valid expressions)
            return 1

        if op == 'EQ':
            # Operands must have same width
            if arg_widths[0] != arg_widths[1]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in EQ: operand 0 has width {arg_widths[0]}, operand 1 has width {arg_widths[1]}",
                               self.filename, line_num)
            return 1

        raise RuntimeError(f"Unknown operator: {op}")

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
        if isinstance(expr, (Index, Slice)):
            return self._get_dependencies(expr.expr)
        if isinstance(expr, Concat):
            deps = set()
            for part in expr.parts:
                deps.update(self._get_dependencies(part))
            return deps
        if isinstance(expr, Call):
            deps = set()
            for arg in expr.args:
                deps.update(self._get_dependencies(arg))
            return deps
        return set()


class Evaluator:
    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self.values = {}  # name -> int value

    def evaluate(self, inputs: dict) -> dict:
        """Evaluate circuit with given input values. Returns output values."""
        self.values = dict(inputs)
        for asn in self.circuit.assignments:
            self.values[asn.lhs] = self._eval_expr(asn.rhs)
        return {sig.name: self.values[sig.name] for sig in self.circuit.outputs}

    def _eval_expr(self, expr) -> int:
        if isinstance(expr, Literal):
            return expr.value

        if isinstance(expr, Identifier):
            return self.values[expr.name]

        if isinstance(expr, Index):
            base_val = self._eval_expr(expr.expr)
            # Get bit at position (0-indexed from LSB)
            return (base_val >> expr.index) & 1

        if isinstance(expr, Slice):
            base_val = self._eval_expr(expr.expr)
            # Extract bits from lo to hi (inclusive)
            width = expr.hi - expr.lo + 1
            mask = (1 << width) - 1
            return (base_val >> expr.lo) & mask

        if isinstance(expr, Concat):
            result = 0
            for part in expr.parts:
                val = self._eval_expr(part)
                # Get width of this part
                width = self._get_expr_width(part)
                result = (result << width) | (val & ((1 << width) - 1))
            return result

        if isinstance(expr, Call):
            args = [self._eval_expr(arg) for arg in expr.args]
            return self._apply_operator(expr.operator, args, expr)

        raise RuntimeError(f"Unknown expression type: {type(expr)}")

    def _get_expr_width(self, expr) -> int:
        """Get the width of an expression for concatenation."""
        if isinstance(expr, Literal):
            return expr.width
        if isinstance(expr, Identifier):
            sig = self.circuit.get_signal(expr.name)
            return sig.width if sig else 1
        if isinstance(expr, Index):
            return 1
        if isinstance(expr, Slice):
            return expr.hi - expr.lo + 1
        if isinstance(expr, Concat):
            return sum(self._get_expr_width(p) for p in expr.parts)
        if isinstance(expr, Call):
            return self._get_operator_result_width(expr)
        return 1

    def _get_operator_result_width(self, call: Call) -> int:
        """Get the result width of an operator call."""
        op = call.operator
        if op in ('NOT', 'BUF', 'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            return self._get_expr_width(call.args[0])
        if op in ('MUX', 'ITE'):
            return self._get_expr_width(call.args[1])
        if op in ('REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR', 'EQ'):
            return 1
        return 1

    def _apply_operator(self, op: str, args: list, call: Call) -> int:
        if op == 'NOT':
            width = self._get_expr_width(call.args[0])
            mask = (1 << width) - 1
            return (~args[0]) & mask

        if op == 'BUF':
            return args[0]

        if op in ('AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'):
            width = self._get_expr_width(call.args[0])
            mask = (1 << width) - 1
            result = args[0]
            for a in args[1:]:
                if op in ('AND', 'NAND'):
                    result = result & a
                elif op in ('OR', 'NOR'):
                    result = result | a
                elif op in ('XOR', 'XNOR'):
                    result = result ^ a
            if op in ('NAND', 'NOR', 'XNOR'):
                result = (~result) & mask
            return result

        if op in ('MUX', 'ITE'):
            # sel=1 -> a, sel=0 -> b
            sel, a, b = args[0], args[1], args[2]
            return a if sel else b

        if op == 'REDUCE_AND':
            val = args[0]
            width = self._get_expr_width(call.args[0])
            # Check if all bits are 1
            mask = (1 << width) - 1
            return 1 if (val & mask) == mask else 0

        if op == 'REDUCE_OR':
            val = args[0]
            return 1 if val != 0 else 0

        if op == 'REDUCE_XOR':
            val = args[0]
            # XOR all bits
            result = 0
            while val:
                result ^= (val & 1)
                val >>= 1
            return result

        if op == 'EQ':
            return 1 if args[0] == args[1] else 0

        raise RuntimeError(f"Unknown operator: {op}")


def output_json(data: dict):
    print(json.dumps(data, separators=(',', ':')))


def load_circuit(filename: str, command: str, json_mode: bool):
    """Read, parse, and validate a circuit file. Returns Circuit or exit code int."""
    try:
        with open(filename, 'r') as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        return output_error(CircError("FileNotFoundError", f"Cannot read file: {filename}", filename), command, json_mode)

    try:
        circuit = Parser(content, filename).parse()
        Validator(circuit, filename).validate()
    except CircError as e:
        return output_error(e, command, json_mode)

    return circuit


def signal_list(signals):
    return [{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in sorted(signals, key=lambda s: s.name)]


def get_exit_code(error_type: str) -> int:
    if error_type in VALIDATION_ERRORS:
        return EXIT_VALIDATION_ERROR
    return ERROR_EXIT_CODES.get(error_type, 1)


def output_error(error, command: str, json_mode: bool) -> int:
    exit_code = get_exit_code(error.error_type)
    if json_mode:
        output_json({"ok": False, "command": command, "exit_code": exit_code, "error": error.to_dict()})
    else:
        loc = ""
        if hasattr(error, 'file') and error.file:
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
        return output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)

    filename = args[0]
    result = load_circuit(filename, "check", json_mode)
    if isinstance(result, int):
        return result
    circuit = result

    inputs = signal_list(circuit.inputs)
    outputs = signal_list(circuit.outputs)

    if json_mode:
        output_json({"ok": True, "command": "check", "format": "circ", "inputs": inputs, "outputs": outputs})
    else:
        print("Circuit is valid.")
        print(f"Inputs: {', '.join(s['name'] for s in inputs)}")
        print(f"Outputs: {', '.join(s['name'] for s in outputs)}")

    return 0


def parse_input_value(value_str: str, expected_width: int, signal_name: str, filename: str = None) -> int:
    """Parse an input value and validate its width matches expected."""
    try:
        value, width = parse_literal(value_str, filename)
    except CircError as e:
        raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {value_str}")

    if width != expected_width:
        raise EvalError("InputWidthMismatchError",
                       f"Input width mismatch for '{signal_name}': expected width {expected_width}, got width {width}")

    return value


def eval_command(args: list, json_mode: bool) -> int:
    if not args:
        return output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)

    filename = args[0]

    set_values = {}
    default_value = None
    allow_extra = False
    radix = 'bin'

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
            set_values[name] = value
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
        elif arg == '--radix':
            if i + 1 >= len(args):
                return output_error(CircError("CliUsageError", "--radix requires a value (bin, hex, or dec)"), "eval", json_mode)
            radix_arg = args[i + 1].lower()
            if radix_arg not in ('bin', 'hex', 'dec'):
                return output_error(CircError("CliUsageError", f"Invalid --radix value: {radix_arg}, must be bin, hex, or dec"), "eval", json_mode)
            radix = radix_arg
            i += 2
        elif '=' in arg:
            # Bare name=value argument
            name, value = arg.split('=', 1)
            set_values[name] = value
            i += 1
        else:
            return output_error(CircError("CliUsageError", f"Unknown option: {arg}"), "eval", json_mode)

    result = load_circuit(filename, "eval", json_mode)
    if isinstance(result, int):
        return result
    circuit = result

    # Build signal info
    signal_info = {sig.name: sig for sig in circuit.inputs}
    input_names = set(signal_info.keys())

    # Check for unknown inputs
    for name in set_values:
        if name not in input_names and not allow_extra:
            return output_error(EvalError("UnknownInputError", f"Unknown input: {name}"), "eval", json_mode)

    # Check for missing inputs
    missing_inputs = input_names - set(set_values.keys())
    if missing_inputs and default_value is None:
        return output_error(EvalError("MissingInputError", f"Missing input values for: {', '.join(sorted(missing_inputs))}"), "eval", json_mode)

    # Parse and validate input values
    inputs = {}
    try:
        for sig in circuit.inputs:
            if sig.name in set_values:
                value = parse_input_value(set_values[sig.name], sig.width, sig.name, filename)
                inputs[sig.name] = value
            elif default_value is not None:
                # For scalar inputs, default is just 0 or 1
                if sig.width == 1:
                    inputs[sig.name] = default_value
                else:
                    # For vector inputs with default, fill all bits
                    inputs[sig.name] = default_value * ((1 << sig.width) - 1) if default_value else 0
    except EvalError as e:
        return output_error(e, "eval", json_mode)

    try:
        results = Evaluator(circuit).evaluate(inputs)
    except Exception as e:
        return output_error(CircError("InternalError", str(e)), "eval", json_mode)

    # Format output
    sorted_outputs = sorted(results.items(), key=lambda x: x[0])

    if json_mode:
        output_list = []
        for name, value in sorted_outputs:
            sig = circuit.get_signal(name)
            output_list.append({
                "name": name,
                "msb": sig.msb if sig else 0,
                "lsb": sig.lsb if sig else 0,
                "value": format_value(value, sig.width if sig else 1, radix)
            })
        output_json({
            "ok": True,
            "command": "eval",
            "mode": "2val",
            "radix": radix,
            "inputs": [{"name": s.name, "msb": s.msb, "lsb": s.lsb} for s in sorted(circuit.inputs, key=lambda x: x.name)],
            "outputs": output_list
        })
    else:
        for name, value in sorted_outputs:
            sig = circuit.get_signal(name)
            width = sig.width if sig else 1
            formatted = format_value(value, width, radix)
            print(f"{name}={formatted}")

    return 0


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
        return 0

    if '--version' in args:
        json_mode = '--json' in args
        if json_mode:
            output_json({"ok": True, "command": "__version__", "version": VERSION})
        else:
            print(VERSION)
        return 0

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
