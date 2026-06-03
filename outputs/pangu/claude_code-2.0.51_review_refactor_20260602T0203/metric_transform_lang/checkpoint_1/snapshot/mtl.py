#!/usr/bin/env python3
"""
MTL (Mini Transformation Language) - A file-based CLI for processing sales events.
"""

import argparse
import csv
import json
import math
import re
import sys
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


# =============================================================================
# Error types and exit codes
# =============================================================================

class ErrorType(Enum):
    BAD_DSL = "bad_dsl"
    TYPE_ERROR = "type_error"
    BAD_IO = "bad_io"
    RUNTIME_ERROR = "runtime_error"


def error_exit(error_type: ErrorType, message: str) -> None:
    """Print error to stderr and exit with appropriate code."""
    print(f"ERROR:{error_type.value}:{message}", file=sys.stderr)
    exit_codes = {
        ErrorType.BAD_DSL: 1,
        ErrorType.TYPE_ERROR: 2,
        ErrorType.BAD_IO: 3,
        ErrorType.RUNTIME_ERROR: 4,
    }
    sys.exit(exit_codes[error_type])


# =============================================================================
# Type system
# =============================================================================

class DataType(Enum):
    STRING = "string"
    NUMBER = "number"
    TIMESTAMP = "timestamp"
    BOOLEAN = "boolean"
    INT = "int"  # For internal use (like day/month/year results)

    def __eq__(self, other):
        if isinstance(other, DataType):
            return self.value == other.value
        return False

    def is_numeric(self) -> bool:
        return self == DataType.NUMBER or self == DataType.INT


def get_type(value: Any) -> DataType:
    """Infer type from Python value."""
    if isinstance(value, bool):
        return DataType.BOOLEAN
    elif isinstance(value, int) and not isinstance(value, bool):
        return DataType.INT
    elif isinstance(value, float):
        return DataType.NUMBER
    elif isinstance(value, datetime):
        return DataType.TIMESTAMP
    elif isinstance(value, str):
        return DataType.STRING
    else:
        return DataType.STRING


def compare_types(t1: DataType, t2: DataType) -> bool:
    """Check if two types are comparable (same or numeric widening)."""
    if t1 == t2:
        return True
    # Numeric widening: int <-> float
    if t1.is_numeric() and t2.is_numeric():
        return True
    return False


# =============================================================================
# Expression system (for parsing and evaluating)
# =============================================================================

class Expr(ABC):
    """Base class for all expressions."""

    @abstractmethod
    def evaluate(self, row: Dict[str, Any]) -> Any:
        pass

    @abstractmethod
    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        pass


class ColumnRef(Expr):
    def __init__(self, name: str):
        self.name = name

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return row.get(self.name)

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        return schema.get(self.name, DataType.STRING)

    def __repr__(self):
        return f"ColumnRef({self.name!r})"


class Literal(Expr):
    def __init__(self, value: Any):
        self.value = value

    def evaluate(self, row: Dict[str, Any]) -> Any:
        return self.value

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        return get_type(self.value)

    def __repr__(self):
        return f"Literal({self.value!r})"


