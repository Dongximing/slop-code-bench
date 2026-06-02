#!/usr/bin/env python3
"""MTL (Market Transformation Language) - file-based CLI for ad-hoc rollups."""

from __future__ import annotations

import ast
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Union

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
    NUMBER = "number"
    INT = "int"
    BOOL = "bool"
    TIMESTAMP = "timestamp"

@dataclass
class ColumnInfo:
    name: str
    mtype: MTLType
    pandas_dtype: str

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
class Pipeline:
    filters: List[Tuple[str, Any]] = field(default_factory=list)  # (bool_expr_str, parsed_ast)
    applies: List[Tuple[str, str, Any]] = field(default_factory=list)  # (transform_expr, new_col, parsed_ast)
    group_by: Optional[GroupSpec] = None
    window: Optional[WindowSpec] = None
    aggregates: List[Aggregation] = None

    def __post_init__(self):
        if self.aggregates is None:
            self.aggregates = []


# --- Parser ---

class DSLParser:
    """Parser for MTL DSL."""

    # Token patterns
    IDENTIFIER = r'[a-zA-Z_][a-zA-Z0-9_]*'
    STRING_LITERAL = r"'[^']*'"
    NUMBER_LITERAL = r'\d+\.?\d*'
    BOOL_LITERAL = r'true|false'
    DURATION_UNIT = r'm|h|d'
    DURATION = rf'\d+{DURATION_UNIT}'

    def __init__(self, text: str):
        self.text = text
        self.lines = text.split('\n')
        self.line_num = 0

    def parse(self) -> Pipeline:
        """Parse entire DSL into a Pipeline."""
        pipeline = Pipeline()
        seen_agg = False

        for line_num, line in enumerate(self.lines, 1):
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            try:
                if line.startswith('filter(') and line.endswith(')'):
                    expr = line[7:-1].strip()
                    parsed = self._parse_bool_expr(expr)
                    pipeline.filters.append((expr, parsed))

                elif line.startswith('apply(') and line.endswith(')'):
                    # apply(transform, new_col)
                    rest = line[6:-1].strip()
                    comma_idx = rest.rfind(',')
                    if comma_idx == -1:
                        raise SyntaxError(f"apply() requires transform, new_col")
                    transform = rest[:comma_idx].strip()
                    new_col = rest[comma_idx+1:].strip()
                    # Validate new_col is identifier
                    if not re.fullmatch(self.IDENTIFIER, new_col):
                        raise SyntaxError(f"Invalid column name: {new_col}")
                    parsed = self._parse_transform_expr(transform)
                    pipeline.applies.append((transform, new_col, parsed))

                elif line.startswith('group_by(') and line.endswith(')'):
                    if pipeline.group_by is not None:
                        raise SyntaxError("Multiple group_by not allowed")
                    rest = line[9:-1].strip()
                    # Handle both single column and list
                    if rest.startswith('[') and rest.endswith(']'):
                        cols_str = rest[1:-1]
                        columns = [c.strip() for c in cols_str.split(',')]
                        columns = [c for c in columns if c]  # filter empty
                    else:
                        columns = [rest]
                    # Validate column names
                    for col in columns:
                        if not re.fullmatch(self.IDENTIFIER, col):
                            raise SyntaxError(f"Invalid column name: {col}")
                    pipeline.group_by = GroupSpec(columns)

                elif line.startswith('window(') and line.endswith(')'):
                    if pipeline.window is not None:
                        raise SyntaxError("Multiple window not allowed")
                    rest = line[7:-1].strip()
                    if not re.fullmatch(self.DURATION, rest):
                        raise SyntaxError(f"Invalid duration literal: {rest}")
                    duration = self._parse_duration(rest)
                    pipeline.window = WindowSpec(duration)

                elif line.lower().startswith('aggregate'):
                    if seen_agg:
                        raise SyntaxError("Multiple aggregate not allowed")
                    seen_agg = True
                    # Parse aggregate list
                    rest = line[9:].strip()
                    if not rest:
                        raise SyntaxError("aggregate requires a list")
                    # Remove leading/trailing whitespace and any leading/trailing delimiters
                    rest = rest.strip()
                    if rest.startswith(' ') or rest.startswith('\t'):
                        # Handle multi-line aggregates inline
                        pass
                    # Split by commas, but be careful about 'as' clauses
                    aggregations = self._parse_aggregates(rest)
                    pipeline.aggregates = aggregations

                else:
                    raise SyntaxError(f"Unknown statement: {line}")

            except SyntaxError as e:
                raise SyntaxError(f"Line {line_num}: {e}")

        if not seen_agg or not pipeline.aggregates:
            raise SyntaxError("Exactly one aggregate statement is required")

        return pipeline

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
        """Parse aggregate list: sum(price) as total, average(qty) as avg, ..."""
        # Split by comma that's not part of ' as '
        parts = []
        current = ''
        i = 0
        paren_depth = 0
        in_as = False
        while i < len(text):
            # Track if we're inside parentheses
            if text[i] == '(':
                paren_depth += 1
            elif text[i] == ')':
                paren_depth -= 1
            # Check for comma outside parentheses
            if text[i] == ',' and paren_depth == 0:
                parts.append(current.strip())
                current = ''
                i += 1
                # skip spaces
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
            # Parse: <fn>(<col|*>) as <alias>
            # Need to find the first '(' and match it with the closing ')'
            # Then everything after ' as ' is the alias
            open_paren = part.find('(')
            if open_paren == -1:
                raise SyntaxError(f"Invalid aggregate format (missing '('): {part}")
            close_paren = part.rfind(')')
            if close_paren == -1:
                raise SyntaxError(f"Invalid aggregate format (missing ')'): {part}")
            fn = part[:open_paren].strip()
            col = part[open_paren+1:close_paren].strip()
            # Find ' as ' after close_paren
            as_idx = part.find(' as ', close_paren)
            if as_idx == -1:
                raise SyntaxError(f"Invalid aggregate format (missing ' as'): {part}")
            alias = part[as_idx+4:].strip()
            if not re.fullmatch(self.IDENTIFIER, alias):
                raise SyntaxError(f"Invalid aggregate alias: {alias}")
            if not re.fullmatch(self.IDENTIFIER, fn):
                raise SyntaxError(f"Invalid aggregate function: {fn}")
            aggregates.append(Aggregation(fn=fn, column=col, alias=alias))

        # Check unique aliases
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
        # Preprocess: replace 'true'/'false' and 'in' operator
        processed = self._preprocess_expr(expr)
        return self._safe_parse(processed, mode='eval')

    def _preprocess_expr(self, expr: str) -> str:
        """Preprocess expression for Python AST parsing."""
        # Pattern: string_literal in identifier  ->  CONTAINS(identifier, string_literal)
        # This is the only valid form per spec

        # Pattern: 'string' in identifier (NOT identifier in 'string')
        # We need to handle: 'lit' in col -> CONTAINS(col, 'lit')
        # But only this direction is valid per spec

        result = []
        i = 0
        while i < len(expr):
            # Check for string literal followed by 'in' followed by identifier
            if expr[i] == "'":
                # Find end of string
                j = i + 1
                while j < len(expr) and expr[j] != "'":
                    j += 1
                if j < len(expr):
                    str_lit = expr[i:j+1]
                    after_str = expr[j+1:].strip()
                    if after_str.startswith('in') and (len(after_str) == 2 or not after_str[2].isalnum()):
                        # Found 'string_literal in identifier' pattern
                        # Parse the identifier after 'in'
                        rest = after_str[2:].strip()
                        # Find the identifier (letters, digits, underscore)
                        k = 0
                        while k < len(rest) and (rest[k].isalnum() or rest[k] == '_'):
                            k += 1
                        identifier = rest[:k]
                        if identifier:
                            # Replace: 'lit' in col -> CONTAINS(col, 'lit')
                            result.append(f'CONTAINS({identifier},{str_lit})')
                            i = j + 1 + 2 + len(rest) - k
                            continue
            # Check for boolean literals
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

    def _safe_parse(self, expr: str, mode: str) -> ast.AST:
        """Safely parse expression, with some custom token handling."""
        try:
            tree = ast.parse(expr, mode=mode)
        except SyntaxError as e:
            raise SyntaxError(f"Syntax error: {e}")
        return tree


