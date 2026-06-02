"""Parsers for .circ, .json, and .bench circuit formats."""

import json
import re
from typing import Tuple

from ast_nodes import Signal, Identifier, Literal, Index, Slice, Concat, Call, Assignment, Circuit
from errors import CircError


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


def _parse_sized_literal(text: str, allow_x: bool, filename: str = None, line: int = None, col: int = None):
    """Parse a sized literal like 4'b0101, 8'hFF, 3'd5.
    Returns (value, width, known_mask) or None if not a sized literal.
    For non-X mode, known_mask is None (meaning all-known of given width).
    For X mode, known_mask is the actual mask.
    """
    sized_match = re.match(r"^(\d+)'([bhd])([0-9a-fA-FxX_]+)$", text, re.IGNORECASE)
    if not sized_match:
        return None

    width = int(sized_match.group(1))
    base = sized_match.group(2).lower()
    digits = sized_match.group(3).replace('_', '')

    if width <= 0:
        return None

    if base == 'b':
        if allow_x:
            if not re.match(r'^[01Xx]+$', digits):
                return None
            return _parse_binary_with_x(digits, width), width, None
        if not re.match(r'^[01]+$', digits):
            return None
        return int(digits, 2), width, None

    if 'x' in digits.lower():
        return None

    if base == 'h':
        if not re.match(r'^[0-9a-fA-F]+$', digits):
            return None
        value = int(digits, 16)
    elif base == 'd':
        if not re.match(r'^[0-9]+$', digits):
            return None
        value = int(digits, 10)
    else:
        return None

    if value > (1 << width) - 1:
        return None
    return value, width, None


def parse_literal(text: str, filename: str = None, line: int = None, col: int = None) -> Tuple[int, int]:
    original = text
    text = text.replace('_', '')

    if text == 'X':
        raise CircError("CircParseError", "Literal 'X' values are not allowed in .circ files", filename, line, col)

    if text in ('0', '1'):
        return int(text), 1

    if text.lower().startswith('0b'):
        digits = text[2:]
        if not digits or not re.match(r'^[01]+$', digits):
            raise CircError("CircParseError", f"Invalid binary literal: {original}", filename, line, col)
        return int(digits, 2), len(digits)

    if text.lower().startswith('0x'):
        digits = text[2:]
        if not digits or not re.match(r'^[0-9a-fA-F]+$', digits):
            raise CircError("CircParseError", f"Invalid hex literal: {original}", filename, line, col)
        return int(digits, 16), len(digits) * 4

    sized = _parse_sized_literal(text, allow_x=False, filename=filename, line=line, col=col)
    if sized is not None:
        return sized[0], sized[1]

    raise CircError("CircParseError", f"Invalid literal: {original}", filename, line, col)


def _parse_binary_with_x(digits: str, width: int):
    """Parse binary digits containing X. Returns (value_mask, known_mask) tuple."""
    from trivalue import TriValue
    value_mask = 0
    known_mask = 0
    for i, digit in enumerate(digits):
        bit_pos = width - 1 - i
        if digit.upper() != 'X':
            known_mask |= (1 << bit_pos)
            if digit == '1':
                value_mask |= (1 << bit_pos)
    return TriValue(value_mask, known_mask, width)


def parse_3val_input(value_str: str, expected_width: int, signal_name: str, filename: str = None):
    from trivalue import TriValue
    from errors import EvalError

    original = value_str
    text = value_str.replace('_', '')

    if text in ('0', '1'):
        return TriValue.from_int(int(text), expected_width)
    if text.upper() == 'X':
        return TriValue(0, 0, expected_width)

    def _width_err(expected, got):
        return EvalError("InputWidthMismatchError",
                         f"Input width mismatch for '{signal_name}': expected width {expected}, got width {got}")
    def _val_err():
        return EvalError("InputValueParseError", f"Invalid input value for '{signal_name}': {original}")

    if text.lower().startswith('0b'):
        digits = text[2:]
        if not digits or not re.match(r'^[01Xx]+$', digits):
            raise _val_err()
        if len(digits) != expected_width:
            raise _width_err(expected_width, len(digits))
        return _parse_binary_with_x(digits, expected_width)

    if text.lower().startswith('0x'):
        digits = text[2:]
        if not digits or 'x' in digits.lower() or not re.match(r'^[0-9a-fA-F]+$', digits):
            raise _val_err()
        width = len(digits) * 4
        if width != expected_width:
            raise _width_err(expected_width, width)
        return TriValue.from_int(int(digits, 16), expected_width)

    sized_match = re.match(r"^(\d+)'([bhd])([0-9a-fA-FxX_]+)$", text, re.IGNORECASE)
    if sized_match:
        width = int(sized_match.group(1))
        base = sized_match.group(2).lower()
        digits = sized_match.group(3).replace('_', '')

        if width <= 0:
            raise _val_err()
        if width != expected_width:
            raise _width_err(expected_width, width)

        if base == 'b':
            if not re.match(r'^[01Xx]+$', digits):
                raise _val_err()
            return _parse_binary_with_x(digits, width)

        if 'x' in digits.lower():
            raise _val_err()

        if base == 'h':
            if not re.match(r'^[0-9a-fA-F]+$', digits):
                raise _val_err()
            value = int(digits, 16)
        else:
            if not re.match(r'^[0-9]+$', digits):
                raise _val_err()
            try:
                value = int(digits, 10)
            except ValueError:
                raise _val_err()

        if value > (1 << width) - 1:
            raise _val_err()
        return TriValue.from_int(value, width)

    raise _val_err()