class BinaryOp(Expr):
    def __init__(self, left: Expr, op: str, right: Expr):
        self.left = left
        self.op = op
        self.right = right

    def evaluate(self, row: Dict[str, Any]) -> Any:
        lval = self.left.evaluate(row)
        rval = self.right.evaluate(row)

        if self.op in ("<", "<=", ">", ">=", "==", "!="):
            # Numeric comparison
            if isinstance(lval, (int, float)) and isinstance(rval, (int, float)):
                lv = float(lval)
                rv = float(rval)
                if self.op == "<":
                    return lv < rv
                elif self.op == "<=":
                    return lv <= rv
                elif self.op == ">":
                    return lv > rv
                elif self.op == ">=":
                    return lv >= rv
                elif self.op == "==":
                    return lv == rv
                elif self.op == "!=":
                    return lv != rv
            # String comparison
            elif isinstance(lval, str) and isinstance(rval, str):
                if self.op == "<":
                    return lval < rval
                elif self.op == "<=":
                    return lval <= rval
                elif self.op == ">":
                    return lval > rval
                elif self.op == ">=":
                    return lval >= rval
                elif self.op == "==":
                    return lval == rval
                elif self.op == "!=":
                    return lval != rval
            # Timestamp comparison
            elif isinstance(lval, datetime) and isinstance(rval, datetime):
                if self.op == "<":
                    return lval < rval
                elif self.op == "<=":
                    return lval <= rval
                elif self.op == ">":
                    return lval > rval
                elif self.op == ">=":
                    return lval >= rval
                elif self.op == "==":
                    return lval == rval
                elif self.op == "!=":
                    return lval != rval
            # Boolean comparison
            elif isinstance(lval, bool) and isinstance(rval, bool):
                if self.op == "==":
                    return lval == rval
                elif self.op == "!=":
                    return lval != rval
            raise RuntimeError(f"Cannot compare {type(lval).__name__} and {type(rval).__name__}")

        elif self.op == "&":
            return bool(lval) and bool(rval)
        elif self.op == "|":
            return bool(lval) or bool(rval)

        raise RuntimeError(f"Unknown operator: {self.op}")

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        ltype = self.left.get_type(schema)
        rtype = self.right.get_type(schema)

        if self.op in ("<", "<=", ">", ">=", "==", "!="):
            if not compare_types(ltype, rtype):
                error_exit(ErrorType.TYPE_ERROR, f"Cannot compare {ltype.value} with {rtype.value}")
            return DataType.BOOLEAN
        elif self.op in ("&", "|"):
            if ltype != DataType.BOOLEAN or rtype != DataType.BOOLEAN:
                error_exit(ErrorType.TYPE_ERROR, f"Logical operators require boolean operands, got {ltype.value} and {rtype.value}")
            return DataType.BOOLEAN

        return DataType.BOOLEAN

    def __repr__(self):
        return f"BinaryOp({self.left!r}, {self.op!r}, {self.right!r})"


class UnaryOp(Expr):
    def __init__(self, op: str, operand: Expr):
        self.op = op
        self.operand = operand

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.operand.evaluate(row)
        if self.op == "!":
            return not bool(val)
        raise RuntimeError(f"Unknown unary operator: {self.op}")

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        if self.op == "!":
            if self.operand.get_type(schema) != DataType.BOOLEAN:
                error_exit(ErrorType.TYPE_ERROR, f"Operand of '!' must be boolean, got {self.operand.get_type(schema).value}")
            return DataType.BOOLEAN
        return DataType.BOOLEAN

    def __repr__(self):
        return f"UnaryOp({self.op!r}, {self.operand!r})"


class InExpr(Expr):
    """String literal in column expression."""

    def __init__(self, literal: str, column: ColumnRef):
        self.literal = literal
        self.column = column

    def evaluate(self, row: Dict[str, Any]) -> Any:
        col_val = self.column.evaluate(row)
        if not isinstance(col_val, str):
            raise RuntimeError(f"Right operand of 'in' must be string, got {type(col_val).__name__}")
        return self.literal in col_val

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if col_type != DataType.STRING:
            error_exit(ErrorType.TYPE_ERROR, f"Right operand of 'in' must be string column, got {col_type.value}")
        return DataType.BOOLEAN

    def __repr__(self):
        return f"InExpr({self.literal!r}, {self.column!r})"


class TransformExpr(Expr):
    """Base class for transform expressions (apply)."""
    pass


class LenExpr(TransformExpr):
    def __init__(self, column: ColumnRef):
        self.column = column

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.column.evaluate(row)
        if not isinstance(val, str):
            raise RuntimeError(f"len() requires string, got {type(val).__name__}")
        return len(val)

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if col_type != DataType.STRING:
            error_exit(ErrorType.TYPE_ERROR, f"len() requires string column, got {col_type.value}")
        return DataType.INT


class DayExpr(TransformExpr):
    def __init__(self, column: ColumnRef):
        self.column = column

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.column.evaluate(row)
        if not isinstance(val, datetime):
            raise RuntimeError(f"day() requires timestamp, got {type(val).__name__}")
        return val.day

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if col_type != DataType.TIMESTAMP:
            error_exit(ErrorType.TYPE_ERROR, f"day() requires timestamp column, got {col_type.value}")
        return DataType.INT


