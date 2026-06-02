#!/usr/bin/env python3
"""MTL (Market Transformation Language) - file-based CLI for ad-hoc rollups with schemas and params."""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# --- Exit codes ---
EXIT_OK = 0
EXIT_BAD_DSL = 1
EXIT_TYPE_ERROR = 2
EXIT_BAD_IO = 3
EXIT_RUNTIME_ERROR = 4

# --- Types ---

class MTLType(Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    TIMESTAMP = "timestamp"

    @classmethod
    def from_string(cls, s: str) -> MTLType:
        mapping = {
            "string": cls.STRING,
            "int": cls.INT,
            "float": cls.FLOAT,
            "bool": cls.BOOL,
            "timestamp": cls.TIMESTAMP,
        }
        if s not in mapping:
            raise SyntaxError(f"Unknown type: {s}")
        return mapping[s]

    def is_numeric(self) -> bool:
        return self in (MTLType.INT, MTLType.FLOAT)

    def can_widen_to(self, other: MTLType) -> bool:
        """Check if this type can be widened to 'other' (numeric widening only)."""
        if self == other:
            return True
        # INT can widen to FLOAT
        if self == MTLType.INT and other == MTLType.FLOAT:
            return True
        # FLOAT cannot widen to INT (narrowing is illegal)
        return False


@dataclass
class ColumnInfo:
    name: str
    mtype: MTLType
    pandas_dtype: str
    is_physical: bool = True  # True if column exists in CSV, False if derived/calculated


@dataclass
class WindowSpec:
    duration: timedelta


@dataclass
class GroupSpec:
    columns: List[str]


@dataclass
class Aggregation:
    fn: str
    column: str  # or "*" for count
    alias: str


@dataclass
class SchemaField:
    name: str
    mtype: MTLType
    expr: Optional[str] = None  # Optional calculated expression (schema-level)
    stage: str = "pre"  # Schema calcs are always pre-stage


@dataclass
class Schema:
    name: str
    fields: Dict[str, SchemaField] = field(default_factory=dict)
    base: Optional[str] = None  # Name of base schema


@dataclass
class ParamInfo:
    name: str
    mtype: Optional[MTLType] = None  # None = string type inferred from usage
    default: Optional[str] = None  # Literal string from DSL
    value: Any = None  # Resolved value (after CLI params merged)


@dataclass
class CalcInfo:
    name: str
    expr: str
    mtype: MTLType
    stage: str  # "pre" or "post_agg"
    parsed_ast: Optional[ast.AST] = None


@dataclass
class Pipeline:
    name: str
    using_schema: str
    params: List[ParamInfo] = field(default_factory=list)
    filters: List[Tuple[str, ast.AST]] = field(default_factory=list)
    applies: List[Tuple[str, str, ast.AST]] = field(default_factory=list)  # (transform_expr, new_col, ast)
    group_by: Optional[GroupSpec] = None
    window: Optional[WindowSpec] = None
    calcs: List[CalcInfo] = field(default_factory=list)  # Includes both pre and post_agg calcs
    aggregates: List[Aggregation] = field(default_factory=list)

    def __post_init__(self):
        if self.aggregates is None:
            self.aggregates = []


# --- DSL Parser ---

class DSLParser:
    """Parser for extended MTL DSL with schemas, params, and calcs."""

    IDENTIFIER = r'[a-zA-Z_][a-zA-Z0-9_]*'
    STRING_LITERAL = r"'[^']*'"
    NUMBER_LITERAL = r'\d+\.?\d*'
    BOOL_LITERAL = r'true|false'
    TYPE_NAME = r'string|int|float|bool|timestamp'
    DURATION_UNIT = r'm|h|d'
    DURATION = rf'\d+{DURATION_UNIT}'

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split('\n')
        self.line_num = 0
        self.schemas: Dict[str, Schema] = {}
        self.pipeline: Optional[Pipeline] = None

    def _ peek(self, n: int = 1) -> str:
        """Peek at remaining text."""
        return ''.join(self.lines[self.line_num:self.line_num+n])

    def _read_line(self) -> str:
        """Read and advance one line."""
        if self.line_num >= len(self.lines):
            return ''
        line = self.lines[self.line_num]
        self.line_num += 1
        return line

    def _read_while(self, predicate) -> str:
        """Read consecutive lines matching predicate."""
        result = []
        while self.line_num < len(self.lines) and predicate(self.lines[self.line_num]):
            result.append(self.lines[self.line_num])
            self.line_num += 1
        return '\n'.join(result)

    def skip_ws_and_comments(self):
        while self.line_num < len(self.lines):
            line = self.lines[self.line_num].strip()
            if not line or line.startswith('#'):
                self.line_num += 1
            else:
                break

    def _expect(self, token: str, msg: str = None):
        """Expect specific token at current position."""
        line = self.lines[self.line_num].strip()
        if not line.startswith(token):
            raise SyntaxError(f"Expected '{token}', got: {line}")
        self.line_num += 1

    def parse(self) -> Pipeline:
        """Parse entire DSL into schemas and Pipeline."""
        # Parse schema blocks
        while self.line_num < len(self.lines):
            self.skip_ws_and_comments()
            if self.line_num >= len(self.lines):
                break
            line = self.lines[self.line_num].strip()

            if line.startswith('schema '):
                self._parse_schema_block()
            elif line.startswith('pipeline '):
                break
            else:
                raise SyntaxError(f"Expected schema or pipeline, got: {line}")

        if self.pipeline is None:
            raise SyntaxError("No pipeline block found")

        # Resolve schema inheritance to get final field types for validation
        return self.pipeline

    def _parse_schema_block(self):
        """Parse a schema block."""
        line = self.lines[self.line_num].strip()
        m = re.match(r'schema\s+(' + self.IDENTIFIER + r')(?:\s+extends\s+(' + self.IDENTIFIER + r'))?\s*\{', line)
        if not m:
            raise SyntaxError(f"Invalid schema declaration: {line}")

        name = m.group(1)
        base = m.group(2)
        self.line_num += 1

        # Check for cycles (will validate after all schemas are parsed)
        if base and base in self.schemas:
            # Check if base inherits from this new schema (cycle detection deferred)
            pass

        schema = Schema(name=name, base=base)
        self.schemas[name] = schema

        # Parse fields until closing brace
        while self.line_num < len(self.lines):
            line = self.lines[self.line_num].strip()
            if line == '}':
                self.line_num += 1
                break
            if line.startswith('#') or not line:
                self.line_num += 1
                continue

            # Parse field: <field_name>: <type> [= <expr>]
            m = re.match(r'^(' + self.IDENTIFIER + r')\s*:\s*(' + self.TYPE_NAME + r')(\s*=\s*(.+))?$', line)
            if not m:
                raise SyntaxError(f"Invalid schema field: {line}")

            field_name = m.group(1)
            mtype = MTLType.from_string(m.group(2))
            expr = m.group(3)
            if expr:
                expr = expr.strip()

            schema.fields[field_name] = SchemaField(
                name=field_name,
                mtype=mtype,
                expr=expr,
                stage="pre"
            )
            self.line_num += 1

    def _parse_pipeline_block(self):
        """Parse the pipeline block."""
        line = self.lines[self.line_num].strip()
        m = re.match(r'pipeline\s+(' + self.IDENTIFIER + r')\s+using\s+(' + self.IDENTIFIER + r')\s*\{', line)
        if not m:
            raise SyntaxError(f"Invalid pipeline declaration: {line}")

        name = m.group(1)
        using = m.group(2)
        self.line_num += 1

        self.pipeline = Pipeline(name=name, using_schema=using)

        # Parse params block if present
        self.skip_ws_and_comments()
        line = self.lines[self.line_num].strip() if self.line_num < len(self.lines) else ''

        if line == 'params {':
            self.line_num += 1
            self._parse_params_block()
            self.skip_ws_and_comments()
            if self.line_num < len(self.lines):
                line = self.lines[self.line_num].strip()
            else:
                line = ''

        # Parse body statements
        seen_agg = False
        while self.line_num < len(self.lines):
            line = self.lines[self.line_num].strip()
            if not line or line.startswith('#'):
                self.line_num += 1
                continue
            if line == '}':
                self.line_num += 1
                break

            try:
                if line.startswith('filter(') and line.endswith(')'):
                    expr = line[7:-1].strip()
                    parsed = self._parse_bool_expr(expr)
                    self.pipeline.filters.append((expr, parsed))
                    self.line_num += 1

                elif line.startswith('apply(') and line.endswith(')'):
                    rest = line[6:-1].strip()
                    comma_idx = rest.rfind(',')
                    if comma_idx == -1:
                        raise SyntaxError("apply() requires transform, new_col")
                    transform = rest[:comma_idx].strip()
                    new_col = rest[comma_idx+1:].strip()
                    if not re.fullmatch(self.IDENTIFIER, new_col):
                        raise SyntaxError(f"Invalid column name: {new_col}")
                    parsed = self._parse_transform_expr(transform)
                    self.pipeline.applies.append((transform, new_col, parsed))
                    self.line_num += 1

                elif line.startswith('group_by(') and line.endswith(')'):
                    if self.pipeline.group_by is not None:
                        raise SyntaxError("Multiple group_by not allowed")
                    rest = line[9:-1].strip()
                    if rest.startswith('[') and rest.endswith(']'):
                        cols_str = rest[1:-1]
                        columns = [c.strip() for c in cols_str.split(',')]
                        columns = [c for c in columns if c]
                    else:
                        columns = [rest]
                    for col in columns:
                        if not re.fullmatch(self.IDENTIFIER, col):
                            raise SyntaxError(f"Invalid column name: {col}")
                    self.pipeline.group_by = GroupSpec(columns)
                    self.line_num += 1

                elif line.startswith('window(') and line.endswith(')'):
                    if self.pipeline.window is not None:
                        raise SyntaxError("Multiple window not allowed")
                    rest = line[7:-1].strip()
                    if not re.fullmatch(self.DURATION, rest):
                        raise SyntaxError(f"Invalid duration literal: {rest}")
                    duration = self._parse_duration(rest)
                    self.pipeline.window = WindowSpec(duration)
                    self.line_num += 1

                elif line.lower().startswith('aggregate'):
                    if seen_agg:
                        raise SyntaxError("Multiple aggregate not allowed")
                    seen_agg = True
                    rest = line[9:].strip()
                    if not rest:
                        raise SyntaxError("aggregate requires a list")
                    rest = rest.strip()
                    aggregations = self._parse_aggregates(rest)
                    self.pipeline.aggregates = aggregations
                    self.line_num += 1

                elif line.startswith('calc '):
                    # Parse calc statement
                    calc = self._parse_calc_statement(line)
                    self.pipeline.calcs.append(calc)
                    self.line_num += 1

                else:
                    raise SyntaxError(f"Unknown statement: {line}")

            except SyntaxError as e:
                raise SyntaxError(f"Line {self.line_num}: {e}")

        if not seen_agg or not self.pipeline.aggregates:
            raise SyntaxError("Exactly one aggregate statement is required")

    def _parse_params_block(self):
        """Parse the params block."""
        while self.line_num < len(self.lines):
            line = self.lines[self.line_num].strip()
            if line == '}':
                self.line_num += 1
                break
            if line.startswith('#') or not line:
                self.line_num += 1
                continue

            # Parse: <name> [:<type>] [= <default_literal>]
            m = re.match(r'^(' + self.IDENTIFIER + r')(\s*:\s*(' + self.TYPE_NAME + r'))?(\s*=\s*(.+))?$', line)
            if not m:
                raise SyntaxError(f"Invalid param declaration: {line}")

            name = m.group(1)
            ptype = MTLType.from_string(m.group(3)) if m.group(3) else None
            default = m.group(5)
            if default:
                default = default.strip()

            self.pipeline.params.append(ParamInfo(name=name, mtype=ptype, default=default))
            self.line_num += 1

    def _parse_calc_statement(self, line: str) -> CalcInfo:
        """Parse a calc statement: calc <name> = <expr> : <type> @stage(pre|post_agg)"""
        m = re.match(r'calc\s+(' + self.IDENTIFIER + r')\s*=\s*(.+)\s*:\s*(' + self.TYPE_NAME + r')\s*@stage\((' + self.IDENTIFIER + r')\)', line)
        if not m:
            raise SyntaxError(f"Invalid calc statement: {line}")

        name = m.group(1)
        expr = m.group(2).strip()
        mtype = MTLType.from_string(m.group(3))
        stage = m.group(4)

        if stage not in ('pre', 'post_agg'):
            raise SyntaxError(f"Invalid calc stage: {stage}")

        parsed = self._parse_expr(expr, stage)

        return CalcInfo(name=name, expr=expr, mtype=mtype, stage=stage, parsed_ast=parsed)

    def _parse_duration(self, lit: str) -> timedelta:
        """Parse duration like '10m', '1h', '7d'."""
        match = re.fullmatch(r'(\d+)(m|h|d)', lit)
        if not match:
            raise SyntaxError(f"Invalid duration: {lit}")
        val, unit = int(match.group(1)), match.group(2)
        if unit == 'm':
            return timedelta(minutes=val)
        elif unit == 'h':
            return timedelta(hours=val)
        elif unit == 'd':
            return timedelta(days=val)
        raise SyntaxError(f"Invalid duration unit: {unit}")

    def _parse_aggregates(self, text: str) -> List[Aggregation]:
        """Parse aggregate list."""
        parts = []
        current = ''
        i = 0
        paren_depth = 0
        while i < len(text):
            if text[i] == '(':
                paren_depth += 1
            elif text[i] == ')':
                paren_depth -= 1
            if text[i] == ',' and paren_depth == 0:
                parts.append(current.strip())
                current = ''
                i += 1
                while i < len(text) and text[i].isspace():
                    i += 1
                continue
            current += text[i]
            i += 1
        if current.strip():
            parts.append(current.strip())

        aggregates = []
        for part in parts:
            if not part:
                continue
            open_paren = part.find('(')
            if open_paren == -1:
                raise SyntaxError(f"Invalid aggregate format (missing '('): {part}")
            close_paren = part.rfind(')')
            if close_paren == -1:
                raise SyntaxError(f"Invalid aggregate format (missing ')'): {part}")
            fn = part[:open_paren].strip()
            col = part[open_paren+1:close_paren].strip()
            as_idx = part.find(' as ', close_paren)
            if as_idx == -1:
                raise SyntaxError(f"Invalid aggregate format (missing ' as'): {part}")
            alias = part[as_idx+4:].strip()
            if not re.fullmatch(self.IDENTIFIER, alias):
                raise SyntaxError(f"Invalid aggregate alias: {alias}")
            if not re.fullmatch(self.IDENTIFIER, fn):
                raise SyntaxError(f"Invalid aggregate function: {fn}")
            aggregates.append(Aggregation(fn=fn, column=col, alias=alias))

        seen = set()
        for a in aggregates:
            if a.alias in seen:
                raise SyntaxError(f"Duplicate aggregate alias: {a.alias}")
            seen.add(a.alias)

        return aggregates

    def _parse_transform_expr(self, expr: str) -> ast.AST:
        """Parse transform expression into AST."""
        processed = self._preprocess_expr(expr)
        return self._safe_parse(processed, mode='eval')

    def _parse_bool_expr(self, expr: str) -> ast.AST:
        """Parse boolean expression into AST."""
        processed = self._preprocess_expr(expr)
        return self._safe_parse(processed, mode='eval')

    def _parse_expr(self, expr: str, stage: str) -> ast.AST:
        """Parse a general expression, handling params, lag/lead, etc."""
        # Preprocess for new constructs
        processed = self._preprocess_general_expr(expr, stage)
        return self._safe_parse(processed, mode='eval')

    def _preprocess_general_expr(self, expr: str, stage: str) -> str:
        """Preprocess expression for Python AST parsing with new constructs."""
        result = []
        i = 0

        while i < len(expr):
            # Handle param("name") -> PARAM(name)
            if expr[i:i+6].lower() == 'param(':
                j = i + 6
                # Find the string argument
                if j < len(expr) and expr[j] == '"':
                    j += 1
                    start = j
                    while j < len(expr) and expr[j] != '"':
                        j += 1
                    if j >= len(expr):
                        raise SyntaxError("Unterminated param() string")
                    param_name = expr[start:j]
                    # Validate identifier
                    if not re.fullmatch(self.IDENTIFIER, param_name):
                        raise SyntaxError(f"Invalid param name: {param_name}")
                    result.append(f'PARAM("{param_name}")')
                    i = j + 1
                    continue
                elif j < len(expr) and expr[j] == "':
                    j += 1
                    start = j
                    while j < len(expr) and expr[j] != "'":
                        j += 1
                    if j >= len(expr):
                        raise SyntaxError("Unterminated param() string")
                    param_name = expr[start:j]
                    if not re.fullmatch(self.IDENTIFIER, param_name):
                        raise SyntaxError(f"Invalid param name: {param_name}")
                    result.append(f'PARAM("{param_name}")')
                    i = j + 1
                    continue
                else:
                    raise SyntaxError("param() expects string literal")

            # Handle lag(expr, k) -> LAG(expr, k)
            if expr[i:i+3].lower() == 'lag(':
                if stage != 'post_agg':
                    raise SyntaxError("lag() is only valid in @stage(post_agg) expressions")
                paren_depth = 1
                j = i + 3
                start_expr = j
                while j < len(expr) and paren_depth > 0:
                    if expr[j] == '(':
                        paren_depth += 1
                    elif expr[j] == ')':
                        paren_depth -= 1
                    j += 1
                if paren_depth > 0:
                    raise SyntaxError("Unterminated lag()")
                inner = expr[start_expr:j-1].strip()
                # Find the comma separating expr and k
                comma_idx = -1
                depth = 0
                for k in range(len(inner)):
                    if inner[k] == '(':
                        depth += 1
                    elif inner[k] == ')':
                        depth -= 1
                    elif inner[k] == ',' and depth == 0:
                        comma_idx = k
                        break
                if comma_idx == -1:
                    raise SyntaxError("lag() requires expr and k parameters")
                lag_expr = inner[:comma_idx].strip()
                k_str = inner[comma_idx+1:].strip()
                # Validate k is integer literal or param reference
                k_valid = False
                if k_str.isdigit() or (k_str.startswith('-') and k_str[1:].isdigit()):
                    k_valid = True
                elif k_str.startswith('param('):
                    # Check it's a valid param call
                    k_valid = 'param(' in k_str.lower()  # Will be validated later
                if not k_valid:
                    raise SyntaxError("lag() k must be an integer literal or param()")
                result.append(f'LAG({lag_expr}, {k_str})')
                i = j
                continue

            # Handle lead(expr, k) -> LEAD(expr, k)
            if expr[i:i+5].lower() == 'lead(':
                if stage != 'post_agg':
                    raise SyntaxError("lead() is only valid in @stage(post_agg) expressions")
                paren_depth = 1
                j = i + 5
                start_expr = j
                while j < len(expr) and paren_depth > 0:
                    if expr[j] == '(':
                        paren_depth += 1
                    elif expr[j] == ')':
                        paren_depth -= 1
                    j += 1
                if paren_depth > 0:
                    raise SyntaxError("Unterminated lead()")
                inner = expr[start_expr:j-1].strip()
                comma_idx = -1
                depth = 0
                for k in range(len(inner)):
                    if inner[k] == '(':
                        depth += 1
                    elif inner[k] == ')':
                        depth -= 1
                    elif inner[k] == ',' and depth == 0:
                        comma_idx = k
                        break
                if comma_idx == -1:
                    raise SyntaxError("lead() requires expr and k parameters")
                lead_expr = inner[:comma_idx].strip()
                k_str = inner[comma_idx+1:].strip()
                if not k_str.isdigit():
                    raise SyntaxError("lead() k must be an integer literal")
                result.append(f'LEAD({lead_expr}, {k_str})')
                i = j
                continue

            # Handle coalesce(a, b) -> COALESCE(a, b)
            if expr[i:i+9].lower() == 'coalesce' and expr[i+9] == '(':
                paren_depth = 1
                j = i + 9
                start = j
                while j < len(expr) and paren_depth > 0:
                    if expr[j] == '(':
                        paren_depth += 1
                    elif expr[j] == ')':
                        paren_depth -= 1
                    j += 1
                if paren_depth > 0:
                    raise SyntaxError("Unterminated coalesce()")
                inner = expr[start:j-1].strip()
                result.append(f'COALESCE({inner})')
                i = j
                continue

            # Handle cast(expr as type) -> CAST(expr, type)
            if expr[i:i+4].lower() == 'cast' and expr[i+4] == '(':
                paren_depth = 1
                j = i + 4
                start = j
                while j < len(expr) and paren_depth > 0:
                    if expr[j] == '(':
                        paren_depth += 1
                    elif expr[j] == ')':
                        paren_depth -= 1
                    j += 1
                if paren_depth > 0:
                    raise SyntaxError("Unterminated cast()")
                inner = expr[start:j-1].strip()
                # Parse: expr as type
                parts = inner.split()
                if len(parts) < 3 or parts[-2].lower() != 'as':
                    raise SyntaxError("cast() requires: expr as type")
                expr_part = ' '.join(parts[:-2])
                type_part = parts[-1]
                result.append(f'CAST({expr_part}, \"{type_part}\")')
                i = j
                continue

            # Handle string methods: starts_with, ends_with, contains
            # These are handled as function calls: starts_with(str, prefix)
            for fn in ['starts_with', 'ends_with', 'contains']:
                fn_len = len(fn)
                if expr[i:i+fn_len].lower() == fn and i+fn_len < len(expr) and expr[i+fn_len] == '(':
                    # Just pass through as function call - will be validated
                    paren_depth = 1
                    j = i + fn_len + 1
                    while j < len(expr) and paren_depth > 0:
                        if expr[j] == '(':
                            paren_depth += 1
                        elif expr[j] == ')':
                            paren_depth -= 1
                        j += 1
                    result.append(expr[i:j])
                    i = j
                    continue

            # Handle boolean literal 'true'/'false'
            if expr[i:i+4] == 'true':
                if i == 0 or not expr[i-1].isalnum() and expr[i-1] != '_':
                    result.append('True')
                    i += 4
                    continue
            if expr[i:i+5] == 'false':
                if i == 0 or not expr[i-1].isalnum() and expr[i-1] != '_':
                    result.append('False')
                    i += 5
                    continue

            result.append(expr[i])
            i += 1

        return ''.join(result)

    def _preprocess_expr(self, expr: str) -> str:
        """Preprocess expression for Python AST parsing (Part 1 compatibility)."""
        # Reuse general preprocess
        return self._preprocess_general_expr(expr, "pre")

    def _safe_parse(self, expr: str, mode: str) -> ast.AST:
        """Safely parse expression."""
        try:
            tree = ast.parse(expr, mode=mode)
        except SyntaxError as e:
            raise SyntaxError(f"Syntax error: {e}")
        return tree


# --- Type Checker (Extended) ---

class TypeChecker(ast.NodeVisitor):
    """Type check expressions against column/schema environment."""

    def __init__(self, columns: Dict[str, ColumnInfo], params: Dict[str, Tuple[MTLType, Any]], stage: str):
        self.columns = columns
        self.params = params
        self.stage = stage
        self.errors = []

    def check(self, node: ast.AST) -> MTLType:
        """Check type of expression, return inferred type."""
        if isinstance(node, ast.Expression):
            node = node.body
        try:
            return self.visit(node)
        except TypeError as e:
            raise TypeError(f"Type error: {e}")

    def visit(self, node):
        return super().visit(node)

    def _resolve_column_type(self, name: str) -> MTLType:
        """Resolve a column reference to its type."""
        if name in self.columns:
            return self.columns[name].mtype
        # Could be a param reference via PARAM() function
        raise TypeError(f"Unknown field: {name}")

    def visit_BoolOp(self, node):
        left_type = self.check(node.values[0])
        for val in node.values[1:]:
            right_type = self.check(val)
            if left_type != right_type:
                raise TypeError(f"Type mismatch in boolean op: {left_type} vs {right_type}")
        return MTLType.BOOL

    def visit_UnaryOp(self, node):
        operand_type = self.check(node.operand)
        if isinstance(node.op, ast.Not):
            if operand_type != MTLType.BOOL:
                raise TypeError(f"! requires bool, got {operand_type}")
            return MTLType.BOOL
        if isinstance(node.op, ast.USub):
            if operand_type not in (MTLType.INT, MTLType.FLOAT):
                raise TypeError(f"Unary - requires numeric, got {operand_type}")
            return operand_type
        raise TypeError(f"Unknown unary op: {node.op}")

    def visit_BinOp(self, node):
        left_type = self.check(node.left)
        right_type = self.check(node.right)

        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            if left_type not in (MTLType.INT, MTLType.FLOAT):
                raise TypeError(f"Numeric op left: {left_type}")
            if right_type not in (MTLType.INT, MTLType.FLOAT):
                raise TypeError(f"Numeric op right: {right_type}")
            if left_type == MTLType.FLOAT or right_type == MTLType.FLOAT:
                return MTLType.FLOAT
            return MTLType.INT
        if isinstance(node.op, ast.Mod):
            if left_type not in (MTLType.INT, MTLType.FLOAT) or right_type not in (MTLType.INT, MTLType.FLOAT):
                raise TypeError(f"% requires numeric operands")
            return MTLType.INT
        if isinstance(node.op, ast.Eq):
            if left_type != right_type:
                raise TypeError(f"== requires same type, got {left_type} vs {right_type}")
            return MTLType.BOOL
        if isinstance(node.op, ast.NotEq):
            if left_type != right_type:
                raise TypeError(f"!= requires same type, got {left_type} vs {right_type}")
            return MTLType.BOOL
        if isinstance(node.op, ast.LtE):
            if left_type != right_type:
                raise TypeError(f"<= requires same type")
            return MTLType.BOOL
        if isinstance(node.op, ast.Lt):
            if left_type != right_type:
                raise TypeError(f"< requires same type")
            return MTLType.BOOL
        if isinstance(node.op, ast.GtE):
            if left_type != right_type:
                raise TypeError(f">= requires same type")
            return MTLType.BOOL
        if isinstance(node.op, ast.Gt):
            if left_type != right_type:
                raise TypeError(f"> requires same type")
            return MTLType.BOOL
        if isinstance(node.op, ast.And):
            return MTLType.BOOL
        if isinstance(node.op, ast.Or):
            return MTLType.BOOL
        raise TypeError(f"Unknown binop: {node.op}")

    def visit_Compare(self, node):
        left_type = self.check(node.left)
        result = MTLType.BOOL

        for op, comparator in zip(node.ops, node.comparators):
            right_type = self.check(comparator)
            if left_type != right_type:
                raise TypeError(f"Comparison type mismatch: {left_type} vs {right_type}")
            if isinstance(op, (ast.Eq, ast.NotEq)):
                pass
            elif isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                if left_type not in (MTLType.INT, MTLType.FLOAT, MTLType.STRING, MTLType.TIMESTAMP):
                    raise TypeError(f"Comparison op not supported for {left_type}")
            else:
                raise TypeError(f"Unknown compare op: {op}")
            left_type = right_type

        return result

    def visit_Name(self, node):
        name = node.id
        if name in self.columns:
            return self.columns[name].mtype
        if name == 'True' or name == 'False':
            return MTLType.BOOL
        raise TypeError(f"Unknown field: {name}")

    def visit_Constant(self, node):
        val = node.value
        if val is None:
            raise TypeError("None literals not allowed")
        if isinstance(val, bool):
            return MTLType.BOOL
        if isinstance(val, int):
            return MTLType.INT
        if isinstance(val, float):
            return MTLType.FLOAT
        if isinstance(val, str):
            return MTLType.STRING
        raise TypeError(f"Unknown literal type: {type(val)}")

    def visit_Num(self, node):
        if isinstance(node.n, int):
            return MTLType.INT
        return MTLType.FLOAT

    def visit_Str(self, node):
        return MTLType.STRING

    def visit_NameConstant(self, node):
        return MTLType.BOOL

    def generic_visit(self, node):
        if isinstance(node, ast.Call):
            return self._visit_Call(node)
        raise TypeError(f"Unsupported expression: {type(node).__name__}")

    def _visit_Call(self, node: ast.Call) -> MTLType:
        if not isinstance(node.func, ast.Name):
            raise TypeError("Only simple function calls allowed")

        fn = node.func.id

        if fn == 'len':
            if len(node.args) != 1:
                raise TypeError("len() takes exactly 1 argument")
            arg_type = self.check(node.args[0])
            if arg_type != MTLType.STRING:
                raise TypeError(f"len() requires string, got {arg_type}")
            return MTLType.INT

        elif fn == 'day':
            if len(node.args) != 1:
                raise TypeError("day() takes exactly 1 argument")
            arg_type = self.check(node.args[0])
            if arg_type != MTLType.TIMESTAMP:
                raise TypeError(f"day() requires timestamp, got {arg_type}")
            return MTLType.INT

        elif fn == 'month':
            if len(node.args) != 1:
                raise TypeError("month() takes exactly 1 argument")
            arg_type = self.check(node.args[0])
            if arg_type != MTLType.TIMESTAMP:
                raise TypeError(f"month() requires timestamp, got {arg_type}")
            return MTLType.INT

        elif fn == 'year':
            if len(node.args) != 1:
                raise TypeError("year() takes exactly 1 argument")
            arg_type = self.check(node.args[0])
            if arg_type != MTLType.TIMESTAMP:
                raise TypeError(f"year() requires timestamp, got {arg_type}")
            return MTLType.INT

        elif fn == 'round':
            if len(node.args) < 1 or len(node.args) > 2:
                raise TypeError("round() takes 1-2 arguments")
            arg_type = self.check(node.args[0])
            if arg_type not in (MTLType.INT, MTLType.FLOAT):
                raise TypeError(f"round() requires numeric, got {arg_type}")
            if len(node.args) == 2:
                digits_type = self.check(node.args[1])
                if digits_type != MTLType.INT:
                    raise TypeError(f"round() digits must be int, got {digits_type}")
            return MTLType.FLOAT

        elif fn == 'CONTAINS':
            if len(node.args) != 2:
                raise TypeError("CONTAINS() takes 2 arguments")
            col_type = self.check(node.args[0])
            lit_type = self.check(node.args[1])
            if col_type != MTLType.STRING:
                raise TypeError(f"CONTAINS() first arg must be string, got {col_type}")
            if lit_type != MTLType.STRING:
                raise TypeError(f"CONTAINS() second arg must be string literal, got {lit_type}")
            return MTLType.BOOL

        elif fn == 'PARAM':
            if len(node.args) != 1:
                raise TypeError("PARAM() takes exactly 1 argument")
            arg_type = self.check(node.args[0])
            if arg_type != MTLType.STRING:
                raise TypeError("PARAM() argument must be string literal")
            # The param name is in the AST constant
            if isinstance(node.args[0], ast.Constant):
                param_name = node.args[0].value
            elif hasattr(ast, 'Str') and isinstance(node.args[0], ast.Str):
                param_name = node.args[0].s
            else:
                raise TypeError("PARAM() argument must be string literal")
            if not isinstance(param_name, str):
                raise TypeError("PARAM() argument must be string literal")
            if param_name not in self.params:
                raise TypeError(f"Unknown param: {param_name}")
            ptype, _ = self.params[param_name]
            return ptype

        elif fn == 'LAG':
            if len(node.args) != 2:
                raise TypeError("LAG() takes 2 arguments")
            expr_type = self.check(node.args[0])
            k_type = self.check(node.args[1])
            if k_type != MTLType.INT:
                raise TypeError(f"LAG() k must be int, got {k_type}")
            return expr_type

        elif fn == 'LEAD':
            if len(node.args) != 2:
                raise TypeError("LEAD() takes 2 arguments")
            expr_type = self.check(node.args[0])
            k_type = self.check(node.args[1])
            if k_type != MTLType.INT:
                raise TypeError(f"LEAD() k must be int, got {k_type}")
            return expr_type

        elif fn == 'COALESCE':
            if len(node.args) < 2:
                raise TypeError("COALESCE() takes at least 2 arguments")
            first_type = self.check(node.args[0])
            for i, arg in enumerate(node.args[1:]):
                arg_type = self.check(arg)
                if arg_type != first_type:
                    raise TypeError(f"COALESCE() argument {i+2} type mismatch: {first_type} vs {arg_type}")
            return first_type

        elif fn == 'CAST':
            if len(node.args) != 2:
                raise TypeError("CAST() takes 2 arguments: expr and type")
            expr_type = self.check(node.args[0])
            # Second arg should be string literal with type name
            if isinstance(node.args[1], ast.Constant):
                type_str = node.args[1].value
            elif hasattr(ast, 'Str') and isinstance(node.args[1], ast.Str):
                type_str = node.args[1].s
            else:
                raise TypeError("CAST() type must be string literal")
            target_type = MTLType.from_string(type_str)
            # Check if cast is valid (can widen, or same type)
            if expr_type != target_type and not expr_type.can_widen_to(target_type):
                raise TypeError(f"Invalid cast from {expr_type} to {target_type}")
            return target_type

        elif fn in ('starts_with', 'ends_with', 'contains'):
            if len(node.args) != 2:
                raise TypeError(f"{fn}() takes 2 arguments")
            str_type = self.check(node.args[0])
            lit_type = self.check(node.args[1])
            if str_type != MTLType.STRING:
                raise TypeError(f"{fn}() first arg must be string, got {str_type}")
            if lit_type != MTLType.STRING:
                raise TypeError(f"{fn}() second arg must be string literal, got {lit_type}")
            return MTLType.BOOL

        else:
            raise TypeError(f"Unknown function: {fn}")


# --- Expression Evaluator (Extended) ---

class ExpressionEvaluator:
    """Evaluate expressions against a DataFrame row."""

    def __init__(self, df: pd.DataFrame, params: Dict[str, Any]):
        self.df = df
        self.params = params

    def evaluate(self, node: ast.AST, row_idx: int) -> Any:
        """Evaluate expression at given row."""
        self.row_idx = row_idx
        return self._eval(node)

    def _eval(self, node) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.NameConstant):
            return node.value

        if isinstance(node, ast.Name):
            col = node.id
            val = self.df.at[self.row_idx, col]
            if isinstance(val, pd.Timestamp):
                return val.to_pydatetime()
            return val

        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            if isinstance(node.op, ast.UAdd):
                return +operand
            raise TypeError(f"Unknown unary op: {node.op}")

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise ZeroDivisionError("Division by zero")
                return left / right
            if isinstance(node.op, ast.Mod):
                return left % right
            raise TypeError(f"Unknown binop: {node.op}")

        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator)
                if isinstance(op, ast.Eq):
                    if not (left == right):
                        return False
                elif isinstance(op, ast.NotEq):
                    if not (left != right):
                        return False
                elif isinstance(op, ast.Lt):
                    if not (left < right):
                        return False
                elif isinstance(op, ast.LtE):
                    if not (left <= right):
                        return False
                elif isinstance(op, ast.Gt):
                    if not (left > right):
                        return False
                elif isinstance(op, ast.GtE):
                    if not (left >= right):
                        return False
                else:
                    raise TypeError(f"Unknown compare op: {op}")
                left = right
            return True

        if isinstance(node, ast.BoolOp):
            values = [self._eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise TypeError(f"Unknown boolop: {node.op}")

        if isinstance(node, ast.Call):
            fn = node.func.id
            args = [self._eval(arg) for arg in node.args]

            if fn == 'len':
                return len(args[0])
            if fn == 'day':
                return args[0].day
            if fn == 'month':
                return args[0].month
            if fn == 'year':
                return args[0].year
            if fn == 'round':
                val, digits = args[0], (args[1] if len(args) > 1 else 0)
                return round(val, digits)
            if fn == 'CONTAINS':
                return args[1] in args[0]
            if fn == 'PARAM':
                param_name = args[0]
                if param_name not in self.params:
                    raise KeyError(f"Unknown param: {param_name}")
                return self.params[param_name]
            if fn == 'COALESCE':
                for arg in args:
                    if arg is not None:
                        # For numeric, check if it's not NaN
                        if isinstance(arg, (int, float)) and np.isnan(arg):
                            continue
                        return arg
                return None
            if fn == 'CAST':
                val, type_str = args
                # No-op for same type
                return val
            if fn in ('starts_with', 'ends_with', 'contains'):
                str_val, pattern = args
                if fn == 'starts_with':
                    return str(str_val).startswith(str(pattern))
                elif fn == 'ends_with':
                    return str(str_val).endswith(str(pattern))
                elif fn == 'contains':
                    return str(pattern) in str(str_val)

            raise TypeError(f"Unknown function: {fn}")

        raise TypeError(f"Cannot evaluate: {type(node).__name__}")


# --- Post-Aggregation Evaluator ---

class PostAggEvaluator:
    """Evaluate post-aggregation expressions over result rows."""

    def __init__(self, results: List[Dict], params: Dict[str, Any], calcs: List[CalcInfo], all_results: List[Dict]):
        self.results = results
        self.params = params
        self.calcs = {c.name: c for c in calcs if c.stage == 'post_agg'}
        self.all_results = all_results  # Keep reference to all sorted results for LAG/LEAD
        self._computed = {}

    def evaluate_all(self):
        """Evaluate all post-agg calcs for all result rows."""
        # Initialize computed dict for each row
        for i in range(len(self.results)):
            self._computed[i] = {}

        # Evaluate each post-agg calc in order
        for calc in self.calcs.values():
            for i, row in enumerate(self.results):
                try:
                    # Create evaluator with row position
                    evaluator = PostAggRowEvaluator(i, self.all_results, self.params, self._computed)
                    val = evaluator.evaluate(calc.parsed_ast)
                    self._computed[i][calc.name] = val
                except Exception as e:
                    self._computed[i][calc.name] = None

        # Add computed values to results
        for i, row in enumerate(self.results):
            for calc_name in self.calcs:
                row[calc_name] = self._computed[i][calc_name]

        return self.results


class PostAggRowEvaluator(ExpressionEvaluator):
    """Evaluate expressions against a single result row for post-agg calcs."""

    def __init__(self, row_idx: int, all_results: List[Dict], params: Dict[str, Any], prev_computed: Dict[int, Dict]):
        self.row_idx = row_idx
        self.all_results = all_results
        self.params = params
        self.prev_computed = prev_computed

    def _get_current_row_data(self) -> Dict:
        """Get the current result row data."""
        if self.row_idx < len(self.all_results):
            return self.all_results[self.row_idx]
        return {}

    def _eval(self, node) -> Any:
        row_data = self._get_current_row_data()

        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.Str):
            return node.s
        if isinstance(node, ast.NameConstant):
            return node.value

        if isinstance(node, ast.Name):
            col = node.id
            if col in row_data:
                val = row_data[col]
                if isinstance(val, pd.Timestamp):
                    return val.to_pydatetime()
                return val
            raise TypeError(f"Unknown field: {col}")

        if isinstance(node, ast.UnaryOp):
            operand = self._eval(node.operand)
            if isinstance(node.op, ast.Not):
                return not operand
            if isinstance(node.op, ast.USub):
                return -operand
            raise TypeError(f"Unknown unary op: {node.op}")

        if isinstance(node, ast.BinOp):
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise ZeroDivisionError("Division by zero")
                return left / right
            if isinstance(node.op, ast.Mod):
                return left % right
            raise TypeError(f"Unknown binop: {node.op}")

        if isinstance(node, ast.Compare):
            left = self._eval(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = self._eval(comparator)
                if isinstance(op, ast.Eq):
                    if not (left == right):
                        return False
                elif isinstance(op, ast.NotEq):
                    if not (left != right):
                        return False
                elif isinstance(op, ast.Lt):
                    if not (left < right):
                        return False
                elif isinstance(op, ast.LtE):
                    if not (left <= right):
                        return False
                elif isinstance(op, ast.Gt):
                    if not (left > right):
                        return False
                elif isinstance(op, ast.GtE):
                    if not (left >= right):
                        return False
                    else:
                        raise TypeError(f"Unknown compare op: {op}")
                left = right
            return True

        if isinstance(node, ast.BoolOp):
            values = [self._eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
            raise TypeError(f"Unknown boolop: {node.op}")

        if isinstance(node, ast.Call):
            fn = node.func.id
            args = [self._eval(arg) for arg in node.args]

            if fn == 'len':
                return len(args[0])
            if fn == 'day':
                return args[0].day
            if fn == 'month':
                return args[0].month
            if fn == 'year':
                return args[0].year
            if fn == 'round':
                val, digits = args[0], (args[1] if len(args) > 1 else 0)
                return round(val, digits)
            if fn == 'CONTAINS':
                return args[1] in args[0]
            if fn == 'PARAM':
                param_name = args[0]
                if param_name not in self.params:
                    raise KeyError(f"Unknown param: {param_name}")
                return self.params[param_name]
            if fn == 'COALESCE':
                for arg in args:
                    if arg is not None:
                        if isinstance(arg, (int, float)) and np.isnan(arg):
                            continue
                        return arg
                return None
            if fn == 'CAST':
                return args[0]
            if fn in ('starts_with', 'ends_with', 'contains'):
                str_val, pattern = args
                if fn == 'starts_with':
                    return str(str_val).startswith(str(pattern))
                elif fn == 'ends_with':
                    return str(str_val).endswith(str(pattern))
                elif fn == 'contains':
                    return str(pattern) in str(str_val)
            if fn == 'LAG':
                # LAG(expr, k): get value of expr from k-th previous row
                # args[0] is the expression to evaluate
                k_arg = args[1] if len(args) > 1 else 1
                if not isinstance(k_arg, int):
                    # Try to evaluate k_arg as an expression
                    k = self._eval(node.args[1])
                    if not isinstance(k, int):
                        raise TypeError(f"LAG() k must be int, got {type(k)}")
                    k_arg = k

                prev_idx = self.row_idx - k_arg
                if prev_idx < 0 or prev_idx >= len(self.all_results):
                    return None

                # Evaluate the expression at the previous row
                prev_row = self.all_results[prev_idx]
                # Create a temporary evaluator for that row
                temp_eval = PostAggRowEvaluator(prev_idx, self.all_results, self.params, self.prev_computed)
                return temp_eval._eval(node.args[0])

            if fn == 'LEAD':
                # LEAD(expr, k): get value of expr from k-th next row
                k_arg = args[1] if len(args) > 1 else 1
                if not isinstance(k_arg, int):
                    k = self._eval(node.args[1])
                    if not isinstance(k, int):
                        raise TypeError(f"LEAD() k must be int, got {type(k)}")
                    k_arg = k

                next_idx = self.row_idx + k_arg
                if next_idx < 0 or next_idx >= len(self.all_results):
                    return None

                # Evaluate the expression at the next row
                next_row = self.all_results[next_idx]
                temp_eval = PostAggRowEvaluator(next_idx, self.all_results, self.params, self.prev_computed)
                return temp_eval._eval(node.args[0])

            raise TypeError(f"Unknown function: {fn}")

        raise TypeError(f"Cannot evaluate: {type(node).__name__}")


# --- Main MTL Engine (Extended) ---

class MTLEngine:
    """Main engine for executing MTL pipelines with schemas, params, and calcs."""

    def __init__(self, csv_path: str, tz: str = 'UTC', timestamp_format: str = None, param_values: Dict[str, str] = None):
        self.tz = tz
        self.timestamp_format = timestamp_format
        self.param_values = param_values or {}
        self.df = self._load_csv(csv_path)
        self._apply_columns: Dict[str, ColumnInfo] = {}
        self._calculated_columns: Dict[str, ColumnInfo] = {}  # Schema-level calcs + pipeline pre calcs

    def _load_csv(self, path: str) -> pd.DataFrame:
        """Load CSV, parse timestamps."""
        try:
            df = pd.read_csv(path)
        except Exception as e:
            raise IOError(f"Cannot read CSV: {e}")

        if 'timestamp' not in df.columns:
            raise ValueError("CSV must have 'timestamp' column")

        tz_info = self.tz
        try:
            if self.timestamp_format:
                df['timestamp'] = pd.to_datetime(df['timestamp'], format=self.timestamp_format)
            else:
                df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
                if df['timestamp'].dt.tz is None:
                    df['timestamp'] = df['timestamp'].dt.tz_localize(tz_info)
                else:
                    df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
        except Exception as e:
            raise ValueError(f"Invalid timestamp format: {e}")

        return df

    def resolve_schema(self, schema_name: str, all_schemas: Dict[str, Schema]) -> Dict[str, SchemaField]:
        """Resolve schema inheritance to get final field definitions."""
        if schema_name not in all_schemas:
            raise SyntaxError(f"Unknown schema: {schema_name}")

        schema = all_schemas[schema_name]

        # Check for cycles
        visited = set()
        current = schema_name
        while current:
            if current in visited:
                raise SyntaxError(f"Circular schema inheritance detected involving {current}")
            visited.add(current)
            if current not in all_schemas:
                break
            current_schema = all_schemas[current]
            if current_schema.base:
                current = current_schema.base
            else:
                break

        # Build field map by walking inheritance chain
        fields = {}
        seen_names = set()

        # Walk from base to derived (inheritance order)
        inheritance_chain = []
        current = schema_name
        while current:
            inheritance_chain.append(current)
            if current not in all_schemas:
                break
            base = all_schemas[current].base
            if base:
                if base not in all_schemas:
                    raise SyntaxError(f"Base schema not found: {base}")
                current = base
            else:
                break
        inheritance_chain.reverse()  # Base first

        for schema_name_in_chain in inheritance_chain:
            sch = all_schemas[schema_name_in_chain]
            for field_name, field_def in sch.fields.items():
                if field_name in seen_names:
                    # Override: check if type change is legal
                    existing = fields[field_name]
                    if field_def.mtype != existing.mtype:
                        # Check if it's numeric widening
                        if not existing.mtype.can_widen_to(field_def.mtype):
                            raise TypeError(
                                f"Illegal override of {field_name}: "
                                f"{existing.mtype} -> {field_def.mtype} (narrowing not allowed)"
                            )
                    # Type is same or widened - update field info
                    fields[field_name] = field_def
                else:
                    fields[field_name] = field_def
                    seen_names.add(field_name)

        return fields

    def validate_physical_columns(self, fields: Dict[str, SchemaField], df: pd.DataFrame):
        """Validate that all physical columns exist in CSV."""
        csv_cols = set(df.columns)
        for name, field in fields.items():
            if field.expr is None and name not in csv_cols:
                raise SyntaxError(f"Physical column '{name}' not found in CSV")

    def resolve_param_values(self, params: List[ParamInfo]) -> Dict[str, Tuple[MTLType, Any]]:
        """Resolve parameter values from CLI or DSL defaults."""
        resolved = {}

        for param in params:
            if param.name in self.param_values:
                # CLI value takes precedence
                value_str = self.param_values[param.name]
                ptype = param.mtype
                if ptype is None:
                    # Infer string type
                    value = value_str
                    ptype = MTLType.STRING
                else:
                    # Parse according to declared type
                    if ptype == MTLType.STRING:
                        value = value_str
                    elif ptype == MTLType.INT:
                        try:
                            value = int(value_str)
                        except ValueError:
                            raise TypeError(f"Param '{param.name}': cannot parse '{value_str}' as int")
                    elif ptype == MTLType.FLOAT:
                        try:
                            value = float(value_str)
                        except ValueError:
                            raise TypeError(f"Param '{param.name}': cannot parse '{value_str}' as float")
                    elif ptype == MTLType.BOOL:
                        if value_str.lower() in ('true', '1', 'yes', 'on'):
                            value = True
                        elif value_str.lower() in ('false', '0', 'no', 'off'):
                            value = False
                        else:
                            raise TypeError(f"Param '{param.name}': cannot parse '{value_str}' as bool")
                    elif ptype == MTLType.TIMESTAMP:
                        try:
                            # Try parsing as ISO or custom format
                            if self.timestamp_format:
                                value = datetime.strptime(value_str, self.timestamp_format)
                            else:
                                value = pd.to_datetime(value_str).to_pydatetime()
                        except Exception:
                            raise TypeError(f"Param '{param.name}': cannot parse '{value_str}' as timestamp")
                    else:
                        value = value_str
                resolved[param.name] = (ptype, value)
            elif param.default is not None:
                # Use DSL default - need to parse as literal
                default_str = param.default.strip()
                ptype = param.mtype
                if ptype is None:
                    ptype = MTLType.STRING
                    value = default_str.strip("'")  # Remove quotes
                else:
                    if ptype == MTLType.STRING:
                        value = default_str.strip("'")
                    elif ptype == MTLType.INT:
                        try:
                            value = int(default_str)
                        except ValueError:
                            raise TypeError(f"Param '{param.name}': default '{default_str}' not valid int")
                    elif ptype == MTLType.FLOAT:
                        try:
                            value = float(default_str)
                        except ValueError:
                            raise TypeError(f"Param '{param.name}': default '{default_str}' not valid float")
                    elif ptype == MTLType.BOOL:
                        if default_str.lower() == 'true':
                            value = True
                        elif default_str.lower() == 'false':
                            value = False
                        else:
                            raise TypeError(f"Param '{param.name}': default '{default_str}' not valid bool")
                    elif ptype == MTLType.TIMESTAMP:
                        try:
                            if self.timestamp_format:
                                value = datetime.strptime(default_str, self.timestamp_format)
                            else:
                                value = pd.to_datetime(default_str.strip("'")).to_pydatetime()
                        except Exception:
                            raise TypeError(f"Param '{param.name}': default '{default_str}' not valid timestamp")
                    else:
                        value = default_str
                resolved[param.name] = (ptype, value)
            else:
                raise SyntaxError(f"Param '{param.name}' must be provided via --param (no default)")

        return resolved

    def get_column_info(self) -> Dict[str, ColumnInfo]:
        """Get column info including applied and calculated columns."""
        result = {}
        for col in self.df.columns:
            dtype = str(self.df[col].dtype)
            if dtype.startswith('datetime'):
                mtype = MTLType.TIMESTAMP
            elif dtype in ('int64', 'int32', 'int'):
                mtype = MTLType.INT
            elif dtype in ('float64', 'float32'):
                mtype = MTLType.FLOAT
            else:
                mtype = MTLType.STRING
            result[col] = ColumnInfo(name=col, mtype=mtype, pandas_dtype=dtype, is_physical=True)
        result.update(self._apply_columns)
        result.update(self._calculated_columns)
        return result

    def execute(self, pipeline: Pipeline, all_schemas: Dict[str, Schema]) -> Any:
        """Execute the pipeline and return result."""
        # Resolve schema inheritance
        schema_fields = self.resolve_schema(pipeline.using_schema, all_schemas)

        # Validate physical columns exist
        self.validate_physical_columns(schema_fields, self.df)

        # Resolve parameter values
        resolved_params = self.resolve_param_values(pipeline.params)

        # Build column info from schema
        # Add CSV columns and schema-level fields
        # Schema-level calculated fields are physical=False

        # Phase 0: Add schema-level calculated columns to dataframe
        for field_name, field_def in schema_fields.items():
            if field_def.expr is not None:
                # Calculate this field
                col_info = self._eval_schema_field(field_name, field_def, resolved_params)

        # Phase 1: Pipeline pre-stage calculated fields
        for calc in pipeline.calcs:
            if calc.stage == "pre":
                self._add_pipeline_calc_column(calc, resolved_params)

        # Phase 2: Filters
        if pipeline.filters:
            mask = pd.Series([True] * len(self.df), index=self.df.index)
            for expr_str, parsed_ast in pipeline.filters:
                col_info = self.get_column_info()
                checker = TypeChecker(col_info, resolved_params, "pre")
                checker.check(parsed_ast)
                evaluator = ExpressionEvaluator(self.df, {n: v for n, (t, v) in resolved_params.items()})
                new_mask = pd.Series([True] * len(self.df), index=self.df.index)
                for i in range(len(self.df)):
                    try:
                        result = evaluator.evaluate(parsed_ast, i)
                        new_mask.iloc[i] = bool(result)
                    except (KeyError, TypeError, ZeroDivisionError) as e:
                        new_mask.iloc[i] = False
                mask = mask & new_mask
            self.df = self.df[mask].reset_index(drop=True)

        # Phase 3: Apply transforms (in order)
        for expr_str, new_col, parsed_ast in pipeline.applies:
            self._apply_transform(expr_str, new_col, parsed_ast, resolved_params)

        # Phase 4: Windowing
        if pipeline.window:
            self._apply_window(pipeline.window)

        # Phase 5: Group by
        if pipeline.group_by:
            self._apply_group_by(pipeline.group_by)

        # Phase 6: Aggregation
        result = self._apply_aggregates(pipeline.aggregates, resolved_params)

        # Phase 7: Post-aggregation calculated fields
        if any(c.stage == "post_agg" for c in pipeline.calcs):
            post_agg_calcs = [c for c in pipeline.calcs if c.stage == "post_agg"]
            # Validate post-agg calcs can reference available fields
            available_fields = set()
            if hasattr(self, '_window_applied') and self._window_applied:
                available_fields.add('window_start')
                available_fields.add('window_end')
            if hasattr(self, '_group_columns'):
                available_fields.update(self._group_columns)
            available_fields.update([a.alias for a in pipeline.aggregates])

            for calc in post_agg_calcs:
                # Type checking happens during evaluation
                pass

            # Apply post-agg calcs
            evaluator = PostAggEvaluator(result, {n: v for n, (t, v) in resolved_params.items()}, post_agg_calcs)
            result = evaluator.evaluate_all()

        # Phase 8: Format output
        return self._format_output(result, pipeline)

    def _eval_schema_field(self, field_name: str, field_def: SchemaField, resolved_params: Dict[str, Tuple[MTLType, Any]]):
        """Evaluate a schema-level calculated field."""
        # Parse and type check the expression
        parser = DSLParser("")  # Dummy parser for preprocessing
        processed = parser._preprocess_general_expr(field_def.expr, "pre")
        parsed_ast = parser._safe_parse(processed, mode='eval')

        col_info = self.get_column_info()
        checker = TypeChecker(col_info, resolved_params, "pre")
        result_type = checker.check(parsed_ast)

        if result_type != field_def.mtype:
            raise TypeError(f"Schema calc {field_name} type mismatch: {result_type} vs {field_def.mtype}")

        # Evaluate and add to dataframe
        values = []
        evaluator = ExpressionEvaluator(self.df, {n: v for n, (t, v) in resolved_params.items()})
        for i in range(len(self.df)):
            try:
                val = evaluator.evaluate(parsed_ast, i)
                values.append(val)
            except Exception:
                values.append(None)

        dtype_map = {
            MTLType.INT: 'int64',
            MTLType.FLOAT: 'float64',
            MTLType.TIMESTAMP: 'datetime64[ns]',
            MTLType.BOOL: 'bool',
            MTLType.STRING: 'object',
        }
        dtype = dtype_map.get(result_type, 'object')
        self.df[field_name] = pd.Series(values, dtype=dtype)
        self._calculated_columns[field_name] = ColumnInfo(
            name=field_name,
            mtype=result_type,
            pandas_dtype=str(self.df[field_name].dtype),
            is_physical=False
        )

    def _add_pipeline_calc_column(self, calc: CalcInfo, resolved_params: Dict[str, Tuple[MTLType, Any]]):
        """Add a pipeline pre-stage calculated column."""
        # Already parsed, just evaluate
        col_info = self.get_column_info()
        checker = TypeChecker(col_info, resolved_params, "pre")
        result_type = checker.check(calc.parsed_ast)

        if result_type != calc.mtype:
            raise TypeError(f"Calc {calc.name} type mismatch: {result_type} vs {calc.mtype}")

        if calc.name in self._apply_columns or calc.name in self._calculated_columns:
            raise SyntaxError(f"Duplicate calc name: {calc.name}")

        values = []
        evaluator = ExpressionEvaluator(self.df, {n: v for n, (t, v) in resolved_params.items()})
        for i in range(len(self.df)):
            try:
                val = evaluator.evaluate(calc.parsed_ast, i)
                values.append(val)
            except Exception:
                values.append(None)

        dtype_map = {
            MTLType.INT: 'int64',
            MTLType.FLOAT: 'float64',
            MTLType.TIMESTAMP: 'datetime64[ns]',
            MTLType.BOOL: 'bool',
            MTLType.STRING: 'object',
        }
        dtype = dtype_map.get(result_type, 'object')
        self.df[calc.name] = pd.Series(values, dtype=dtype)
        self._calculated_columns[calc.name] = ColumnInfo(
            name=calc.name,
            mtype=result_type,
            pandas_dtype=str(self.df[calc.name].dtype),
            is_physical=False
        )

    def _apply_transform(self, expr_str: str, new_col: str, parsed_ast: ast.AST, resolved_params: Dict[str, Tuple[MTLType, Any]]):
        """Apply a transform expression to create a new column."""
        col_info = self.get_column_info()
        checker = TypeChecker(col_info, resolved_params, "pre")
        result_type = checker.check(parsed_ast)

        values = []
        evaluator = ExpressionEvaluator(self.df, {n: v for n, (t, v) in resolved_params.items()})
        for i in range(len(self.df)):
            try:
                val = evaluator.evaluate(parsed_ast, i)
                values.append(val)
            except Exception:
                values.append(None)

        dtype_map = {
            MTLType.INT: 'int64',
            MTLType.FLOAT: 'float64',
            MTLType.TIMESTAMP: 'datetime64[ns]',
            MTLType.BOOL: 'bool',
            MTLType.STRING: 'object',
        }
        dtype = dtype_map.get(result_type, 'object')
        self.df[new_col] = pd.Series(values, dtype=dtype)
        self._apply_columns[new_col] = ColumnInfo(
            name=new_col,
            mtype=result_type,
            pandas_dtype=str(self.df[new_col].dtype)
        )

    def _apply_window(self, window: WindowSpec):
        """Apply windowing - add window_start and window_end columns."""
        epoch = pd.Timestamp('1970-01-01T00:00:00Z', tz='UTC')
        ts_ns = (self.df['timestamp'] - epoch).dt.total_seconds() * 1_000_000_000
        window_ns = window.duration.total_seconds() * 1_000_000_000
        window_idx = (ts_ns // window_ns).astype(int)
        self.df['window_start'] = epoch + pd.to_timedelta(window_idx * window_ns, unit='ns')
        self.df['window_end'] = self.df['window_start'] + pd.to_timedelta(window_ns, unit='ns')
        self._window_applied = True

    def _apply_group_by(self, group: GroupSpec):
        """Apply group by - set index."""
        self._group_applied = True
        self._group_columns = group.columns

    def _apply_aggregates(self, aggregates: List[Aggregation], resolved_params: Dict[str, Tuple[MTLType, Any]]) -> Union[Dict, List]:
        """Apply aggregation and return result."""
        has_window = hasattr(self, '_window_applied') and self._window_applied
        has_group = hasattr(self, '_group_applied') and self._group_applied

        if has_window or has_group:
            return self._aggregate_grouped_or_windowed(aggregates, resolved_params)
        else:
            return self._aggregate_flat(aggregates)

    def _aggregate_flat(self, aggregates: List[Aggregation]) -> Dict:
        """Aggregate without grouping or windowing."""
        result = {}
        for agg in aggregates:
            if agg.column == '*':
                result[agg.alias] = len(self.df)
            else:
                if agg.column not in self.df.columns:
                    raise ValueError(f"Column not found: {agg.column}")
                col_data = self.df[agg.column]
                if not pd.api.types.is_numeric_dtype(col_data):
                    raise TypeError(f"Aggregate requires numeric column: {agg.column}")
                col_data = col_data.dropna()
                if len(col_data) == 0:
                    result[agg.alias] = None
                else:
                    result[agg.alias] = self._compute_agg(agg.fn, col_data)
        return result

    def _compute_agg(self, fn: str, data: pd.Series) -> float:
        """Compute single aggregation."""
        if fn == 'sum':
            return float(data.sum())
        elif fn == 'average':
            return float(data.mean())
        elif fn == 'median':
            return float(data.median())
        elif fn == 'count':
            return float(len(data))
        elif fn == 'std':
            return float(data.std(ddof=0))
        elif fn == 'var':
            return float(data.var(ddof=0))
        elif fn == 'min':
            return float(data.min())
        elif fn == 'max':
            return float(data.max())
        else:
            raise ValueError(f"Unknown aggregate: {fn}")

    def _aggregate_grouped_or_windowed(self, aggregates: List[Aggregation], resolved_params: Dict[str, Tuple[MTLType, Any]]) -> List[Dict]:
        """Unified aggregation for grouped, windowed, or both cases."""
        group_cols = getattr(self, '_group_columns', [])
        has_window = hasattr(self, '_window_applied') and self._window_applied

        if has_window:
            group_keys = ['window_start', 'window_end'] + group_cols
        else:
            group_keys = group_cols

        results = []
        grouped = self.df.groupby(group_keys, sort=False)

        for keys, group in grouped:
            result = {}

            if has_window:
                result['window_start'] = self._format_timestamp(keys[0])
                result['window_end'] = self._format_timestamp(keys[1])
                key_offset = 2
            else:
                key_offset = 0

            if group_cols:
                group_keys_part = keys[key_offset:]
                if len(group_cols) == 1:
                    result[group_cols[0]] = group_keys_part
                else:
                    for i, col in enumerate(group_cols):
                        result[col] = group_keys_part[i]

            for agg in aggregates:
                if agg.column == '*':
                    result[agg.alias] = len(group)
                else:
                    if agg.column not in group.columns:
                        result[agg.alias] = None
                        continue
                    col_data = group[agg.column].dropna()
                    if len(col_data) == 0:
                        result[agg.alias] = None
                    else:
                        result[agg.alias] = self._compute_agg(agg.fn, col_data)

            results.append(result)

        return results

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for output."""
        return ts.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(ts, 'strftime') else str(ts)

    def _format_output(self, result: Any, pipeline: Pipeline) -> str:
        """Format output according to determinism rules."""
        import json

        has_window = hasattr(self, '_window_applied') and self._window_applied
        has_group = hasattr(self, '_group_applied') and self._group_applied
        post_agg_calcs = [c.name for c in pipeline.calcs if c.stage == 'post_agg']

        if isinstance(result, dict):
            return json.dumps(result, sort_keys=True, separators=(',', ':'))

        elif isinstance(result, list):
            # Sort by window_start then group keys
            def sort_key(item):
                keys = []
                if has_window:
                    keys.append(item.get('window_start', ''))
                if has_group:
                    for col in getattr(self, '_group_columns', []):
                        keys.append(str(item.get(col, '')))
                return tuple(keys)

            result.sort(key=sort_key)

            def sort_object_keys(obj):
                if isinstance(obj, dict):
                    fixed_keys = []
                    if has_window:
                        fixed_keys = ['window_start', 'window_end']
                    if has_group:
                        fixed_keys.extend(getattr(self, '_group_columns', []))
                    # Agg aliases sorted lexicographically
                    agg_aliases = sorted([a.alias for a in pipeline.aggregates])
                    # Post-agg calc names sorted lexicographically
                    calc_aliases = sorted(post_agg_calcs)
                    new_obj = {}
                    for k in fixed_keys:
                        if k in obj:
                            new_obj[k] = sort_object_keys(obj[k])
                    for k in agg_aliases:
                        if k in obj:
                            new_obj[k] = sort_object_keys(obj[k])
                    for k in calc_aliases:
                        if k in obj:
                            new_obj[k] = sort_object_keys(obj[k])
                    return new_obj
                elif isinstance(obj, list):
                    return [sort_object_keys(i) for i in obj]
                else:
                    return obj

            result = [sort_object_keys(item) for item in result]
            return json.dumps(result, sort_keys=True, separators=(',', ':'))

        raise TypeError(f"Unexpected result type: {type(result)}")


# --- CLI ---

def parse_args():
    import argparse
    parser = argparse.ArgumentParser(description='MTL: Market Transformation Language CLI')
    parser.add_argument('--csv', required=True, help='Path to input CSV file')
    parser.add_argument('--dsl', required=True, help='Path to DSL file, or - for STDIN')
    parser.add_argument('--out', default=None, help='Output file path (default: STDOUT)')
    parser.add_argument('--tz', default='UTC', help='Default timezone (default: UTC)')
    parser.add_argument('--timestamp-format', default=None, help='Timestamp format string')
    parser.add_argument('--strict', action='store_true', help='Enable strict mode')
    parser.add_argument('--param', action='append', dest='params', default=[],
                        help='Parameter as key=value (can be used multiple times)')
    return parser.parse_args()


def parse_param_list(param_strings: List[str]) -> Dict[str, str]:
    """Parse --param key=value into dict."""
    result = {}
    for p in param_strings:
        if '=' not in p:
            raise ValueError(f"Invalid param format: {p} (expected key=value)")
        key, value = p.split('=', 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Empty param key in: {p}")
        result[key] = value
    return result


def read_dsl(path: str) -> str:
    """Read DSL from file or STDIN."""
    if path == '-':
        return sys.stdin.read()
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception as e:
        raise IOError(f"Cannot read DSL: {e}")


def main():
    args = parse_args()

    # Parse params
    try:
        param_values = parse_param_list(args.params)
    except ValueError as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)

    # Read DSL
    try:
        dsl_text = read_dsl(args.dsl)
    except IOError as e:
        print(f"ERROR:bad_io:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_IO)

    # Parse DSL
    try:
        parser = DSLParser(dsl_text)
        # Parse schema blocks first
        parser.parse()  # This populates parser.schemas and parser.pipeline
    except SyntaxError as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)
    except Exception as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)

    # Load CSV and execute
    try:
        engine = MTLEngine(args.csv, tz=args.tz, timestamp_format=args.timestamp_format, param_values=param_values)

        # We need to parse the pipeline block separately
        # Reset parser and parse pipeline
        parser.line_num = 0
        # Skip schemas we already parsed
        while parser.line_num < len(parser.lines):
            line = parser.lines[parser.line_num].strip()
            if line.startswith('schema '):
                # Skip past this schema block
                parser.line_num += 1
                while parser.line_num < len(parser.lines) and parser.lines[parser.line_num].strip() != '}':
                    parser.line_num += 1
                parser.line_num += 1  # Skip closing }
            elif line.startswith('pipeline '):
                break
            else:
                parser.line_num += 1

        parser._parse_pipeline_block()

        output = engine.execute(parser.pipeline, parser.schemas)
    except SyntaxError as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)
    except ValueError as e:
        print(f"ERROR:type_error:{e}", file=sys.stderr)
        sys.exit(EXIT_TYPE_ERROR)
    except TypeError as e:
        print(f"ERROR:type_error:{e}", file=sys.stderr)
        sys.exit(EXIT_TYPE_ERROR)
    except IOError as e:
        print(f"ERROR:bad_io:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_IO)
    except Exception as e:
        print(f"ERROR:runtime_error:{e}", file=sys.stderr)
        sys.exit(EXIT_RUNTIME_ERROR)

    # Write output
    try:
        if args.out:
            with open(args.out, 'w') as f:
                f.write(output)
        else:
            print(output)
    except Exception as e:
        print(f"ERROR:bad_io:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_IO)


if __name__ == '__main__':
    main()