def _is_valid_identifier(name: str) -> bool:
    return bool(name) and not name[0].isdigit() and all(c.isalnum() or c == '_' for c in name)


class CircParser:
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
                if not _is_valid_identifier(part):
                    raise CircError("CircParseError", f"Invalid identifier: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                if part in existing_names:
                    raise CircError("DuplicateNameError", f"Duplicate name: {part}",
                                   self.filename, line_num, line.find(part) + 1)
                target.append(Signal(part, 0, 0))
                existing_names.add(part)

    def _parse_assignment(self, line: str, circuit: Circuit, line_num: int, raw_line: str):
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


def _validate_bench_expr(expr, filename: str, line_num: int, defined_names: set):
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


def parse_json_circuit(content: str, filename: str) -> Circuit:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise CircError("JsonParseError", f"Invalid JSON: {e.msg}", filename, e.lineno, e.colno)

    if not isinstance(data, dict):
        raise CircError("JsonSchemaError", "JSON root must be an object", filename)
    if data.get("format_version") != 1:
        raise CircError("JsonSchemaError", f"Unsupported format_version: {data.get('format_version')}, expected 1", filename)

    for field_name in ("inputs", "outputs", "wires", "assignments"):
        if field_name not in data:
            raise CircError("JsonSchemaError", f"Missing required field: {field_name}", filename)
        if not isinstance(data[field_name], list):
            raise CircError("JsonSchemaError", f"Field '{field_name}' must be an array", filename)

    circuit = Circuit()
    all_names = set()

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
        if lhs not in all_names:
            raise CircError("UndefinedNameError", f"Undefined signal: {lhs}", filename)
        if lhs in assigned_lhs:
            raise CircError("RedefinitionError", f"Signal assigned multiple times: {lhs}", filename)
        assigned_lhs.add(lhs)

        parser = CircParser("", filename)
        try:
            rhs_expr, _ = parser._parse_expression(rhs_str, 1, 1)
        except CircError as e:
            raise CircError(e.error_type, e.message, filename, 1, e.col)
        circuit.assignments.append(Assignment(lhs, rhs_expr, 1, 1))

    for sig in circuit.outputs + circuit.wires:
        if sig.name not in assigned_lhs:
            kind = "Output" if sig in circuit.outputs else "Wire"
            raise CircError("UnassignedSignalError", f"{kind} not assigned: {sig.name}", filename)

    return circuit


def parse_bench_circuit(content: str, filename: str) -> Circuit:
    circuit = Circuit()
    all_names = set()
    defined_wires = set()
    output_refs = set()
    assignments_list = []

    lines = content.split('\n')
    for line_num, line in enumerate(lines, 1):
        if '#' in line:
            line = line[:line.index('#')]
        line = line.strip()
        if not line:
            continue

        if '[' in line or ']' in line:
            raise CircError("BenchParseError", "BENCH format does not support vector signals (brackets not allowed)", filename, line_num)

        input_match = re.match(r'^INPUT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$', line, re.IGNORECASE)
        if input_match:
            name = input_match.group(1)
            if name in all_names:
                raise CircError("RedefinitionError", f"Signal defined multiple times: {name}", filename, line_num)
            all_names.add(name)
            circuit.inputs.append(Signal(name, 0, 0))
            continue

        output_match = re.match(r'^OUTPUT\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)$', line, re.IGNORECASE)
        if output_match:
            name = output_match.group(1)
            if name in output_refs:
                raise CircError("RedefinitionError", f"OUTPUT declared multiple times: {name}", filename, line_num)
            output_refs.add(name)
            circuit.outputs.append(Signal(name, 0, 0))
            continue

        assign_match = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$', line)
        if assign_match:
            lhs = assign_match.group(1)
            rhs_str = assign_match.group(2).strip()
            if lhs in defined_wires:
                raise CircError("RedefinitionError", f"Signal assigned multiple times: {lhs}", filename, line_num)
            if lhs in all_names and lhs not in output_refs:
                raise CircError("InputAssignmentError", f"Cannot assign to input: {lhs}", filename, line_num)
            defined_wires.add(lhs)
            all_names.add(lhs)
            assignments_list.append((lhs, rhs_str, line_num))
            continue

        raise CircError("BenchParseError", f"Invalid syntax: {line}", filename, line_num)

    for name in defined_wires - output_refs:
        circuit.wires.append(Signal(name, 0, 0))

    parser = CircParser("", filename)
    for lhs, rhs_str, line_num in assignments_list:
        try:
            rhs_expr, _ = parser._parse_expression(rhs_str, line_num, 1)
        except CircError as e:
            raise CircError(e.error_type, e.message, filename, line_num, e.col)
        _validate_bench_expr(rhs_expr, filename, line_num, all_names)
        circuit.assignments.append(Assignment(lhs, rhs_expr, line_num, 1))

    input_names = {sig.name for sig in circuit.inputs}
    for output_sig in circuit.outputs:
        name = output_sig.name
        if name not in defined_wires and name not in input_names:
            raise CircError("UndefinedNameError", f"OUTPUT references undefined signal: {name}", filename)

    return circuit