class MonthExpr(TransformExpr):
    def __init__(self, column: ColumnRef):
        self.column = column

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.column.evaluate(row)
        if not isinstance(val, datetime):
            raise RuntimeError(f"month() requires timestamp, got {type(val).__name__}")
        return val.month

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if col_type != DataType.TIMESTAMP:
            error_exit(ErrorType.TYPE_ERROR, f"month() requires timestamp column, got {col_type.value}")
        return DataType.INT


class YearExpr(TransformExpr):
    def __init__(self, column: ColumnRef):
        self.column = column

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.column.evaluate(row)
        if not isinstance(val, datetime):
            raise RuntimeError(f"year() requires timestamp, got {type(val).__name__}")
        return val.year

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if col_type != DataType.TIMESTAMP:
            error_exit(ErrorType.TYPE_ERROR, f"year() requires timestamp column, got {col_type.value}")
        return DataType.INT


class RoundExpr(TransformExpr):
    def __init__(self, column: ColumnRef, digits: Optional[int] = None):
        self.column = column
        self.digits = digits if digits is not None else 0

    def evaluate(self, row: Dict[str, Any]) -> Any:
        val = self.column.evaluate(row)
        if not isinstance(val, (int, float)):
            raise RuntimeError(f"round() requires number, got {type(val).__name__}")
        return round_half_away_from_zero(float(val), self.digits)

    def get_type(self, schema: Dict[str, DataType]) -> DataType:
        col_type = self.column.get_type(schema)
        if not col_type.is_numeric():
            error_exit(ErrorType.TYPE_ERROR, f"round() requires numeric column, got {col_type.value}")
        return DataType.NUMBER


def round_half_away_from_zero(value: float, digits: int) -> float:
    """Round half away from zero."""
    if math.isnan(value) or math.isinf(value):
        raise RuntimeError(f"Cannot round NaN/Inf value: {value}")
    multiplier = 10 ** digits
    rounded = math.floor(value * multiplier + 0.5 * (1 if value >= 0 else -1))
    if rounded == 0 and value != 0:
        rounded = 0.0
    return rounded / multiplier


# =============================================================================
# DSL Parser
# =============================================================================

