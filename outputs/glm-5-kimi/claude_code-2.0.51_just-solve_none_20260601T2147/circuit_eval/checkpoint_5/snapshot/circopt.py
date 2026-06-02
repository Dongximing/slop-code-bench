#!/usr/bin/env python3
"""Circuit optimizer CLI tool - Part 5: JSON and BENCH input formats."""

import sys
import json
import re
from dataclasses import dataclass, field
from typing import Tuple

VERSION = "1.0.0"

EXIT_PARSE_ERROR = 2
EXIT_VALIDATION_ERROR = 3
EXIT_INPUT_VALUE_ERROR = 2

VALIDATION_ERRORS = {
    "DeclarationAfterAssignmentError", "DuplicateNameError", "UndefinedNameError",
    "UnassignedSignalError", "InputAssignmentError", "MultipleAssignmentError",
    "ArityError", "CycleError", "WidthMismatchError", "IndexOutOfBoundsError",
    "RedefinitionError", "JsonSchemaError",
}

ERROR_EXIT_CODES = {
    "CircParseError": EXIT_PARSE_ERROR,
    "JsonParseError": EXIT_PARSE_ERROR,
    "BenchParseError": EXIT_PARSE_ERROR,
    "MissingInputError": 1,
    "UnknownInputError": 1,
    "UnknownInputFormatError": 1,
    "InputValueParseError": EXIT_INPUT_VALUE_ERROR,
    "InputWidthMismatchError": EXIT_VALIDATION_ERROR,
    "RadixNotAllowedIn3ValError": 1,
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


@dataclass
class Identifier:
    name: str


@dataclass
class Literal:
    value: int
    width: int


@dataclass
class Index:
    expr: 'Expr'
    index: int


@dataclass
class Slice:
    expr: 'Expr'
    hi: int
    lo: int


@dataclass
class Concat:
    parts: list


@dataclass
class Call:
    operator: str
    args: list


Expr = (Identifier, Literal, Index, Slice, Concat, Call)


class TriValue:
    """3-valued logic value using two masks:
    - known_mask: 1 = bit is known, 0 = bit is unknown (X)
    - value_mask: the value for known bits (meaningful only where known_mask bit is 1)
    """

    def __init__(self, value_mask: int = 0, known_mask: int = None, width: int = 1):
        self.value_mask = value_mask
        self.known_mask = (1 << width) - 1 if known_mask is None else known_mask
        self.width = width

    @property
    def is_fully_known(self) -> bool:
        return self.known_mask == (1 << self.width) - 1

    @property
    def has_unknown(self) -> bool:
        return self.known_mask != (1 << self.width) - 1

    def get_bit(self, bit: int) -> str:
        if not (self.known_mask >> bit) & 1:
            return 'X'
        return '1' if (self.value_mask >> bit) & 1 else '0'

    def to_int(self) -> int:
        if self.has_unknown:
            raise ValueError("Cannot convert TriValue with unknown bits to int")
        return self.value_mask

    def __repr__(self):
        return f"TriValue(value={self.value_mask}, known={self.known_mask}, width={self.width})"

    @staticmethod
    def from_int(value: int, width: int) -> 'TriValue':
        return TriValue(value, (1 << width) - 1, width)

    @staticmethod
    def from_bit(bit: str, width: int = 1) -> 'TriValue':
        if bit.upper() == 'X':
            return TriValue(0, 0, width)
        return TriValue(int(bit), 1, width)

    def copy(self) -> 'TriValue':
        return TriValue(self.value_mask, self.known_mask, self.width)

    def with_width(self, width: int) -> 'TriValue':
        if width == self.width:
            return self.copy()
        clip = (1 << min(width, self.width)) - 1
        return TriValue(self.value_mask & clip, self.known_mask & clip, width)

    @staticmethod
    def not_(a: 'TriValue') -> 'TriValue':
        return TriValue((~a.value_mask) & a.known_mask, a.known_mask, a.width)

    @staticmethod
    def and2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        a_known0 = a.known_mask & ~a.value_mask
        b_known0 = b.known_mask & ~b.value_mask
        result_known0 = a_known0 | b_known0

        a_known1 = a.known_mask & a.value_mask
        b_known1 = b.known_mask & b.value_mask
        result_known1 = a_known1 & b_known1

        return TriValue(result_known1, result_known0 | result_known1, a.width)

    @staticmethod
    def or2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        a_known1 = a.known_mask & a.value_mask
        b_known1 = b.known_mask & b.value_mask
        result_known1 = a_known1 | b_known1

        a_known0 = a.known_mask & ~a.value_mask
        b_known0 = b.known_mask & ~b.value_mask
        result_known0 = a_known0 & b_known0

        return TriValue(result_known1, result_known0 | result_known1, a.width)

    @staticmethod
    def xor2(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        both_known = a.known_mask & b.known_mask
        return TriValue((a.value_mask ^ b.value_mask) & both_known, both_known, a.width)

    @staticmethod
    def mux(sel: 'TriValue', a: 'TriValue', b: 'TriValue') -> 'TriValue':
        if sel.known_mask & 1:
            return a.copy() if sel.value_mask & 1 else b.copy()

        both_known = a.known_mask & b.known_mask
        same_value = both_known & ~(a.value_mask ^ b.value_mask)
        return TriValue((a.value_mask | b.value_mask) & same_value, same_value, a.width)

    @staticmethod
    def eq(a: 'TriValue', b: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        both_known = a.known_mask & b.known_mask

        diff_bits = both_known & (a.value_mask ^ b.value_mask)
        if diff_bits:
            return TriValue(0, 1, 1)

        if both_known == mask and not (a.value_mask ^ b.value_mask):
            return TriValue(1, 1, 1)

        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_and(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask & ~a.value_mask:
            return TriValue(0, 1, 1)
        if a.known_mask == mask and (a.value_mask & mask) == mask:
            return TriValue(1, 1, 1)
        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_or(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask & a.value_mask:
            return TriValue(1, 1, 1)
        if a.known_mask == mask and (a.value_mask & mask) == 0:
            return TriValue(0, 1, 1)
        return TriValue(0, 0, 1)

    @staticmethod
    def reduce_xor(a: 'TriValue') -> 'TriValue':
        mask = (1 << a.width) - 1
        if a.known_mask != mask:
            return TriValue(0, 0, 1)
        return TriValue(bin(a.value_mask & mask).count('1') % 2, 1, 1)


OPERATOR_ARITY = {
    'NOT': (1, 1), 'BUF': (1, 1),
    'AND': (2, None), 'OR': (2, None), 'XOR': (2, None),
    'NAND': (2, None), 'NOR': (2, None), 'XNOR': (2, None),
    'MUX': (3, 3), 'ITE': (3, 3),
    'REDUCE_AND': (1, 1), 'REDUCE_OR': (1, 1), 'REDUCE_XOR': (1, 1),
    'EQ': (2, 2),
}

_UNARY_PROPAGATE = frozenset({'NOT', 'BUF'})
_BINARY_PROPAGATE = frozenset({'AND', 'OR', 'XOR', 'NAND', 'NOR', 'XNOR'})
_SELECT = frozenset({'MUX', 'ITE'})
_REDUCE = frozenset({'REDUCE_AND', 'REDUCE_OR', 'REDUCE_XOR'})

_BINARY_OPS = {
    'AND': TriValue.and2, 'NAND': TriValue.and2,
    'OR': TriValue.or2, 'NOR': TriValue.or2,
    'XOR': TriValue.xor2, 'XNOR': TriValue.xor2,
}
_NEGATED = frozenset({'NAND', 'NOR', 'XNOR'})


def _all_signal_widths(circuit) -> dict:
    return {sig.name: sig.width for sig in circuit.inputs + circuit.outputs + circuit.wires}


def _expr_width(expr, signal_widths: dict) -> int:
    if isinstance(expr, Literal):
        return expr.width
    if isinstance(expr, Identifier):
        return signal_widths[expr.name]
    if isinstance(expr, Index):
        return 1
    if isinstance(expr, Slice):
        return expr.hi - expr.lo + 1
    if isinstance(expr, Concat):
        return sum(_expr_width(p, signal_widths) for p in expr.parts)
    if isinstance(expr, Call):
        op = expr.operator
        if op in _UNARY_PROPAGATE | _BINARY_PROPAGATE:
            return _expr_width(expr.args[0], signal_widths)
        if op in _SELECT:
            return _expr_width(expr.args[1], signal_widths)
        if op in _REDUCE | {'EQ'}:
            return 1
        raise RuntimeError(f"Unknown operator: {op}")
    raise RuntimeError(f"Unknown expression type: {type(expr)}")


def _collect_dependencies(expr) -> set:
    if isinstance(expr, Identifier):
        return {expr.name}
    if isinstance(expr, (Index, Slice)):
        return _collect_dependencies(expr.expr)
    if isinstance(expr, Concat):
        deps = set()
        for part in expr.parts:
            deps.update(_collect_dependencies(part))
        return deps
    if isinstance(expr, Call):
        deps = set()
        for arg in expr.args:
            deps.update(_collect_dependencies(arg))
        return deps
    return set()


def parse_literal(text: str, filename: str = None, line: int = None, col: int = None) -> Tuple[int, int]:
    original = text
    text = text.replace('_', '')

    if text == 'X':
        raise CircError("CircParseError", "Literal 'X' values are not allowed in .circ files", filename, line, col)

    if text in ('0', '1'):
        return int(text), 1

    if text.lower().startswith('0b'):
        digits = text[2:]
        if not digits:
            raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
        if not re.match(r'^[01]+$', digits):
            raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
        return int(digits, 2), len(digits)

    if text.lower().startswith('0x'):
        digits = text[2:]
        if not digits:
            raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
        if not re.match(r'^[0-9a-fA-F]+$', digits):
            raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
        value = int(digits, 16)
        return value, len(digits) * 4

    sized_match = re.match(r"^(\d+)'([bhd])([0-9a-fA-F_]+)$", text, re.IGNORECASE)
    if sized_match:
        width = int(sized_match.group(1))
        base = sized_match.group(2).lower()
        digits = sized_match.group(3).replace('_', '')

        if width <= 0:
            raise CircError("CircParseError", f"Invalid literal width: {original}", filename, line, col)

        base_validators = {
            'b': (r'^[01]+$', 2, f"Invalid binary literal: {original}"),
            'h': (r'^[0-9a-fA-F]+$', 16, f"Invalid hex literal: {original}"),
            'd': (r'^[0-9]+$', 10, f"Invalid decimal literal: {original}"),
        }
        pattern, base_int, err_msg = base_validators[base]
        if not re.match(pattern, digits):
            raise CircError("CircParseError", err_msg, filename, line, col)
        try:
            value = int(digits, base_int)
        except ValueError:
            raise CircError("CircParseError", f"Invalid literal: {original}", filename, line, col)

        if value > (1 << width) - 1:
            raise CircError("CircParseError", f"Sized decimal value {value} exceeds width {width}", filename, line, col)
        return value, width

    raise CircError("CircParseError", f"Invalid literal: {original}", filename, line, col)


def parse_3val_input(value_str: str, expected_width: int, signal_name: str, filename: str = None) -> TriValue:
    original = value_str
    text = value_str.replace('_', '')

    if text in ('0', '1'):
        return TriValue.from_int(int(text), expected_width)
    if text.upper() == 'X':
        return TriValue(0, 0, expected_width)

    if text.lower().startswith('0b'):
        digits = text[2:]
        if not digits or not re.match(r'^[01Xx]+$', digits):
            raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
        if len(digits) != expected_width:
            raise EvalError("InputWidthMismatchError",
                           f"Input width mismatch for '{signal_name}': expected width {expected_width}, got width {len(digits)}")
        return _parse_binary_with_x(digits, expected_width)

    if text.lower().startswith('0x'):
        digits = text[2:]
        if not digits or 'x' in digits.lower() or not re.match(r'^[0-9a-fA-F]+$', digits):
            raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
        width = len(digits) * 4
        if width != expected_width:
            raise EvalError("InputWidthMismatchError",
                           f"Input width mismatch for '{signal_name}': expected width {expected_width}, got width {width}")
        return TriValue.from_int(int(digits, 16), expected_width)

    sized_match = re.match(r"^(\d+)'([bhd])([0-9a-fA-FxX_]+)$", text, re.IGNORECASE)
    if sized_match:
        width = int(sized_match.group(1))
        base = sized_match.group(2).lower()
        digits = sized_match.group(3).replace('_', '')

        if width <= 0:
            raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
        if width != expected_width:
            raise EvalError("InputWidthMismatchError",
                           f"Input width mismatch for '{signal_name}': expected width {expected_width}, got width {width}")

        if base == 'b':
            if not re.match(r'^[01Xx]+$', digits):
                raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
            return _parse_binary_with_x(digits, width)

        if 'x' in digits.lower():
            raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")

        if base == 'h':
            if not re.match(r'^[0-9a-fA-F]+$', digits):
                raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
            value = int(digits, 16)
        else:
            if not re.match(r'^[0-9]+$', digits):
                raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
            try:
                value = int(digits, 10)
            except ValueError:
                raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")

        if value > (1 << width) - 1:
            raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")
        return TriValue.from_int(value, width)

    raise EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")


def _parse_binary_with_x(digits: str, width: int) -> TriValue:
    value_mask = 0
    known_mask = 0
    for i, digit in enumerate(digits):
        bit_pos = width - 1 - i
        if digit.upper() != 'X':
            known_mask |= (1 << bit_pos)
            if digit == '1':
                value_mask |= (1 << bit_pos)
    return TriValue(value_mask, known_mask, width)


def format_trivalue(value: TriValue) -> str:
    if value.width == 1:
        if not (value.known_mask & 1):
            return 'X'
        return '1' if (value.value_mask & 1) else '0'
    return '0b' + ''.join(value.get_bit(i) for i in range(value.width - 1, -1, -1))


def format_value(value: int, width: int, radix: str) -> str:
    if width == 1:
        return str(value & 1)
    if radix == 'bin':
        return f"0b{bin(value)[2:].zfill(width)}"
    if radix == 'hex':
        hex_width = (width + 3) // 4
        return f"0x{hex(value)[2:].zfill(hex_width)}"
    return str(value)


def _split_comma_separated(text: str) -> list:
    parts = []
    current = ""
    brace_depth = 0
    paren_depth = 0
    for ch in text:
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
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


class Parser:
    def __init__(self, content: str, filename: str):
        self.filename = filename
        self.lines = content.split('\n')

    def parse(self) -> 'Circuit':
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

    def _parse_declaration(self, line: str, circuit: 'Circuit', line_num: int):
        parts = line.split()
        if not parts:
            return
        keyword = parts[0].upper()
        target_map = {'INPUT': circuit.inputs, 'OUTPUT': circuit.outputs, 'WIRE': circuit.wires}
        if keyword not in target_map:
            return
        target = target_map[keyword]
        existing_names = circuit.get_all_names()
        for part in parts[1:]:
            vec_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\[(\d+):(\d+)\]$', part)
            if vec_match:
                name, msb, lsb = vec_match.group(1), int(vec_match.group(2)), int(vec_match.group(3))
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
                if not self._is_valid_identifier(part):
                    raise CircError("CircParseError", f"Invalid identifier: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                if part in existing_names:
                    raise CircError("DuplicateNameError", f"Duplicate name: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                target.append(Signal(part, 0, 0))
                existing_names.add(part)

    @staticmethod
    def _is_valid_identifier(name: str) -> bool:
        return bool(name) and not name[0].isdigit() and all(c.isalnum() or c == '_' for c in name)

    def _parse_assignment(self, line: str, circuit: 'Circuit', line_num: int, raw_line: str):
        eq_pos = line.find('=')
        lhs = line[:eq_pos].strip()
        rhs = line[eq_pos + 1:].strip()
        if not lhs:
            raise CircError("CircParseError", "Missing left-hand side in assignment", self.filename, line_num, 1)
        if not re.match(r'^([A-Za-z_][A-Za-z0-9_]*)$', lhs):
            raise CircError("CircParseError", f"Invalid left-hand side: {lhs}", self.filename, line_num, 1)
        rhs_expr, _ = self._parse_expression(rhs, line_num, eq_pos + 2)
        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, raw_line.find(lhs) + 1))

    def _parse_expression(self, text: str, line_num: int, start_col: int) -> Tuple[object, int]:
        text = text.strip()
        if not text:
            raise CircError("CircParseError", "Empty expression", self.filename, line_num, start_col)
        if text.startswith('{'):
            return self._parse_concatenation(text, line_num, start_col)
        expr, consumed = self._parse_primary_expr(text, line_num, start_col)
        remaining = text[consumed:].strip()
        while remaining.startswith('['):
            bracket_end = remaining.find(']')
            if bracket_end == -1:
                raise CircError("CircParseError", "Unmatched bracket in index/slice", self.filename, line_num, start_col + consumed)
            bracket_content = remaining[1:bracket_end]
            if ':' in bracket_content:
                parts = bracket_content.split(':')
                if len(parts) != 2:
                    raise CircError("CircParseError", f"Invalid slice: {bracket_content}", self.filename, line_num, start_col + consumed)
                try:
                    hi, lo = int(parts[0].strip()), int(parts[1].strip())
                except ValueError:
                    raise CircError("CircParseError", f"Invalid slice indices: {bracket_content}", self.filename, line_num, start_col + consumed)
                expr = Slice(expr, hi, lo)
            else:
                try:
                    idx = int(bracket_content.strip())
                except ValueError:
                    raise CircError("CircParseError", f"Invalid index: {bracket_content}", self.filename, line_num, start_col + consumed)
                expr = Index(expr, idx)
            consumed += bracket_end + 1
            remaining = text[consumed:].strip()
        return expr, consumed

    def _try_match_literal(self, text: str):
        m = re.match(r"^(\d+'[bhd])", text, re.IGNORECASE)
        if m:
            prefix = m.group(1).lower()
            rest = text[len(m.group(1)):]
            if prefix.endswith('b'):
                digits_m = re.match(r'^([01_]+)', rest)
            elif prefix.endswith('h'):
                digits_m = re.match(r'^([0-9a-fA-F_]+)', rest)
            else:
                digits_m = re.match(r'^([0-9_]+)', rest)
            if digits_m:
                full = m.group(1) + digits_m.group(1)
                end_pos = len(full)
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return full
            return None
        if text.lower().startswith('0b') and len(text) > 2:
            m = re.match(r'^(0b[01_]+)', text, re.IGNORECASE)
            if m:
                end_pos = len(m.group(1))
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return m.group(1)
            return None
        if text.lower().startswith('0x') and len(text) > 2:
            m = re.match(r'^(0x[0-9a-fA-F_]+)', text, re.IGNORECASE)
            if m:
                end_pos = len(m.group(1))
                if end_pos >= len(text) or not text[end_pos].isalnum() and text[end_pos] != '_':
                    return m.group(1)
            return None
        if text and text[0] in ('0', '1'):
            if len(text) == 1 or not text[1].isalnum() and text[1] != '_':
                return text[0]
        return None

    def _parse_primary_expr(self, text: str, line_num: int, start_col: int) -> Tuple[object, int]:
        text = text.strip()
        if not text:
            raise CircError("CircParseError", "Empty expression", self.filename, line_num, start_col)
        if text.startswith('{'):
            return self._parse_concatenation(text, line_num, start_col)
        if text.startswith('('):
            depth, i = 1, 1
            while i < len(text) and depth > 0:
                if text[i] == '(':
                    depth += 1
                elif text[i] == ')':
                    depth -= 1
                i += 1
            if depth != 0:
                raise CircError("CircParseError", "Unmatched parenthesis", self.filename, line_num, start_col)
            expr, _ = self._parse_expression(text[1:i-1], line_num, start_col + 1)
            return expr, i
        lit_text = self._try_match_literal(text)
        if lit_text is not None:
            value, width = parse_literal(lit_text, self.filename, line_num, start_col)
            return Literal(value, width), len(lit_text)
        call_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*\(', text)
        if call_match:
            op_name = call_match.group(1).upper()
            op_start = call_match.end()
            paren_count, i = 1, op_start
            while i < len(text) and paren_count > 0:
                if text[i] == '(':
                    paren_count += 1
                elif text[i] == ')':
                    paren_count -= 1
                i += 1
            if paren_count != 0:
                raise CircError("CircParseError", "Unmatched parenthesis in expression", self.filename, line_num, start_col)
            args = self._parse_arguments(text[op_start:i-1], line_num, start_col + op_start)
            return Call(op_name, args), i
        id_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)', text)
        if id_match:
            name = id_match.group(1)
            return Identifier(name), len(name)
        raise CircError("CircParseError", f"Invalid expression: {text[:50]}", self.filename, line_num, start_col)

    def _parse_concatenation(self, text: str, line_num: int, start_col: int) -> Tuple[Concat, int]:
        if not text.startswith('{'):
            raise CircError("CircParseError", "Expected '{' for concatenation", self.filename, line_num, start_col)
        depth, i = 1, 1
        while i < len(text) and depth > 0:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        if depth != 0:
            raise CircError("CircParseError", "Unmatched brace in concatenation", self.filename, line_num, start_col)
        inner = text[1:i-1]
        raw_parts = _split_comma_separated(inner)
        if not raw_parts:
            raise CircError("CircParseError", "Empty concatenation", self.filename, line_num, start_col)
        parts = [self._parse_expression(p, line_num, start_col + 1 + idx)[0] for idx, p in enumerate(raw_parts)]
        return Concat(parts), i

    def _parse_arguments(self, text: str, line_num: int, start_col: int) -> list:
        text = text.strip()
        if not text:
            return []
        raw_parts = _split_comma_separated(text)
        args = []
        offset = 0
        for raw in raw_parts:
            idx = text.index(raw, offset)
            expr, _ = self._parse_expression(raw, line_num, start_col + idx)
            args.append(expr)
            offset = idx + len(raw)
        return args


@dataclass
class Assignment:
    lhs: str
    rhs: object
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

    def get_signal(self, name: str):
        for sig in self.inputs + self.outputs + self.wires:
            if sig.name == name:
                return sig
        return None


def parse_json_circuit(content: str, filename: str) -> Circuit:
    """Parse a JSON circuit file."""
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise CircError("JsonParseError", f"Invalid JSON: {e.msg}", filename, e.lineno, e.colno)

    # Validate schema
    if not isinstance(data, dict):
        raise CircError("JsonSchemaError", "JSON root must be an object", filename)

    if "format_version" not in data:
        raise CircError("JsonSchemaError", "Missing required field: format_version", filename)

    if data["format_version"] != 1:
        raise CircError("JsonSchemaError", f"Unsupported format_version: {data.get('format_version')}, expected 1", filename)

    for field_name in ("inputs", "outputs", "wires", "assignments"):
        if field_name not in data:
            raise CircError("JsonSchemaError", f"Missing required field: {field_name}", filename)
        if not isinstance(data[field_name], list):
            raise CircError("JsonSchemaError", f"Field '{field_name}' must be an array", filename)

    circuit = Circuit()
    all_names = set()

    # Parse ports
    def parse_port(port_data, target_list, kind):
        if not isinstance(port_data, dict):
            raise CircError("JsonSchemaError", f"{kind} must be an object", filename)
        if "name" not in port_data:
            raise CircError("JsonSchemaError", f"{kind} missing required field: name", filename)
        name = port_data["name"]
        if not isinstance(name, str):
            raise CircError("JsonSchemaError", f"{kind} name must be a string", filename)
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
            raise CircError("JsonSchemaError", f"Invalid identifier: {name}", filename)

        msb = port_data.get("msb", 0)
        lsb = port_data.get("lsb", 0)
        if not isinstance(msb, int) or not isinstance(lsb, int):
            raise CircError("JsonSchemaError", f"{kind} msb and lsb must be integers", filename)
        if msb < lsb:
            raise CircError("JsonSchemaError", f"{kind} msb ({msb}) must be >= lsb ({lsb})", filename)
        if lsb < 0:
            raise CircError("JsonSchemaError", f"{kind} lsb ({lsb}) must be >= 0", filename)

        if name in all_names:
            raise CircError("DuplicateNameError", f"Duplicate name: {name}", filename)
        all_names.add(name)
        target_list.append(Signal(name, msb, lsb))

    for port in data["inputs"]:
        parse_port(port, circuit.inputs, "Input port")
    for port in data["outputs"]:
        parse_port(port, circuit.outputs, "Output port")
    for port in data["wires"]:
        parse_port(port, circuit.wires, "Wire port")

    # Parse assignments
    assigned_lhs = set()
    for i, asn_data in enumerate(data["assignments"]):
        if not isinstance(asn_data, dict):
            raise CircError("JsonSchemaError", f"Assignment {i} must be an object", filename)
        if "lhs" not in asn_data:
            raise CircError("JsonSchemaError", f"Assignment {i} missing required field: lhs", filename)
        if "rhs" not in asn_data:
            raise CircError("JsonSchemaError", f"Assignment {i} missing required field: rhs", filename)

        lhs = asn_data["lhs"]
        rhs_str = asn_data["rhs"]

        if not isinstance(lhs, str):
            raise CircError("JsonSchemaError", f"Assignment {i} lhs must be a string", filename)
        if not isinstance(rhs_str, str):
            raise CircError("JsonSchemaError", f"Assignment {i} rhs must be a string", filename)

        # Check that lhs is declared
        if lhs not in all_names:
            raise CircError("UndefinedNameError", f"Undefined signal: {lhs}", filename)

        # Check that every output and wire appears exactly once as LHS
        if lhs in assigned_lhs:
            raise CircError("RedefinitionError", f"Signal assigned multiple times: {lhs}", filename)
        assigned_lhs.add(lhs)

        # Parse RHS expression using existing parser
        parser = Parser("", filename)
        try:
            rhs_expr, _ = parser._parse_expression(rhs_str, 1, 1)
        except CircError as e:
            raise CircError(e.error_type, e.message, filename, 1, e.col)

        circuit.assignments.append(Assignment(lhs, rhs_expr, 1, 1))

    # Check that all outputs and wires are assigned
    for sig in circuit.outputs + circuit.wires:
        if sig.name not in assigned_lhs:
            kind = "Output" if sig in circuit.outputs else "Wire"
            raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", filename)

    return circuit


def parse_bench_circuit(content: str, filename: str) -> Circuit:
    """Parse a BENCH circuit file."""
    circuit = Circuit()
    all_names = set()
    defined_wires = set()  # Wires defined by assignment
    output_refs = set()  # OUTPUT() references
    assignments_list = []  # [(lhs, rhs_str, line_num)]

    lines = content.split('\n')

    for line_num, line in enumerate(lines, 1):
        # Remove comments
        if '#' in line:
            line = line[:line.index('#')]
        line = line.strip()
        if not line:
            continue

        # Check for brackets in the line (not allowed in BENCH)
        if '[' in line or ']' in line:
            raise CircError("BenchParseError", "BENCH format does not support vector signals (brackets not allowed)", filename, line_num)

        # Parse INPUT(name)
        input_match = re.match(r'^INPUT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$', line, re.IGNORECASE)
        if input_match:
            name = input_match.group(1)
            if name in all_names:
                raise CircError("RedefinitionError", f"Signal defined multiple times: {name}", filename, line_num)
            all_names.add(name)
            circuit.inputs.append(Signal(name, 0, 0))
            continue

        # Parse OUTPUT(name)
        output_match = re.match(r'^OUTPUT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$', line, re.IGNORECASE)
        if output_match:
            name = output_match.group(1)
            if name in output_refs:
                raise CircError("RedefinitionError", f"OUTPUT declared multiple times: {name}", filename, line_num)
            output_refs.add(name)
            circuit.outputs.append(Signal(name, 0, 0))
            continue

        # Parse assignment: lhs = OP(args)
        assign_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if assign_match:
            lhs = assign_match.group(1)
            rhs_str = assign_match.group(2).strip()

            if lhs in defined_wires:
                raise CircError("RedefinitionError", f"Signal assigned multiple times: {lhs}", filename, line_num)

            # Check if lhs is an input (cannot assign to inputs)
            if lhs in all_names and lhs not in output_refs:
                raise CircError("InputAssignmentError", f"Cannot assign to input: {lhs}", filename, line_num)

            # Add to defined wires and all_names
            defined_wires.add(lhs)
            all_names.add(lhs)

            # Store assignment for later processing
            assignments_list.append((lhs, rhs_str, line_num))
            continue

        raise CircError("BenchParseError", f"Invalid syntax: {line}", filename, line_num)

    # Add wire declarations for defined wires (excluding outputs that reference inputs or other wires)
    wire_names = defined_wires - output_refs
    for name in wire_names:
        circuit.wires.append(Signal(name, 0, 0))

    # Now parse all assignments
    parser = Parser("", filename)
    for lhs, rhs_str, line_num in assignments_list:
        # Parse RHS - it must be a function call in BENCH format
        try:
            rhs_expr, _ = parser._parse_expression(rhs_str, line_num, 1)
        except CircError as e:
            raise CircError(e.error_type, e.message, filename, line_num, e.col)

        # Validate BENCH-specific constraints
        _validate_bench_expr(rhs_expr, filename, line_num, all_names)

        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, 1))

    # Validate OUTPUT references - must reference either an input or a defined wire
    input_names = {sig.name for sig in circuit.inputs}
    for output_sig in circuit.outputs:
        name = output_sig.name
        if name not in all_names:
            raise CircError("UndefinedNameError", f"OUTPUT references undefined signal: {name}", filename)
        if name not in defined_wires and name not in input_names:
            raise CircError("UndefinedNameError", f"OUTPUT references undefined signal: {name}", filename)

    return circuit


def _validate_bench_expr(expr, filename: str, line_num: int, defined_names: set):
    """Validate that a BENCH expression contains no literals and uses defined names."""
    if isinstance(expr, Literal):
        raise CircError("BenchParseError", "Literals are not allowed in BENCH format", filename, line_num)

    if isinstance(expr, Identifier):
        if expr.name not in defined_names:
            raise CircError("UndefinedNameError", f"Undefined signal: {expr.name}", filename, line_num)
        return

    if isinstance(expr, Call):
        for arg in expr.args:
            _validate_bench_expr(arg, filename, line_num, defined_names)
        return

    if isinstance(expr, Index):
        raise CircError("BenchParseError", "Indexing is not allowed in BENCH format", filename, line_num)

    if isinstance(expr, Slice):
        raise CircError("BenchParseError", "Slicing is not allowed in BENCH format", filename, line_num)

    if isinstance(expr, Concat):
        raise CircError("BenchParseError", "Concatenation is not allowed in BENCH format", filename, line_num)


class Validator:
    def __init__(self, circuit: Circuit, filename: str):
        self.circuit = circuit
        self.filename = filename
        self.signal_types = {sig.name: 'input' for sig in circuit.inputs}
        self.signal_types.update({sig.name: 'output' for sig in circuit.outputs})
        self.signal_types.update({sig.name: 'wire' for sig in circuit.wires})
        self.signal_info = {sig.name: sig for sig in circuit.inputs + circuit.outputs + circuit.wires}
        self._all_names = circuit.get_all_names()
        self._signal_widths = {name: sig.width for name, sig in self.signal_info.items()}
        # Track signals that are both inputs and outputs (BENCH pass-through case)
        self._input_names = {sig.name for sig in circuit.inputs}

    def validate(self):
        self._check_assignments()
        self._check_cycles()

    def _check_assignments(self):
        assigned = set()
        for asn in self.circuit.assignments:
            if asn.lhs not in self._all_names:
                raise CircError("UndefinedNameError", f"Undefined signal: {asn.lhs}", self.filename, asn.line, asn.col)
            if self.signal_types.get(asn.lhs) == 'input':
                raise CircError("InputAssignmentError", f"Cannot assign to input: {asn.lhs}", self.filename, asn.line, asn.col)
            if asn.lhs in assigned:
                raise CircError("MultipleAssignmentError", f"Signal assigned multiple times: {asn.lhs}", self.filename, asn.line, asn.col)
            assigned.add(asn.lhs)
            self._validate_expr(asn.rhs, asn.line)
        for sig in self.circuit.outputs + self.circuit.wires:
            if sig.name not in assigned:
                # Skip if this output is also an input (BENCH pass-through)
                if sig.name in self._input_names and self.signal_types.get(sig.name) == 'output':
                    continue
                kind = "Output" if self.signal_types.get(sig.name) == 'output' else "Wire"
                raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", self.filename)

    def _validate_expr(self, expr, line_num: int) -> int:
        if isinstance(expr, Literal):
            return expr.width
        if isinstance(expr, Identifier):
            if expr.name not in self._all_names:
                raise CircError("UndefinedNameError", f"Undefined signal: {expr.name}", self.filename, line_num)
            return self._signal_widths[expr.name]
        if isinstance(expr, Index):
            self._validate_expr(expr.expr, line_num)
            self._check_index_bounds(expr, line_num)
            return 1
        if isinstance(expr, Slice):
            self._validate_expr(expr.expr, line_num)
            if expr.hi < expr.lo:
                raise CircError("CircParseError", f"Invalid slice: hi ({expr.hi}) < lo ({expr.lo})",
                               self.filename, line_num)
            self._check_slice_bounds(expr, line_num)
            return expr.hi - expr.lo + 1
        if isinstance(expr, Concat):
            return sum(self._validate_expr(p, line_num) for p in expr.parts)
        if isinstance(expr, Call):
            self._check_operator_arity(expr, line_num)
            return self._check_operator_width(expr, line_num)
        raise RuntimeError(f"Unknown expression type: {type(expr)}")

    def _check_index_bounds(self, expr: Index, line_num: int):
        sig = self.signal_info.get(expr.expr.name) if isinstance(expr.expr, Identifier) else None
        if sig:
            if expr.index < sig.lsb or expr.index > sig.msb:
                raise CircError("IndexOutOfBoundsError",
                               f"Index {expr.index} out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                               self.filename, line_num)
        else:
            base_width = self._validate_expr(expr.expr, line_num)
            if expr.index < 0 or expr.index >= base_width:
                raise CircError("IndexOutOfBoundsError",
                               f"Index {expr.index} out of bounds for expression of width {base_width}",
                               self.filename, line_num)

    def _check_slice_bounds(self, expr: Slice, line_num: int):
        sig = self.signal_info.get(expr.expr.name) if isinstance(expr.expr, Identifier) else None
        if sig:
            if expr.lo < sig.lsb or expr.hi > sig.msb:
                raise CircError("IndexOutOfBoundsError",
                               f"Slice [{expr.hi}:{expr.lo}] out of bounds for signal {sig.name}[{sig.msb}:{sig.lsb}]",
                               self.filename, line_num)
        else:
            base_width = self._validate_expr(expr.expr, line_num)
            if expr.lo < 0 or expr.hi >= base_width:
                raise CircError("IndexOutOfBoundsError",
                               f"Slice [{expr.hi}:{expr.lo}] out of bounds for expression of width {base_width}",
                               self.filename, line_num)

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
        op = call.operator
        arg_widths = [self._validate_expr(arg, line_num) for arg in call.args]
        if op in _UNARY_PROPAGATE:
            return arg_widths[0]
        if op in _BINARY_PROPAGATE:
            for i, w in enumerate(arg_widths[1:], 1):
                if w != arg_widths[0]:
                    raise CircError("WidthMismatchError",
                                   f"Width mismatch in {op}: operand 0 has width {arg_widths[0]}, operand {i} has width {w}",
                                   self.filename, line_num)
            return arg_widths[0]
        if op in _SELECT:
            if arg_widths[0] != 1:
                raise CircError("WidthMismatchError",
                               f"Selector in {op} must have width 1, got width {arg_widths[0]}", self.filename, line_num)
            if arg_widths[1] != arg_widths[2]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in {op}: operand 1 has width {arg_widths[1]}, operand 2 has width {arg_widths[2]}",
                               self.filename, line_num)
            return arg_widths[1]
        if op in _REDUCE:
            return 1
        if op == 'EQ':
            if arg_widths[0] != arg_widths[1]:
                raise CircError("WidthMismatchError",
                               f"Width mismatch in EQ: operand 0 has width {arg_widths[0]}, operand 1 has width {arg_widths[1]}",
                               self.filename, line_num)
            return 1
        raise RuntimeError(f"Unknown operator: {op}")

    def _check_cycles(self):
        dependencies = {sig.name: set() for sig in self.circuit.inputs}
        for asn in self.circuit.assignments:
            dependencies[asn.lhs] = _collect_dependencies(asn.rhs)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {name: WHITE for name in self._all_names}

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

        for node in self._all_names:
            if color[node] == WHITE:
                cycle = dfs(node, [])
                if cycle:
                    raise CircError("CycleError", f"Cycle detected: {' -> '.join(cycle)}", self.filename)


class Evaluator:
    """Evaluates a circuit using 3-valued logic. For 2-valued mode, inputs are
    fully-known TriValues and results are converted to int at output time."""

    def __init__(self, circuit: Circuit):
        self.circuit = circuit
        self._signal_widths = _all_signal_widths(circuit)

    def evaluate(self, inputs: dict) -> dict:
        self.values = dict(inputs)
        for asn in self.circuit.assignments:
            self.values[asn.lhs] = self._eval_expr(asn.rhs)
        return {sig.name: self.values[sig.name] for sig in self.circuit.outputs}

    def _eval_expr(self, expr) -> TriValue:
        if isinstance(expr, Literal):
            return TriValue.from_int(expr.value, expr.width)
        if isinstance(expr, Identifier):
            return self.values[expr.name]
        if isinstance(expr, Index):
            val = self._eval_expr(expr.expr)
            return TriValue(
                (val.value_mask >> expr.index) & 1,
                (val.known_mask >> expr.index) & 1,
                1
            )
        if isinstance(expr, Slice):
            val = self._eval_expr(expr.expr)
            width = expr.hi - expr.lo + 1
            mask = (1 << width) - 1
            return TriValue(
                (val.value_mask >> expr.lo) & mask,
                (val.known_mask >> expr.lo) & mask,
                width
            )
        if isinstance(expr, Concat):
            result_value = 0
            result_known = 0
            total_width = 0
            for part in expr.parts:
                w = _expr_width(part, self._signal_widths)
                part_val = self._eval_expr(part)
                result_value = (result_value << w) | (part_val.value_mask & ((1 << w) - 1))
                result_known = (result_known << w) | (part_val.known_mask & ((1 << w) - 1))
                total_width += w
            return TriValue(result_value, result_known, total_width)
        if isinstance(expr, Call):
            args = [self._eval_expr(arg) for arg in expr.args]
            return self._apply_operator(expr.operator, args)
        raise RuntimeError(f"Unknown expression type: {type(expr)}")

    def _apply_operator(self, op: str, args: list) -> TriValue:
        if op == 'NOT':
            return TriValue.not_(args[0])
        if op == 'BUF':
            return args[0].copy()

        if op in _BINARY_OPS:
            result = args[0]
            for a in args[1:]:
                result = _BINARY_OPS[op](result, a)
            if op in _NEGATED:
                result = TriValue.not_(result)
            return result

        if op in _SELECT:
            return TriValue.mux(args[0], args[1], args[2])

        if op == 'REDUCE_AND':
            return TriValue.reduce_and(args[0])
        if op == 'REDUCE_OR':
            return TriValue.reduce_or(args[0])
        if op == 'REDUCE_XOR':
            return TriValue.reduce_xor(args[0])
        if op == 'EQ':
            return TriValue.eq(args[0], args[1])

        raise RuntimeError(f"Unknown operator: {op}")


def output_json(data: dict):
    print(json.dumps(data, separators=(',', ':')))


def detect_format(filename: str) -> str:
    """Detect format from file extension."""
    if filename.endswith('.circ'):
        return 'circ'
    if filename.endswith('.json'):
        return 'json'
    if filename.endswith('.bench'):
        return 'bench'
    return None


def load_circuit(filename: str, command: str, json_mode: bool, format_override: str = None):
    try:
        with open(filename, 'r') as f:
            content = f.read()
    except (FileNotFoundError, IOError):
        return output_error(CircError("FileNotFoundError", f"Cannot read file: {filename}", filename), command, json_mode)

    # Determine format
    if format_override and format_override != 'auto':
        format_name = format_override
    else:
        format_name = detect_format(filename)
        if format_name is None:
            return output_error(CircError("UnknownInputFormatError", f"Cannot determine format from extension: {filename}"), command, json_mode)

    try:
        if format_name == 'circ':
            circuit = Parser(content, filename).parse()
            Validator(circuit, filename).validate()
        elif format_name == 'json':
            circuit = parse_json_circuit(content, filename)
            Validator(circuit, filename).validate()
        elif format_name == 'bench':
            circuit = parse_bench_circuit(content, filename)
            Validator(circuit, filename).validate()
        else:
            return output_error(CircError("UnknownInputFormatError", f"Unknown format: {format_name}"), command, json_mode)
    except CircError as e:
        return output_error(e, command, json_mode)

    return circuit, format_name


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


def check_command(args: list, json_mode: bool, format_override: str = None) -> int:
    if not args:
        return output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)
    filename = args[0]
    result = load_circuit(filename, "check", json_mode, format_override)
    if isinstance(result, int):
        return result
    circuit, format_name = result
    inputs = signal_list(circuit.inputs)
    outputs = signal_list(circuit.outputs)
    if json_mode:
        output_json({"ok": True, "command": "check", "format": format_name, "inputs": inputs, "outputs": outputs})
    else:
        print("Circuit is valid.")
        print(f"Inputs: {', '.join(s['name'] for s in inputs)}")
        print(f"Outputs: {', '.join(s['name'] for s in outputs)}")
    return 0


def _parse_eval_args(args, json_mode):
    """Parse eval command arguments. Returns (opts_dict, error_result_or_None)."""
    if not args:
        return None, output_error(CircError("CliUsageError", "Missing required argument: <file.circ>"), "__cli__", json_mode)
    opts = {
        'filename': args[0],
        'set_values': {},
        'default_value': None,
        'allow_extra': False,
        'radix': 'bin',
        'mode': '2val',
        'format': 'auto',
    }
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == '--set':
            if i + 1 >= len(args):
                return None, output_error(CircError("CliUsageError", "--set requires a name=value argument"), "eval", json_mode)
            set_arg = args[i + 1]
            if '=' not in set_arg:
                return None, output_error(CircError("CliUsageError", f"Invalid --set format: {set_arg}, expected name=value"), "eval", json_mode)
            name, value = set_arg.split('=', 1)
            opts['set_values'][name] = value
            i += 2
        elif arg == '--default':
            if i + 1 >= len(args):
                return None, output_error(CircError("CliUsageError", "--default requires a value (0 or 1)"), "eval", json_mode)
            default_arg = args[i + 1]
            if default_arg not in ('0', '1'):
                return None, output_error(CircError("CliUsageError", f"Invalid --default value: {default_arg}, must be 0 or 1"), "eval", json_mode)
            opts['default_value'] = int(default_arg)
            i += 2
        elif arg == '--allow-extra':
            opts['allow_extra'] = True
            i += 1
        elif arg == '--radix':
            if i + 1 >= len(args):
                return None, output_error(CircError("CliUsageError", "--radix requires a value (bin, hex, or dec)"), "eval", json_mode)
            radix_arg = args[i + 1].lower()
            if radix_arg not in ('bin', 'hex', 'dec'):
                return None, output_error(CircError("CliUsageError", f"Invalid --radix value: {radix_arg}, must be bin, hex, or dec"), "eval", json_mode)
            opts['radix'] = radix_arg
            i += 2
        elif arg == '--mode':
            if i + 1 >= len(args):
                return None, output_error(CircError("CliUsageError", "--mode requires a value (2val or 3val)"), "eval", json_mode)
            mode_arg = args[i + 1].lower()
            if mode_arg not in ('2val', '3val'):
                return None, output_error(CircError("CliUsageError", f"Invalid --mode value: {mode_arg}, must be 2val or 3val"), "eval", json_mode)
            opts['mode'] = mode_arg
            i += 2
        elif arg == '--format':
            if i + 1 >= len(args):
                return None, output_error(CircError("CliUsageError", "--format requires a value (auto, circ, json, or bench)"), "eval", json_mode)
            format_arg = args[i + 1].lower()
            if format_arg not in ('auto', 'circ', 'json', 'bench'):
                return None, output_error(CircError("CliUsageError", f"Invalid --format value: {format_arg}, must be auto, circ, json, or bench"), "eval", json_mode)
            opts['format'] = format_arg
            i += 2
        elif '=' in arg:
            name, value = arg.split('=', 1)
            opts['set_values'][name] = value
            i += 1
        else:
            return None, output_error(CircError("CliUsageError", f"Unknown option: {arg}"), "eval", json_mode)
    return opts, None


def _default_fill(default_value, width):
    """Compute the fill value for a signal when using --default."""
    if width == 1:
        return TriValue.from_int(default_value, 1)
    fill = default_value * ((1 << width) - 1) if default_value else 0
    return TriValue.from_int(fill, width)


def eval_command(args: list, json_mode: bool, format_override: str = 'auto') -> int:
    opts, err = _parse_eval_args(args, json_mode)
    if err is not None:
        return err

    filename = opts['filename']
    set_values = opts['set_values']
    default_value = opts['default_value']
    allow_extra = opts['allow_extra']
    radix = opts['radix']
    mode = opts['mode']
    # Use eval's --format if specified, otherwise use global format_override
    format_val = opts['format'] if opts['format'] != 'auto' else format_override

    if mode == '3val' and radix != 'bin':
        return output_error(EvalError("RadixNotAllowedIn3ValError", "Only --radix bin is allowed with --mode 3val"), "eval", json_mode)

    result = load_circuit(filename, "eval", json_mode, format_val)
    if isinstance(result, int):
        return result
    circuit, _ = result
    input_names = {sig.name for sig in circuit.inputs}

    for name in set_values:
        if name not in input_names and not allow_extra:
            return output_error(EvalError("UnknownInputError", f"Unknown input: {name}"), "eval", json_mode)

    missing_inputs = input_names - set(set_values.keys())
    if missing_inputs and default_value is None:
        return output_error(EvalError("MissingInputError", f"Missing input values for: {', '.join(sorted(missing_inputs))}"), "eval", json_mode)

    # Build TriValue inputs for all modes
    inputs = {}
    try:
        for sig in circuit.inputs:
            if sig.name in set_values:
                inputs[sig.name] = parse_3val_input(set_values[sig.name], sig.width, sig.name, filename)
            elif default_value is not None:
                inputs[sig.name] = _default_fill(default_value, sig.width)
    except EvalError as e:
        return output_error(e, "eval", json_mode)

    try:
        results = Evaluator(circuit).evaluate(inputs)
    except Exception as e:
        return output_error(CircError("InternalError", str(e)), "eval", json_mode)

    # Format outputs
    sorted_outputs = sorted(results.items(), key=lambda x: x[0])

    if mode == '2val':
        if json_mode:
            output_list = []
            for name, val in sorted_outputs:
                sig = circuit.get_signal(name)
                w = sig.width if sig else 1
                output_list.append({
                    "name": name,
                    "msb": sig.msb if sig else 0,
                    "lsb": sig.lsb if sig else 0,
                    "value": format_value(val.to_int(), w, radix)
                })
            output_json({
                "ok": True, "command": "eval", "mode": "2val", "radix": radix,
                "inputs": signal_list(circuit.inputs),
                "outputs": output_list
            })
        else:
            for name, val in sorted_outputs:
                sig = circuit.get_signal(name)
                w = sig.width if sig else 1
                print(f"{name}={format_value(val.to_int(), w, radix)}")
    else:
        if json_mode:
            output_list = []
            for name, val in sorted_outputs:
                sig = circuit.get_signal(name)
                output_list.append({
                    "name": name,
                    "msb": sig.msb if sig else 0,
                    "lsb": sig.lsb if sig else 0,
                    "value": format_trivalue(val)
                })
            output_json({
                "ok": True, "command": "eval", "mode": "3val", "radix": "bin",
                "inputs": signal_list(circuit.inputs),
                "outputs": output_list
            })
        else:
            for name, val in sorted_outputs:
                print(f"{name}={format_trivalue(val)}")

    return 0


def main():
    args = sys.argv[1:]
    if '--help' in args:
        print("""Usage: circopt.py [OPTIONS] <COMMAND> [ARGS]

Options:
  --help     Show this help message and exit
  --version  Show version and exit
  --json     Output in JSON format
  --format   Input format: auto, circ, json, bench (default: auto)

Commands:
  check      Validate a circuit file
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

    # Parse --format option
    format_override = 'auto'
    i = 0
    while i < len(args):
        if args[i] == '--format':
            if i + 1 >= len(args):
                return output_error(CircError("CliUsageError", "--format requires a value (auto, circ, json, or bench)"), "__cli__", json_mode)
            format_val = args[i + 1].lower()
            if format_val not in ('auto', 'circ', 'json', 'bench'):
                return output_error(CircError("CliUsageError", f"Invalid --format value: {format_val}, must be auto, circ, json, or bench"), "__cli__", json_mode)
            format_override = format_val
            args = args[:i] + args[i+2:]
            break
        i += 1

    if not args:
        return output_error(CircError("CliUsageError", "No command provided. Use --help for usage information."), "__cli__", json_mode)
    command, *command_args = args
    if command == 'check':
        return check_command(command_args, json_mode, format_override)
    if command == 'eval':
        return eval_command(command_args, json_mode, format_override)
    return output_error(CircError("CliUsageError", f"Unknown command: {command}"), "__cli__", json_mode)


if __name__ == '__main__':
    sys.exit(main())