# --- Type Checker ---

class TypeChecker(ast.NodeVisitor):
    """Type check expressions against column schema."""

    def __init__(self, columns: Dict[str, ColumnInfo], df: pd.DataFrame):
        self.columns = columns

    def check(self, node: ast.AST) -> MTLType:
        """Check type of expression, return inferred type."""
        # Unwrap Expression wrapper if present
        if isinstance(node, ast.Expression):
            node = node.body
        return self.visit(node)

    def visit(self, node):
        return super().visit(node)

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
            if operand_type not in (MTLType.INT, MTLType.NUMBER):
                raise TypeError(f"Unary - requires numeric, got {operand_type}")
            return operand_type
        raise TypeError(f"Unknown unary op: {node.op}")

    def visit_BinOp(self, node):
        left_type = self.check(node.left)
        right_type = self.check(node.right)

        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            # Numeric operations only
            if left_type not in (MTLType.INT, MTLType.NUMBER):
                raise TypeError(f"Numeric op left: {left_type}")
            if right_type not in (MTLType.INT, MTLType.NUMBER):
                raise TypeError(f"Numeric op right: {right_type}")
            # Result is float if either is float
            if left_type == MTLType.NUMBER or right_type == MTLType.NUMBER:
                return MTLType.NUMBER
            return MTLType.INT
        if isinstance(node.op, ast.Mod):
            if left_type not in (MTLType.INT, MTLType.NUMBER) or right_type not in (MTLType.INT, MTLType.NUMBER):
                raise TypeError(f"% requires numeric operands, got {left_type}, {right_type}")
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
            # Check operator type constraints
            if isinstance(op, (ast.Eq, ast.NotEq)):
                pass  # any type
            elif isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                if left_type not in (MTLType.INT, MTLType.NUMBER, MTLType.STRING, MTLType.TIMESTAMP):
                    raise TypeError(f"Comparison op not supported for {left_type}")
            else:
                raise TypeError(f"Unknown compare op: {op}")
            left_type = right_type  # for chained comparisons

        return result

    def visit_Name(self, node):
        """Column reference or literal."""
        name = node.id
        if name in self.columns:
            return self.columns[name].mtype
        # Could be a boolean literal (True/False)
        if name == 'True' or name == 'False':
            return MTLType.BOOL
        raise TypeError(f"Unknown column: {name}")

    def visit_Constant(self, node):
        """Literal values."""
        val = node.value
        if val is None:
            raise TypeError("None literals not allowed")
        if isinstance(val, bool):
            return MTLType.BOOL
        if isinstance(val, int):
            # Check if it fits in int
            return MTLType.INT
        if isinstance(val, float):
            return MTLType.NUMBER
        if isinstance(val, str):
            return MTLType.STRING
        raise TypeError(f"Unknown literal type: {type(val)}")

    # For Python <3.8 compatibility
    def visit_Num(self, node):
        if isinstance(node.n, int):
            return MTLType.INT
        return MTLType.NUMBER

    def visit_Str(self, node):
        return MTLType.STRING

    def visit_NameConstant(self, node):
        return MTLType.BOOL

    def generic_visit(self, node):
        if isinstance(node, ast.Call):
            return self._visit_Call(node)
        raise TypeError(f"Unsupported expression: {type(node).__name__}")

    def _visit_Call(self, node: ast.Call) -> MTLType:
        """Handle function calls in expressions."""
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
            if arg_type not in (MTLType.INT, MTLType.NUMBER):
                raise TypeError(f"round() requires numeric, got {arg_type}")
            if len(node.args) == 2:
                digits_type = self.check(node.args[1])
                if digits_type != MTLType.INT:
                    raise TypeError(f"round() digits must be int, got {digits_type}")
            return MTLType.NUMBER

        elif fn == 'CONTAINS':
            # 'lit' in col -> CONTAINS(col, 'lit')
            # First arg: string column, Second arg: string literal
            if len(node.args) != 2:
                raise TypeError("CONTAINS() takes 2 arguments: column, literal")
            col_type = self.check(node.args[0])
            lit_type = self.check(node.args[1])
            if col_type != MTLType.STRING:
                raise TypeError(f"CONTAINS() first arg must be string column, got {col_type}")
            if lit_type != MTLType.STRING:
                raise TypeError(f"CONTAINS() second arg must be string literal, got {lit_type}")
            return MTLType.BOOL

        else:
            raise TypeError(f"Unknown function: {fn}")