class DSLParser:
    """Parser for the MTL DSL."""

    def __init__(self, text: str):
        self.text = text
        self.statements = []
        self._parse()

    def _parse(self):
        """Parse the DSL text into statements."""
        lines = self.text.strip().split('\n')
        line_num = 0
        buffer = None  # For multi-line aggregate
        in_buffer = False

        for line in lines:
            line_num += 1
            raw_line = line
            line = re.sub(r'#.*$', '', line).strip()
            if not line:
                if in_buffer:
                    continue
                continue

            # Handle multi-line aggregate continuation
            if buffer is not None:
                buffer += ' ' + line
                if line.endswith(','):
                    in_buffer = True
                    continue
                else:
                    in_buffer = False
                    self._parse_aggregate(buffer, line_num)
                    buffer = None
                    continue

            # Check if this is the start of an aggregate
            if line.startswith('aggregate '):
                if line.endswith(','):
                    buffer = line
                    in_buffer = True
                    continue
                else:
                    self._parse_aggregate(line, line_num)
                    continue

            # Other statements
            if line.startswith('filter('):
                self._parse_filter(line, line_num)
            elif line.startswith('apply('):
                self._parse_apply(line, line_num)
            elif line.startswith('group_by('):
                self._parse_group_by(line, line_num)
            elif line.startswith('window('):
                self._parse_window(line, line_num)
            else:
                error_exit(ErrorType.BAD_DSL, f"Unknown statement at line {line_num}: {line}")

    def _parse_filter(self, line: str, line_num: int):
        match = re.match(r'filter\((.+)\)$', line)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid filter syntax at line {line_num}")
        expr_text = match.group(1).strip()
        self.statements.append(('filter', self._parse_bool_expr(expr_text, line_num)))

    def _parse_apply(self, line: str, line_num: int):
        match = re.match(r'apply\((.+),(.+)\)$', line)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid apply syntax at line {line_num}")
        transform_text = match.group(1).strip()
        new_col = match.group(2).strip()

        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', new_col):
            error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {new_col}")

        transform = self._parse_transform(transform_text, line_num)
        self.statements.append(('apply', (transform, new_col)))

    def _parse_group_by(self, line: str, line_num: int):
        match = re.match(r'group_by\((.+)\)$', line)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid group_by syntax at line {line_num}")
        expr_text = match.group(1).strip()

        if expr_text.startswith('['):
            match = re.match(r'^\[(.+)\]$', expr_text)
            if not match:
                error_exit(ErrorType.BAD_DSL, f"Invalid group_by list syntax at line {line_num}")
            cols_text = match.group(1)
            columns = [c.strip() for c in cols_text.split(',')]
            for col in columns:
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col):
                    error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {col}")
            self.statements.append(('group_by', columns))
        else:
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', expr_text):
                error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {expr_text}")
            self.statements.append(('group_by', [expr_text]))

    def _parse_window(self, line: str, line_num: int):
        match = re.match(r'window\((.+)\)$', line)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid window syntax at line {line_num}")
        duration_text = match.group(1).strip()

        match = re.match(r'^(\d+)(m|h|d)$', duration_text)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid duration literal at line {line_num}: {duration_text}")

        value = int(match.group(1))
        unit = match.group(2)

        self.statements.append(('window', (value, unit)))

    def _parse_aggregate(self, line: str, line_num: int):
        if not line.startswith('aggregate '):
            error_exit(ErrorType.BAD_DSL, f"Invalid aggregate syntax at line {line_num}")

        rest = line[len('aggregate '):].strip()
        if not rest.endswith(','):
            rest += ','

        aggregates = []
        current = ''
        depth = 0
        in_quote = False

        for char in rest:
            if char == "'":
                in_quote = not in_quote
                current += char
            elif char == '(' and not in_quote:
                depth += 1
                current += char
            elif char == ')' and not in_quote:
                depth -= 1
                current += char
            elif char == ',' and depth == 0 and not in_quote:
                current = current.strip()
                if current:
                    aggregates.append(self._parse_aggregate_entry(current, line_num))
                current = ''
            else:
                current += char

        if aggregates:
            self.statements.append(('aggregate', aggregates))

    def _parse_aggregate_entry(self, text: str, line_num: int) -> Tuple[str, str, str]:
        match = re.match(r'(\w+)\((.+)\)\s+as\s+(\w+)$', text)
        if not match:
            error_exit(ErrorType.BAD_DSL, f"Invalid aggregate syntax at line {line_num}: {text}")

        func = match.group(1)
        col = match.group(2).strip()
        alias = match.group(3)

        valid_funcs = {'average', 'median', 'sum', 'count', 'std', 'var', 'min', 'max'}
        if func not in valid_funcs:
            error_exit(ErrorType.BAD_DSL, f"Unknown aggregate function at line {line_num}: {func}")

        return (func, col, alias)

    def _parse_transform(self, text: str, line_num: int) -> TransformExpr:
        text = text.strip()

        match = re.match(r'len\((.+)\)$', text)
        if match:
            col_name = match.group(1).strip()
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col_name):
                error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {col_name}")
            return LenExpr(ColumnRef(col_name))

        for func_name, cls in [('day', DayExpr), ('month', MonthExpr), ('year', YearExpr)]:
            match = re.match(rf'{func_name}\((.+)\)$', text)
            if match:
                col_name = match.group(1).strip()
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col_name):
                    error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {col_name}")
                return cls(ColumnRef(col_name))

        match = re.match(r'round\((.+)(?:,(\d+))?\)$', text)
        if match:
            col_name = match.group(1).strip()
            digits_text = match.group(2)
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col_name):
                error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {col_name}")
            digits = int(digits_text) if digits_text else None
            return RoundExpr(ColumnRef(col_name), digits)

        error_exit(ErrorType.BAD_DSL, f"Unknown transform at line {line_num}: {text}")

    def _parse_bool_expr(self, text: str, line_num: int) -> Expr:
        match = re.match(r"^'(.*)'\s+in\s+([a-zA-Z_][a-zA-Z0-9_]*)$", text.strip())
        if match:
            literal = match.group(1)
            col_name = match.group(2)
            if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', col_name):
                error_exit(ErrorType.BAD_DSL, f"Invalid column name at line {line_num}: {col_name}")
            return InExpr(literal, ColumnRef(col_name))

        return self._parse_or(text.strip(), line_num)

    def _parse_or(self, text: str, line_num: int) -> Expr:
        parts = self._split_by_op(text, '|', line_num)
        if len(parts) == 1:
            return self._parse_and(parts[0], line_num)
        return BinaryOp(self._parse_or(parts[0], line_num), '|', self._parse_and(parts[1], line_num))

    def _parse_and(self, text: str, line_num: int) -> Expr:
        parts = self._split_by_op(text, '&', line_num)
        if len(parts) == 1:
            return self._parse_comparison(parts[0], line_num)
        return BinaryOp(self._parse_and(parts[0], line_num), '&', self._parse_comparison(parts[1], line_num))

    def _parse_comparison(self, text: str, line_num: int) -> Expr:
        text = text.strip()

        if text.startswith('!'):
            operand = self._parse_comparison(text[1:].strip(), line_num)
            return UnaryOp('!', operand)

        if text.startswith('(') and text.endswith(')'):
            inner = text[1:-1].strip()
            if '|' in inner or '&' in inner:
                return self._parse_bool_expr(inner, line_num)
            return self._parse_comparison(inner, line_num)

        operators = ['<=', '>=', '!=', '==', '<', '>']
        for op in operators:
            if op in text:
                parts = text.split(op, 1)
                if len(parts) == 2:
                    left = self._parse_value(parts[0].strip(), line_num)
                    right = self._parse_value(parts[1].strip(), line_num)
                    return BinaryOp(left, op, right)

        val = self._parse_value(text, line_num)
        return val

    def _parse_value(self, text: str, line_num: int) -> Expr:
        text = text.strip()

        if text == 'true':
            return Literal(True)
        if text == 'false':
            return Literal(False)

        if text.startswith("'") and text.endswith("'"):
            return Literal(text[1:-1])

        if re.match(r'^-?\d+$', text):
            return Literal(int(text))

        if re.match(r'^-?\d+\.\d*$', text):
            return Literal(float(text))
        if re.match(r'^-?\d*\.\d+$', text):
            return Literal(float(text))

        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', text):
            return ColumnRef(text)

        if text.startswith('(') and text.endswith(')'):
            inner = text[1:-1].strip()
            return self._parse_bool_expr(inner, line_num)

        error_exit(ErrorType.BAD_DSL, f"Invalid expression at line {line_num}: {text}")

    def _split_by_op(self, text: str, op: str, line_num: int) -> List[str]:
        parts = []
        current = ''
        depth = 0
        i = 0
        while i < len(text):
            char = text[i]
            if char == '(':
                depth += 1
                current += char
            elif char == ')':
                depth -= 1
                current += char
            elif depth == 0 and text[i:i+len(op)] == op:
                parts.append(current.strip())
                current = ''
                i += len(op) - 1
            else:
                current += char
            i += 1
        parts.append(current.strip())
        return parts

    def validate(self) -> dict:
        """Validate statements and return processed structure."""
        result = {
            'filters': [],
            'apply_ops': [],
            'group_by': None,
            'window': None,
            'aggregate': None
        }

        aggregate_count = 0
        group_by_count = 0
        window_count = 0

        for stmt_type, stmt_data in self.statements:
            if stmt_type == 'filter':
                result['filters'].append(stmt_data)
            elif stmt_type == 'apply':
                result['apply_ops'].append(stmt_data)
            elif stmt_type == 'group_by':
                group_by_count += 1
                if group_by_count > 1:
                    error_exit(ErrorType.BAD_DSL, "Multiple group_by statements not allowed")
                result['group_by'] = stmt_data
            elif stmt_type == 'window':
                window_count += 1
                if window_count > 1:
                    error_exit(ErrorType.BAD_DSL, "Multiple window statements not allowed")
                result['window'] = stmt_data
            elif stmt_type == 'aggregate':
                aggregate_count += 1
                if aggregate_count > 1:
                    error_exit(ErrorType.BAD_DSL, "Multiple aggregate statements not allowed")
                result['aggregate'] = stmt_data

        if aggregate_count != 1:
            error_exit(ErrorType.BAD_DSL, "Exactly one aggregate statement required")

        return result


# =============================================================================
# CSV Reader and Data processing
# =============================================================================

def parse_timestamp(ts_str: str, tz: timezone, timestamp_format: Optional[str] = None) -> datetime:
    """Parse a timestamp string."""
    try:
        if timestamp_format:
            dt = datetime.strptime(ts_str, timestamp_format)
            return dt.replace(tzinfo=tz)
        else:
            if 'T' in ts_str:
                if '+' in ts_str or ts_str.count('-') > 2:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                else:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
            else:
                dt = datetime.fromisoformat(ts_str + 'T00:00:00')
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
    except ValueError as e:
        raise RuntimeError(f"Invalid timestamp: {ts_str}") from e


def read_csv(filepath: str, tz: timezone, timestamp_format: Optional[str]) -> Tuple[List[Dict[str, Any]], Dict[str, DataType]]:
    """Read CSV file and infer types."""
    try:
        with open(filepath, 'r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            rows = []
            schema = {}

            for row in reader:
                processed_row = {}
                for col, val in row.items():
                    if col not in schema:
                        schema[col] = DataType.STRING

                    if col == 'timestamp':
                        try:
                            processed_row[col] = parse_timestamp(val, tz, timestamp_format)
                            schema[col] = DataType.TIMESTAMP
                        except Exception:
                            processed_row[col] = val
                    elif val is not None and val != '':
                        try:
                            if '.' in val:
                                processed_row[col] = float(val)
                                schema[col] = DataType.NUMBER
                            else:
                                int_val = int(val)
                                if str(int_val) == val:
                                    processed_row[col] = int_val
                                    schema[col] = DataType.INT
                                else:
                                    processed_row[col] = float(val)
                                    schema[col] = DataType.NUMBER
                        except ValueError:
                            processed_row[col] = val
                    else:
                        processed_row[col] = None

                rows.append(processed_row)

            return rows, schema
    except FileNotFoundError:
        error_exit(ErrorType.BAD_IO, f"CSV file not found: {filepath}")
    except PermissionError:
        error_exit(ErrorType.BAD_IO, f"Permission denied reading CSV: {filepath}")
    except Exception as e:
        error_exit(ErrorType.BAD_IO, f"Error reading CSV: {e}")


# =============================================================================
# Pipeline execution
# =============================================================================

class Pipeline:
    """Executes the transformation pipeline."""

    def __init__(self, dsl_text: str, csv_path: str, tz: timezone, timestamp_format: Optional[str]):
        self.parser = DSLParser(dsl_text)
        self.pipeline_config = self.parser.validate()
        self.tz = tz
        self.timestamp_format = timestamp_format

        self.rows, self.schema = read_csv(csv_path, tz, timestamp_format)
        self._execute_pipeline()

    def _execute_pipeline(self):
        for filter_expr in self.pipeline_config['filters']:
            self.rows = [row for row in self.rows if self._evaluate_bool(filter_expr, row)]

        for transform, new_col in self.pipeline_config['apply_ops']:
            for row in self.rows:
                try:
                    row[new_col] = transform.evaluate(row)
                    new_type = transform.get_type(self.schema)
                    self.schema[new_col] = new_type
                except RuntimeError as e:
                    error_exit(ErrorType.RUNTIME_ERROR, str(e))

        has_window = self.pipeline_config['window'] is not None
        has_group_by = self.pipeline_config['group_by'] is not None

        if has_window:
            self._apply_window()
            if has_group_by:
                self._apply_group_by_with_window()
            else:
                self._window_aggregate()
        elif has_group_by:
            self._apply_group_by_only()
        else:
            self._no_group_no_window_aggregate()

    def _evaluate_bool(self, expr: Expr, row: Dict[str, Any]) -> bool:
        try:
            result = expr.evaluate(row)
            return bool(result)
        except RuntimeError as e:
            error_exit(ErrorType.RUNTIME_ERROR, str(e))

    def _apply_window(self):
        duration_value, duration_unit = self.pipeline_config['window']

        if duration_unit == 'm':
            window_seconds = duration_value * 60
        elif duration_unit == 'h':
            window_seconds = duration_value * 3600
        elif duration_unit == 'd':
            window_seconds = duration_value * 86400
        else:
            error_exit(ErrorType.BAD_DSL, f"Invalid window unit: {duration_unit}")

        window_duration = timedelta(seconds=window_seconds)
        epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        for row in self.rows:
            ts = row.get('timestamp')
            if not isinstance(ts, datetime):
                error_exit(ErrorType.TYPE_ERROR, f"Window requires timestamp column, got {type(ts).__name__}")

            ts_utc = ts.astimezone(timezone.utc)
            ts_seconds = (ts_utc - epoch).total_seconds()
            window_start_seconds = math.floor(ts_seconds / window_seconds) * window_seconds
            window_start = epoch + timedelta(seconds=window_start_seconds)
            window_end = window_start + window_duration

            row['window_start'] = window_start
            row['window_end'] = window_end

    def _apply_group_by_only(self):
        group_cols = self.pipeline_config['group_by']

        for col in group_cols:
            if col not in self.schema:
                error_exit(ErrorType.TYPE_ERROR, f"Group-by column not found: {col}")

        groups = defaultdict(list)
        for row in self.rows:
            key = tuple(row.get(col) for col in group_cols)
            groups[key].append(row)

        self.output_rows = []
        for key, group_rows in groups.items():
            result = {}
            for i, col in enumerate(group_cols):
                result[col] = key[i]
            aggregates = self._compute_aggregates(group_rows)
            result.update(aggregates)
            self.output_rows.append((None, key, result))

        self._sort_output()

    def _apply_group_by_with_window(self):
        group_cols = self.pipeline_config['group_by']

        for col in group_cols:
            if col not in self.schema:
                error_exit(ErrorType.TYPE_ERROR, f"Group-by column not found: {col}")

        groups = defaultdict(list)
        for row in self.rows:
            if 'window_start' not in row or 'window_end' not in row:
                error_exit(ErrorType.RUNTIME_ERROR, "Window columns missing")

            window_start = row['window_start']
            group_key = tuple(row.get(col) for col in group_cols)
            groups[(window_start, group_key)].append(row)

        self.output_rows = []
        for (window_start, group_key), group_rows in groups.items():
            result = {}
            result['window_start'] = window_start
            result['window_end'] = group_rows[0]['window_end']

            for i, col in enumerate(group_cols):
                result[col] = group_key[i]

            aggregates = self._compute_aggregates(group_rows)
            result.update(aggregates)
            self.output_rows.append((window_start, group_key, result))

        self._sort_output()

    def _window_aggregate(self):
        groups = defaultdict(list)
        for row in self.rows:
            if 'window_start' not in row or 'window_end' not in row:
                error_exit(ErrorType.RUNTIME_ERROR, "Window columns missing")

            window_start = row['window_start']
            groups[window_start].append(row)

        self.output_rows = []
        for window_start, group_rows in groups.items():
            result = {}
            result['window_start'] = window_start
            result['window_end'] = group_rows[0]['window_end']

            aggregates = self._compute_aggregates(group_rows)
            result.update(aggregates)
            self.output_rows.append((window_start, (), result))

        self._sort_output()

    def _no_group_no_window_aggregate(self):
        aggregates = self._compute_aggregates(self.rows)
        self.output_rows = [(None, None, aggregates)]

    def _compute_aggregates(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        result = {}

        for func, col, alias in self.pipeline_config['aggregate']:
            try:
                if not rows:
                    if func == 'count' and col == '*':
                        result[alias] = 0
                    else:
                        result[alias] = None
                    continue

                if func == 'count':
                    if col == '*':
                        result[alias] = len(rows)
                    else:
                        count = sum(1 for r in rows if r.get(col) is not None)
                        result[alias] = count

                elif func == 'sum':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"sum() requires numeric column, got {type(v).__name__}")
                        result[alias] = sum(values)

                elif func == 'average':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"average() requires numeric column, got {type(v).__name__}")
                        result[alias] = sum(values) / len(values)

                elif func == 'median':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"median() requires numeric column, got {type(v).__name__}")
                        sorted_vals = sorted(values)
                        n = len(sorted_vals)
                        if n % 2 == 1:
                            result[alias] = sorted_vals[n // 2]
                        else:
                            result[alias] = (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2

                elif func == 'min':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"min() requires numeric column, got {type(v).__name__}")
                        result[alias] = min(values)

                elif func == 'max':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"max() requires numeric column, got {type(v).__name__}")
                        result[alias] = max(values)

                elif func == 'std':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"std() requires numeric column, got {type(v).__name__}")
                        n = len(values)
                        mean = sum(values) / n
                        variance = sum((v - mean) ** 2 for v in values) / n
                        result[alias] = math.sqrt(variance)

                elif func == 'var':
                    values = [r.get(col) for r in rows if r.get(col) is not None]
                    if not values:
                        result[alias] = None
                    else:
                        for v in values:
                            if not isinstance(v, (int, float)):
                                error_exit(ErrorType.TYPE_ERROR, f"var() requires numeric column, got {type(v).__name__}")
                        n = len(values)
                        mean = sum(values) / n
                        result[alias] = sum((v - mean) ** 2 for v in values) / n

            except RuntimeError as e:
                error_exit(ErrorType.RUNTIME_ERROR, str(e))
            except ZeroDivisionError as e:
                error_exit(ErrorType.RUNTIME_ERROR, "Division by zero")

        return result

    def _sort_output(self):
        def sort_key(item):
            window_start, group_key, result = item
            ws_key = 0 if window_start is None else window_start.timestamp()

            group_tuple = tuple(
                str(v) if isinstance(v, str) else
                float(v) if isinstance(v, (int, float)) else
                v.timestamp() if isinstance(v, datetime) else
                v
                for v in group_key
            )

            return (ws_key, group_tuple)

        self.output_rows.sort(key=sort_key)

    def get_output(self) -> Union[Dict, List]:
        has_window = self.pipeline_config['window'] is not None
        has_group_by = self.pipeline_config['group_by'] is not None

        if len(self.output_rows) == 1:
            _, _, result = self.output_rows[0]
            if not has_window and not has_group_by:
                return dict(sorted(result.items()))

        ordered_results = []
        for window_start, group_key, result in self.output_rows:
            ordered_result = self._order_result(result, has_window, has_group_by)
            ordered_results.append(ordered_result)

        if not has_window and not has_group_by:
            return ordered_results[0]

        return ordered_results

    def _order_result(self, result: Dict, has_window: bool, has_group_by: bool) -> Dict:
        ordered = {}

        if has_window:
            ordered['window_start'] = result['window_start']
            ordered['window_end'] = result['window_end']

        if has_group_by:
            group_cols = self.pipeline_config['group_by']
            for col in group_cols:
                ordered[col] = result[col]

        aggregate_aliases = [alias for _, _, alias in self.pipeline_config['aggregate']]
        aggregate_aliases.sort()

        for alias in aggregate_aliases:
            for func, col, alias_name in self.pipeline_config['aggregate']:
                if alias_name == alias:
                    ordered[alias] = result[alias]
                    break

        return ordered