# --- Expression Evaluator ---

class ExpressionEvaluator:
    """Evaluate expressions against a DataFrame row."""

    def __init__(self, df: pd.DataFrame):
        self.df = df

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
            # Convert pd.Timestamp to datetime if needed
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
                # CONTAINS(column, literal) -> literal in column
                return args[1] in args[0]

            raise TypeError(f"Unknown function: {fn}")

        raise TypeError(f"Cannot evaluate: {type(node).__name__}")


# --- Main MTL Engine ---

class MTLEngine:
    """Main engine for executing MTL pipelines."""

    def __init__(self, csv_path: str, tz: str = 'UTC', timestamp_format: str = None):
        self.tz = tz
        self.timestamp_format = timestamp_format
        self.df = self._load_csv(csv_path)
        self._apply_columns: Dict[str, ColumnInfo] = {}

    def _load_csv(self, path: str) -> pd.DataFrame:
        """Load CSV, parse timestamps."""
        try:
            df = pd.read_csv(path)
        except Exception as e:
            raise IOError(f"Cannot read CSV: {e}")

        if 'timestamp' not in df.columns:
            raise ValueError("CSV must have 'timestamp' column")

        # Parse timestamp column
        tz_info = self.tz
        try:
            if self.timestamp_format:
                df['timestamp'] = pd.to_datetime(df['timestamp'], format=self.timestamp_format)
            else:
                # ISO 8601 parsing
                df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
                # If already has timezone, keep it; otherwise localize
                if df['timestamp'].dt.tz is None:
                    df['timestamp'] = df['timestamp'].dt.tz_localize(tz_info)
                else:
                    df['timestamp'] = df['timestamp'].dt.tz_convert('UTC')
        except Exception as e:
            raise ValueError(f"Invalid timestamp format: {e}")

        return df

    def _get_column_info(self) -> Dict[str, ColumnInfo]:
        """Get column info including applied columns."""
        result = {}
        for col in self.df.columns:
            dtype = str(self.df[col].dtype)
            if dtype.startswith('datetime'):
                mtype = MTLType.TIMESTAMP
            elif dtype in ('int64', 'int32', 'int'):
                mtype = MTLType.INT
            elif dtype in ('float64', 'float32'):
                mtype = MTLType.NUMBER
            else:
                mtype = MTLType.STRING
            result[col] = ColumnInfo(name=col, mtype=mtype, pandas_dtype=dtype)
        # Add applied columns
        result.update(self._apply_columns)
        return result

    def execute(self, pipeline: Pipeline) -> Any:
        """Execute the pipeline and return result."""
        # Phase 1: Apply transforms (in order)
        for expr_str, new_col, parsed_ast in pipeline.applies:
            self._apply_transform(expr_str, new_col, parsed_ast)

        # Phase 2: Filters
        if pipeline.filters:
            mask = pd.Series([True] * len(self.df), index=self.df.index)
            for expr_str, parsed_ast in pipeline.filters:
                col_info = self._get_all_columns()
                checker = TypeChecker(col_info, self.df)
                checker.check(parsed_ast)
                evaluator = ExpressionEvaluator(self.df)
                new_mask = pd.Series([True] * len(self.df), index=self.df.index)
                for i in range(len(self.df)):
                    try:
                        result = evaluator.evaluate(parsed_ast, i)
                        new_mask.iloc[i] = bool(result)
                    except (KeyError, TypeError, ZeroDivisionError) as e:
                        new_mask.iloc[i] = False
                mask = mask & new_mask
            self.df = self.df[mask].reset_index(drop=True)

        # Phase 3: Windowing
        if pipeline.window:
            self._apply_window(pipeline.window)

        # Phase 4: Group by
        if pipeline.group_by:
            self._apply_group_by(pipeline.group_by)

        # Phase 5: Aggregation
        result = self._apply_aggregates(pipeline.aggregates)

        # Phase 6: Format output
        return self._format_output(result, pipeline)

    def _apply_transform(self, expr_str: str, new_col: str, parsed_ast: ast.AST):
        """Apply a transform expression to create a new column."""
        col_info = self._get_all_columns()
        checker = TypeChecker(col_info, self.df)
        result_type = checker.check(parsed_ast)

        values = []
        evaluator = ExpressionEvaluator(self.df)
        for i in range(len(self.df)):
            try:
                val = evaluator.evaluate(parsed_ast, i)
                values.append(val)
            except (KeyError, TypeError, ZeroDivisionError, ValueError) as e:
                values.append(None)

        # Determine pandas dtype
        if result_type == MTLType.INT:
            dtype = 'int64'
        elif result_type == MTLType.NUMBER:
            dtype = 'float64'
        elif result_type == MTLType.TIMESTAMP:
            dtype = 'datetime64[ns]'
        else:
            dtype = 'object'

        self.df[new_col] = pd.Series(values, dtype=dtype)
        # Update _apply_columns for subsequent transforms
        self._apply_columns[new_col] = ColumnInfo(
            name=new_col,
            mtype=result_type,
            pandas_dtype=str(self.df[new_col].dtype)
        )

    def _apply_window(self, window: WindowSpec):
        """Apply windowing - add window_start and window_end columns."""
        # Windows are aligned to Unix epoch (1970-01-01T00:00:00Z)
        epoch = pd.Timestamp('1970-01-01T00:00:00Z', tz='UTC')

        # Convert timestamps to nanoseconds since epoch
        ts_ns = (self.df['timestamp'] - epoch).dt.total_seconds() * 1_000_000_000
        window_ns = window.duration.total_seconds() * 1_000_000_000

        # Calculate window index
        window_idx = (ts_ns // window_ns).astype(int)

        # Calculate window boundaries
        self.df['window_start'] = epoch + pd.to_timedelta(window_idx * window_ns, unit='ns')
        self.df['window_end'] = self.df['window_start'] + pd.to_timedelta(window_ns, unit='ns')

        # Store for aggregation
        self._window_applied = True

    def _apply_group_by(self, group: GroupSpec):
        """Apply group by - set index."""
        self._group_applied = True
        self._group_columns = group.columns

    def _apply_aggregates(self, aggregates: List[Aggregation]) -> Union[Dict, List]:
        """Apply aggregation and return result."""
        has_window = hasattr(self, '_window_applied') and self._window_applied
        has_group = hasattr(self, '_group_applied') and self._group_applied

        if has_window or has_group:
            return self._aggregate_grouped_or_windowed(aggregates)
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
                # Check if numeric
                if not pd.api.types.is_numeric_dtype(col_data):
                    raise TypeError(f"Aggregate requires numeric column: {agg.column}")
                # Drop NaN for aggregates
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
            # Population std
            return float(data.std(ddof=0))
        elif fn == 'var':
            # Population variance
            return float(data.var(ddof=0))
        elif fn == 'min':
            return float(data.min())
        elif fn == 'max':
            return float(data.max())
        else:
            raise ValueError(f"Unknown aggregate: {fn}")

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
                # Check if numeric
                if not pd.api.types.is_numeric_dtype(col_data):
                    raise TypeError(f"Aggregate requires numeric column: {agg.column}")
                # Drop NaN for aggregates
                col_data = col_data.dropna()
                if len(col_data) == 0:
                    result[agg.alias] = None
                else:
                    result[agg.alias] = self._compute_agg(agg.fn, col_data)
        return result

    def _format_timestamp(self, ts) -> str:
        """Format timestamp for output."""
        return ts.strftime('%Y-%m-%dT%H:%M:%SZ') if hasattr(ts, 'strftime') else str(ts)

    def _apply_aggregates_to_group(self, group, aggregates: List[Aggregation]) -> Dict:
        """Apply aggregations to a single group and return result dict."""
        result = {}
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
        return result

    def _aggregate_grouped_or_windowed(self, aggregates: List[Aggregation]) -> List[Dict]:
        """Unified aggregation for grouped, windowed, or both cases.

        Returns either Dict or List[Dict]. Use _apply_aggregates which determines
        the appropriate mode based on instance flags.
        """
        group_cols = getattr(self, '_group_columns', [])
        has_window = hasattr(self, '_window_applied') and self._window_applied

        if has_window:
            # Group by window
            group_keys = ['window_start', 'window_end'] + group_cols
        else:
            group_keys = group_cols

        results = []
        grouped = self.df.groupby(group_keys)

        for keys, group in grouped:
            result = {}

            # Add window keys if present
            if has_window:
                result['window_start'] = self._format_timestamp(keys[0])
                result['window_end'] = self._format_timestamp(keys[1])
                key_offset = 2
            else:
                key_offset = 0

            # Add grouping keys
            if group_cols:
                group_keys_part = keys[key_offset:]
                if len(group_cols) == 1:
                    result[group_cols[0]] = group_keys_part
                else:
                    for i, col in enumerate(group_cols):
                        result[col] = group_keys_part[i]

            # Apply aggregations
            agg_result = self._apply_aggregates_to_group(group, aggregates)
            result.update(agg_result)

            results.append(result)

        return results

    def _format_output(self, result: Any, pipeline: Pipeline) -> str:
        """Format output according to determinism rules."""
        import json

        has_window = hasattr(self, '_window_applied') and self._window_applied
        has_group = hasattr(self, '_group_applied') and self._group_applied

        if isinstance(result, dict):
            # Single object (flat aggregation)
            # Sort keys lexicographically
            return json.dumps(result, sort_keys=True, separators=(',', ':'))

        elif isinstance(result, list):
            # Array of results
            # Sort according to rules

            def sort_key(item):
                keys = []
                if has_window:
                    keys.append(item.get('window_start', ''))
                if has_group:
                    for col in getattr(self, '_group_columns', []):
                        keys.append(item.get(col, ''))
                return tuple(keys)

            result.sort(key=sort_key)

            # Now sort object keys within each item
            def sort_object_keys(obj):
                if isinstance(obj, dict):
                    # Determine key order
                    fixed_keys = []
                    if has_window:
                        fixed_keys = ['window_start', 'window_end']
                    if has_group:
                        fixed_keys.extend(getattr(self, '_group_columns', []))
                    # Agg aliases sorted lexicographically
                    agg_aliases = sorted([a.alias for a in pipeline.aggregates])
                    # Build in order
                    new_obj = {}
                    for k in fixed_keys:
                        if k in obj:
                            new_obj[k] = sort_object_keys(obj[k])
                    for k in agg_aliases:
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
    return parser.parse_args()


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

    # Read DSL
    try:
        dsl_text = read_dsl(args.dsl)
    except IOError as e:
        print(f"ERROR:bad_io:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_IO)

    # Parse DSL
    try:
        parser = DSLParser(dsl_text)
        pipeline = parser.parse()
    except SyntaxError as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)
    except Exception as e:
        print(f"ERROR:bad_dsl:{e}", file=sys.stderr)
        sys.exit(EXIT_BAD_DSL)

    # Load CSV and execute
    try:
        engine = MTLEngine(args.csv, tz=args.tz, timestamp_format=args.timestamp_format)
        output = engine.execute(pipeline)
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