def datetime_serializer(obj):
    """Custom JSON serializer for datetime objects."""
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        utc_obj = obj.astimezone(timezone.utc)
        return utc_obj.strftime('%Y-%m-%dT%H:%M:%SZ')
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# =============================================================================
# Main CLI
# =============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description='MTL - Mini Transformation Language for sales events'
    )
    parser.add_argument('--csv', required=True, help='Path to input CSV file')
    parser.add_argument('--dsl', required=True, help='Path to DSL file (or - for stdin)')
    parser.add_argument('--out', default=None, help='Output file (default: stdout)')
    parser.add_argument('--tz', default='UTC', help='Default timezone (default: UTC)')
    parser.add_argument('--timestamp-format', default=None, help='Timestamp format string')

    return parser.parse_args()


def main():
    args = parse_args()

    try:
        if args.dsl == '-':
            dsl_text = sys.stdin.read()
        else:
            with open(args.dsl, 'r', encoding='utf-8') as f:
                dsl_text = f.read()
    except FileNotFoundError:
        error_exit(ErrorType.BAD_IO, f"DSL file not found: {args.dsl}")
    except PermissionError:
        error_exit(ErrorType.BAD_IO, f"Permission denied reading DSL: {args.dsl}")
    except Exception as e:
        error_exit(ErrorType.BAD_IO, f"Error reading DSL: {e}")

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(args.tz)
    except Exception:
        error_exit(ErrorType.BAD_IO, f"Invalid timezone: {args.tz}")

    try:
        pipeline = Pipeline(dsl_text, args.csv, tz, args.timestamp_format)
        output = pipeline.get_output()
    except Exception as e:
        error_exit(ErrorType.RUNTIME_ERROR, str(e))

    try:
        output_json = json.dumps(output, indent=2, default=datetime_serializer, ensure_ascii=False)

        if args.out:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(output_json)
                f.write('\n')
        else:
            print(output_json)
    except Exception as e:
        error_exit(ErrorType.BAD_IO, f"Error writing output: {e}")


if __name__ == '__main__':
    main()
