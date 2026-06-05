#!/usr/bin/env python3
"""
MTL - Metric Transformation Language
A DSL for building reusable metric pipelines with schema inheritance, calculated fields,
pipeline parameters, and post-aggregation LAG/LEAD operations.
"""

import sys
import csv
import json
import argparse
from datetime import datetime, timedelta
from typing import (
    Any, Dict, List, Optional, Tuple, Union, Callable, Set, cast
)
from dataclasses import dataclass, field
from enum import Enum
from abc import ABC, abstractmethod
import operator
from collections import defaultdict

# =============================================================================
# Types
# =============================================================================

class Type(Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    TIMESTAMP = "timestamp"
    ANY = "any"
    NULL = "null"

TYPE_MAP = {
    "string": Type.STRING,
    "int": Type.INT,
    "float": Type.FLOAT,
    "bool": Type.BOOL,
    "timestamp": Type.TIMESTAMP,
}

def parse_type(type_str: str) -> Type:
    t = type_str.lower().strip()
    if t not in TYPE_MAP:
        raise ValueError(f"Unknown type: {type_str}")
    return TYPE_MAP[t]

def is_numeric(t: Type) -> bool:
    return t in (Type.INT, Type.FLOAT)

def is_compatible(dest: Type, src: Type) -> bool:
    if dest == src:
        return True
    if is_numeric(dest) and is_numeric(src):
        return not (src == Type.FLOAT and dest == Type.INT)
    return False

def coerce_type(value: Any, target_type: Type) -> Any:
    if value is None:
        return None
    if target_type == Type.STRING:
        return str(value)
    elif target_type == Type.INT:
        if isinstance(value, float):
            if value != int(value):
                raise TypeError(f"Cannot convert {value} to int")
            return int(value)
        return int(value)
    elif target_type == Type.FLOAT:
        return float(value)
    elif target_type == Type.BOOL:
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'on')
        return bool(value)
    elif target_type == Type.TIMESTAMP:
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            raise ValueError(f"Cannot parse timestamp: {value}")
        return value
    return value

def type_to_type(t: type) -> Type:
    mapping = {str: Type.STRING, int: Type.INT, float: Type.FLOAT, bool: Type.BOOL}
    return mapping.get(t, Type.STRING)

# =============================================================================
# Expressions
# =============================================================================

@dataclass
class Expr(ABC):
    @abstractmethod
    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        pass

    @abstractmethod
    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        pass

@dataclass
class LiteralExpr(Expr):
    value: Any
    type_hint: Type = Type.ANY

    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        if self.type_hint != Type.ANY:
            return self.type_hint
        if isinstance(self.value, bool):
            return Type.BOOL
        elif isinstance(self.value, int):
            return Type.INT
        elif isinstance(self.value, float):
            return Type.FLOAT
        elif isinstance(self.value, str):
            return Type.STRING
        elif isinstance(self.value, datetime):
            return Type.TIMESTAMP
        return Type.ANY

    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return self.value

@dataclass
class ColumnRef(Expr):
    name: str

    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        return schema.get_field_type(self.name)

    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        return ctx.get(self.name)

@dataclass
class BinOpExpr(Expr):
    left: Expr
    op: str
    right: Expr

    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        left_type = self.left.infer_type(schema, params)
        right_type = self.right.infer_type(schema, params)

        if self.op in ('+', '-', '*', '/'):
            if not is_numeric(left_type) or not is_numeric(right_type):
                raise TypeError(f"Numeric operation '{self.op}' requires numeric operands")
            if left_type == Type.FLOAT or right_type == Type.FLOAT:
                return Type.FLOAT
            return Type.INT
        elif self.op == '&':
            if left_type != Type.STRING or right_type != Type.STRING:
                raise TypeError(f"String concat '&' requires string operands")
            return Type.STRING
        return Type.BOOL

    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        left_val = self.left.evaluate(ctx, params)
        right_val = self.right.evaluate(ctx, params)

        if left_val is None or right_val is None:
            if self.op in ('+', '-', '*', '/', '&', 'and', 'or'):
                return None
            return False

        if self.op == '+':
            return left_val + right_val
        elif self.op == '-':
            return left_val - right_val
        elif self.op == '*':
            return left_val * right_val
        elif self.op == '/':
            if right_val == 0:
                raise ZeroDivisionError("Division by zero")
            return left_val / right_val
        elif self.op == '&':
            return str(left_val) + str(right_val)
        elif self.op == 'and':
            return bool(left_val) and bool(right_val)
        elif self.op == 'or':
            return bool(left_val) or bool(right_val)
        elif self.op == '=':
            return left_val == right_val
        elif self.op == '!=':
            return left_val != right_val
        elif self.op == '<':
            return left_val < right_val
        elif self.op == '>':
            return left_val > right_val
        elif self.op == '<=':
            return left_val <= right_val
        elif self.op == '>=':
            return left_val >= right_val
        raise ValueError(f"Unknown operator: {self.op}")

@dataclass
class UnaryOpExpr(Expr):
    op: str
    operand: Expr

    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        inner_type = self.operand.infer_type(schema, params)
        if self.op == 'not':
            if inner_type != Type.BOOL:
                raise TypeError(f"'not' requires bool operand")
            return Type.BOOL
        elif self.op in ('+', '-'):
            if not is_numeric(inner_type):
                raise TypeError(f"Numeric unary op '{self.op}' requires numeric operand")
            return inner_type
        return inner_type

    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        val = self.operand.evaluate(ctx, params)
        if val is None:
            return None
        if self.op == '-':
            return -val
        elif self.op == '+':
            return +val
        elif self.op == 'not':
            return not val
        return val

@dataclass
class FuncCallExpr(Expr):
    func_name: str
    args: List[Expr]

    def infer_type(self, schema: 'Schema', params: Dict[str, Any]) -> Type:
        func_name_lower = self.func_name.lower()

        if func_name_lower == 'param':
            if len(self.args) != 1 or not isinstance(self.args[0], LiteralExpr) or not isinstance(self.args[0].value, str):
                raise TypeError("param() requires a string literal argument")
            param_name = self.args[0].value
            if param_name in params:
                return type_to_type(type(params[param_name]))
            return Type.STRING

        elif func_name_lower in ('lag', 'lead'):
            if len(self.args) != 2:
                raise TypeError(f"{func_name_lower}() requires 2 arguments")
            if not isinstance(self.args[1], LiteralExpr):
                raise TypeError(f"{func_name_lower}() second argument must be a literal")
            if not isinstance(self.args[1].value, int):
                raise TypeError(f"{func_name_lower}() second argument must be an integer")
            return self.args[0].infer_type(schema, params)

        elif func_name_lower == 'coalesce':
            if len(self.args) < 2:
                raise TypeError("coalesce() requires at least 2 arguments")
            return self.args[0].infer_type(schema, params)

        elif func_name_lower == 'cast':
            if len(self.args) != 2:
                raise TypeError("cast() requires 2 arguments")
            if not isinstance(self.args[1], LiteralExpr) or self.args[1].type_hint == Type.ANY:
                raise TypeError("cast() second argument must be a type literal")
            return self.args[1].type_hint

        elif func_name_lower in ('starts_with', 'ends_with', 'contains'):
            if len(self.args) != 2:
                raise TypeError(f"{func_name_lower}() requires 2 arguments")
            for arg in self.args:
                t = arg.infer_type(schema, params)
                if t != Type.STRING:
                    raise TypeError(f"{func_name_lower}() requires string arguments")
            return Type.BOOL

        elif func_name_lower in ('len', 'lower', 'upper', 'abs', 'day', 'month', 'year', 'hour', 'minute'):
            if len(self.args) != 1:
                raise TypeError(f"{func_name_lower}() requires 1 argument")
            if func_name_lower == 'len':
                t = self.args[0].infer_type(schema, params)
                if t != Type.STRING:
                    raise TypeError(f"len() requires string argument")
                return Type.INT
            elif func_name_lower == 'lower':
                t = self.args[0].infer_type(schema, params)
                if t != Type.STRING:
                    raise TypeError(f"lower() requires string argument")
                return Type.STRING
            elif func_name_lower == 'upper':
                t = self.args[0].infer_type(schema, params)
                if t != Type.STRING:
                    raise TypeError(f"upper() requires string argument")
                return Type.STRING
            elif func_name_lower == 'abs':
                t = self.args[0].infer_type(schema, params)
                if not is_numeric(t):
                    raise TypeError(f"abs() requires numeric argument")
                return t
            elif func_name_lower in ('day', 'month', 'year', 'hour', 'minute'):
                t = self.args[0].infer_type(schema, params)
                if t != Type.TIMESTAMP:
                    raise TypeError(f"{func_name_lower}() requires timestamp argument")
                return Type.INT

        raise ValueError(f"Unknown function: {self.func_name}")

    def evaluate(self, ctx: Dict[str, Any], params: Dict[str, Any]) -> Any:
        func_name_lower = self.func_name.lower()
        args_vals = [arg.evaluate(ctx, params) for arg in self.args]

        if func_name_lower == 'param':
            param_name = args_vals[0]
            if param_name not in params:
                raise ValueError(f"Parameter '{param_name}' not provided")
            return params[param_name]

        elif func_name_lower == 'coalesce':
            for val in args_vals:
                if val is not None:
                    return val
            return None

        elif func_name_lower == 'cast':
            val, type_str = args_vals
            target_type = parse_type(type_str)
            return coerce_type(val, target_type)

        elif func_name_lower == 'starts_with':
            s, prefix = args_vals
            if s is None or prefix is None:
                return False
            return str(s).startswith(str(prefix))

        elif func_name_lower == 'ends_with':
            s, suffix = args_vals
            if s is None or suffix is None:
                return False
            return str(s).endswith(str(suffix))

        elif func_name_lower == 'contains':
            s, substr = args_vals
            if s is None or substr is None:
                return False
            return str(substr) in str(s)

        elif func_name_lower == 'len':
            s = args_vals[0]
            return len(s) if s is not None else 0

        elif func_name_lower == 'lower':
            s = args_vals[0]
            return s.lower() if s is not None else None

        elif func_name_lower == 'upper':
            s = args_vals[0]
            return s.upper() if s is not None else None

        elif func_name_lower == 'abs':
            x = args_vals[0]
            return abs(x) if x is not None else None

        elif func_name_lower == 'day':
            dt = args_vals[0]
            return dt.day if isinstance(dt, datetime) else None

        elif func_name_lower == 'month':
            dt = args_vals[0]
            return dt.month if isinstance(dt, datetime) else None

        elif func_name_lower == 'year':
            dt = args_vals[0]
            return dt.year if isinstance(dt, datetime) else None

        elif func_name_lower == 'hour':
            dt = args_vals[0]
            return dt.hour if isinstance(dt, datetime) else None

        elif func_name_lower == 'minute':
            dt = args_vals[0]
            return dt.minute if isinstance(dt, datetime) else None

        elif func_name_lower == 'lag':
            # lag is handled during post-aggregation
            return None  # placeholder

        elif func_name_lower == 'lead':
            # lead is handled during post-aggregation
            return None  # placeholder

        raise ValueError(f"Unknown function: {self.func_name}")

# =============================================================================
# Schema System
# =============================================================================

@dataclass
class SchemaField:
    name: str
    type: Type
    calculated: bool = False
    expr: Optional[Expr] = None

    def __hash__(self):
        return hash(self.name)

class Schema:
    def __init__(self, name: str, base: Optional['Schema'] = None):
        self.name = name
        self.base = base
        self.fields: Dict[str, SchemaField] = {}
        if base:
            for name, field in base.fields.items():
                self.fields[name] = SchemaField(field.name, field.type, field.calculated, field.expr)

    def add_field(self, name: str, type_: Type, calculated: bool = False, expr: Optional[Expr] = None) -> None:
        if name in self.fields:
            existing = self.fields[name]
            if not is_compatible(type_, existing.type):
                raise TypeError(f"Illegal type override for '{name}': {existing.type} -> {type_}")
            self.fields[name] = SchemaField(name, type_, calculated, expr)
        else:
            self.fields[name] = SchemaField(name, type_, calculated, expr)

    def get_field_type(self, name: str) -> Type:
        if name not in self.fields:
            raise KeyError(f"Unknown field: {name}")
        return self.fields[name].type

    def is_calculated(self, name: str) -> bool:
        if name not in self.fields:
            raise KeyError(f"Unknown field: {name}")
        return self.fields[name].calculated

    def get_field(self, name: str) -> SchemaField:
        if name not in self.fields:
            raise KeyError(f"Unknown field: {name}")
        return self.fields[name]

    def get_physical_fields(self) -> List[str]:
        return [name for name, field in self.fields.items() if not field.calculated]

    def get_all_fields(self) -> List[str]:
        if self.base:
            base_fields = self.base.get_all_fields()
            derived_fields = [name for name in self.fields.keys() if name not in base_fields]
            return base_fields + derived_fields
        return list(self.fields.keys())

# =============================================================================
# Pipeline Components
# =============================================================================

@dataclass
class CalcField:
    name: str
    expr: Expr
    type: Type
    stage: str

@dataclass
class Parameter:
    name: str
    type: Optional[Type] = None
    default: Any = None

@dataclass
class FilterStmt:
    expr: Expr

@dataclass
class ApplyStmt:
    name: str
    expr: Expr
    type: Type

@dataclass
class GroupByStmt:
    keys: List[str]

@dataclass
class WindowStmt:
    size_minutes: int

@dataclass
class AggregateFunc(Enum):
    SUM = "sum"
    COUNT = "count"
    AVERAGE = "average"
    MIN = "min"
    MAX = "max"
    FIRST = "first"
    LAST = "last"

@dataclass
class AggregateField:
    expr: Expr
    alias: str
    func: AggregateFunc
    type: Type

@dataclass
class AggregateStmt:
    fields: List[AggregateField]

# =============================================================================
# DSL Parser (Manual Parser)
# =============================================================================

class MTLParser:
    def __init__(self, source: str, params: Dict[str, Any] = None):
        self.source = source
        self.params = params or {}
        self.pos = 0
        self.line = 1
        self.schemas: Dict[str, Schema] = {}

    def peek(self, n: int = 1) -> str:
        if self.pos + n - 1 >= len(self.source):
            return ''
        return self.source[self.pos:self.pos+n]

    def consume(self) -> str:
        ch = self.peek()
        self.pos += 1
        if ch == '\n':
            self.line += 1
        return ch

    def skip_whitespace(self):
        while self.peek() in ' \t\r':
            self.consume()

    def skip_line(self):
        while self.peek() and self.peek() != '\n':
            self.consume()
        if self.peek() == '\n':
            self.consume()

    def read_ident(self) -> str:
        start = self.pos
        if not self.peek().isalpha() and self.peek() != '_':
            raise SyntaxError(f"Expected identifier at line {self.line}")
        while self.peek() and (self.peek().isalnum() or self.peek() == '_'):
            self.consume()
        return self.source[start:self.pos]

    def read_string(self) -> str:
        if self.peek() not in '"\'':
            raise SyntaxError(f"Expected string literal at line {self.line}")
        quote = self.consume()
        start = self.pos
        while self.peek() and self.peek() != quote:
            if self.peek() == '\\':
                self.consume()
            self.consume()
        if not self.peek():
            raise SyntaxError(f"Unterminated string at line {self.line}")
        self.consume()
        return self.source[start:self.pos-1]

    def read_number(self) -> str:
        start = self.pos
        while self.peek() and (self.peek().isdigit() or self.peek() == '.' or self.peek() == '-'):
            self.consume()
        return self.source[start:self.pos]

    def parse_literal(self) -> LiteralExpr:
        self.skip_whitespace()
        if self.peek() in '"\'':
            return LiteralExpr(self.read_string(), Type.STRING)
        elif self.peek().isdigit() or self.peek() == '-':
            num_str = self.read_number()
            if '.' in num_str:
                return LiteralExpr(float(num_str), Type.FLOAT)
            else:
                return LiteralExpr(int(num_str), Type.INT)
        elif self.peek() in 'ftTF':
            ident = self.read_ident().lower()
            if ident == 'true':
                return LiteralExpr(True, Type.BOOL)
            elif ident == 'false':
                return LiteralExpr(False, Type.BOOL)
        ident = self.read_ident().lower()
        if ident == 'null':
            return LiteralExpr(None, Type.NULL)
        raise SyntaxError(f"Unexpected literal at line {self.line}")

    def parse_type(self) -> Type:
        self.skip_whitespace()
        type_str = self.read_ident().lower()
        return parse_type(type_str)

    def parse_expr(self, parent: Optional[str] = None) -> Expr:
        return self._parse_or()

    def _parse_or(self) -> Expr:
        left = self._parse_and()
        while True:
            self.skip_whitespace()
            if self.peek(2) == 'or':
                self.consume()
                self.consume()
                right = self._parse_and()
                left = BinOpExpr(left, 'or', right)
            elif self.peek() == '|':
                self.consume()
                right = self._parse_and()
                left = BinOpExpr(left, '|', right)
            else:
                break
        return left

    def _parse_and(self) -> Expr:
        left = self._parse_comparison()
        while True:
            self.skip_whitespace()
            if self.peek(3) == 'and':
                for _ in range(3):
                    self.consume()
                right = self._parse_comparison()
                left = BinOpExpr(left, 'and', right)
            elif self.peek(2) == '&&':
                self.consume()
                self.consume()
                right = self._parse_comparison()
                left = BinOpExpr(left, 'and', right)
            elif self.peek() == '&':
                self.consume()
                right = self._parse_comparison()
                left = BinOpExpr(left, '&', right)
            else:
                break
        return left

    def _parse_comparison(self) -> Expr:
        left = self._parse_add_sub()
        self.skip_whitespace()
        op_str = None
        if self.peek(2) in ('=', '!', '<', '>'):
            op_char = self.consume()
            if op_char == '=' and self.peek() == '=':
                op_char = '=='
                self.consume()
            elif op_char == '!' and self.peek() == '=':
                op_char = '!='
                self.consume()
            elif op_char == '<' and self.peek() == '=':
                op_char = '<='
                self.consume()
            elif op_char == '>' and self.peek() == '=':
                op_char = '>='
                self.consume()
            op_str = op_char
        if op_str:
            right = self._parse_add_sub()
            op_map = {'=': '=', '==': '=', '!=': '!=', '<': '<', '>': '>', '<=': '<=', '>=': '>='}
            return BinOpExpr(left, op_map.get(op_str, op_str), right)
        return left

    def _parse_add_sub(self) -> Expr:
        left = self._parse_mul_div()
        while True:
            self.skip_whitespace()
            if self.peek() in ('+', '-'):
                op = self.consume()
                right = self._parse_mul_div()
                left = BinOpExpr(left, op, right)
            else:
                break
        return left

    def _parse_mul_div(self) -> Expr:
        left = self._parse_unary()
        while True:
            self.skip_whitespace()
            if self.peek() in ('*', '/'):
                op = self.consume()
                right = self._parse_unary()
                left = BinOpExpr(left, op, right)
            else:
                break
        return left

    def _parse_unary(self) -> Expr:
        self.skip_whitespace()
        if self.peek() in ('+', '-', '|'):
            op_char = self.consume()
            if op_char == '|' and self.peek() == '|':
                self.consume()
                operand = self._parse_unary()
                return UnaryOpExpr('or', operand)
            operand = self._parse_unary()
            return UnaryOpExpr(op_char, operand)
        return self._parse_leaf()

    def _parse_leaf(self) -> Expr:
        self.skip_whitespace()
        if not self.peek():
            raise SyntaxError(f"Unexpected end of expression at line {self.line}")

        if self.peek() in '"\'':
            return self.parse_literal()
        elif self.peek().isdigit() or self.peek() == '-':
            return self.parse_literal()

        ident = self.read_ident()

        self.skip_whitespace()
        if self.peek() == '(':
            self.consume()
            args = []
            self.skip_whitespace()
            if self.peek() != ')':
                args.append(self.parse_expr())
                while self.peek() == ',':
                    self.consume()
                    self.skip_whitespace()
                    args.append(self.parse_expr())
            if self.peek() != ')':
                raise SyntaxError(f"Expected ')' in function call at line {self.line}")
            self.consume()

            func_name_lower = ident.lower()

            if func_name_lower == 'param':
                if len(args) != 1 or not isinstance(args[0], LiteralExpr) or args[0].type_hint != Type.STRING:
                    raise SyntaxError("param() requires a string literal argument")
                param_name = args[0].value
                if param_name not in self.params:
                    raise SyntaxError(f"Parameter '{param_name}' not provided")
                param_val = self.params[param_name]
                return LiteralExpr(param_val, type_to_type(type(param_val)))

            elif func_name_lower in ('coalesce', 'starts_with', 'ends_with', 'contains',
                                      'len', 'lower', 'upper', 'abs', 'day', 'month', 'year',
                                      'hour', 'minute', 'cast', 'lag', 'lead'):
                return FuncCallExpr(ident, args)

            else:
                raise SyntaxError(f"Unknown function: {ident}")
        else:
            return ColumnRef(ident)

    def parse_schema_block(self) -> Tuple[str, Optional[str], List]:
        self.skip_whitespace()
        word = self.read_ident()
        if word != 'schema':
            raise SyntaxError(f"Expected 'schema' at line {self.line}")

        self.skip_whitespace()
        name = self.read_ident()

        self.skip_whitespace()
        base = None
        if self.peek() == 'e':
            pos_save = self.pos
            word = self.read_ident()
            if word == 'extends':
                self.skip_whitespace()
                base = self.read_ident()
            else:
                self.pos = pos_save

        self.skip_whitespace()
        if self.peek() != '{':
            raise SyntaxError(f"Expected '{{' after schema name at line {self.line}")
        self.consume()

        fields = []
        while True:
            self.skip_whitespace()
            if self.peek() == '}':
                self.consume()
                break
            if self.peek() == '\n':
                self.consume()
                continue

            field_name = self.read_ident()

            self.skip_whitespace()
            if self.consume() != ':':
                raise SyntaxError(f"Expected ':' after field name '{field_name}' at line {self.line}")

            self.skip_whitespace()
            field_type = self.parse_type()

            calc_expr = None
            self.skip_whitespace()
            if self.peek() == '=':
                self.consume()
                calc_expr = self.parse_expr()

            while self.peek() and self.peek() not in ('\n', '}'):
                if self.peek() == '#':
                    self.skip_line()
                    break
                self.consume()

            if self.peek() == '\n':
                self.consume()

            fields.append((field_name, field_type, calc_expr))

        return name, base, fields

    def parse_params_block(self) -> Dict[str, Parameter]:
        params = {}
        while True:
            self.skip_whitespace()
            if self.peek() == '}':
                self.consume()
                break
            if self.peek() == '\n':
                self.consume()
                continue

            name = self.read_ident()

            self.skip_whitespace()
            type_ = None
            default = None

            if self.peek() == ':':
                self.consume()
                self.skip_whitespace()
                type_ = self.parse_type()

            self.skip_whitespace()
            if self.peek() == '=':
                self.consume()
                self.skip_whitespace()
                default = self.parse_literal().value

            while self.peek() and self.peek() not in ('\n', '}'):
                if self.peek() == '#':
                    self.skip_line()
                    break
                self.consume()

            if self.peek() == '\n':
                self.consume()

            params[name] = Parameter(name, type_, default)

        return params

    def parse_filter(self) -> FilterStmt:
        self.skip_whitespace()
        if self.consume() != '(' or self.consume() != '(':
            raise SyntaxError("Expected '((filter'")
        word = self.read_ident()
        if word != 'filter':
            raise SyntaxError(f"Expected 'filter', got '{word}'")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after filter")
        expr = self.parse_expr()
        if self.consume() != ')':
            raise SyntaxError("Expected ')' after filter expression")
        if self.consume() != ')':
            raise SyntaxError("Expected '))' to close filter")
        self.skip_line()
        return FilterStmt(expr)

    def parse_apply(self) -> ApplyStmt:
        self.skip_whitespace()
        if self.consume() != '(' or self.consume() != '(':
            raise SyntaxError("Expected '((apply'")
        word = self.read_ident()
        if word != 'apply':
            raise SyntaxError(f"Expected 'apply', got '{word}'")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after apply")
        name = self.read_ident()
        if self.consume() != '=':
            raise SyntaxError("Expected '=' after apply name")
        expr = self.parse_expr()
        if self.consume() != ')':
            raise SyntaxError("Expected ')' after apply expression")
        if self.consume() != ')':
            raise SyntaxError("Expected '))' to close apply")
        self.skip_line()
        # Type inference for apply
        return ApplyStmt(name, expr, None)

    def parse_group_by(self) -> GroupByStmt:
        self.skip_whitespace()
        if self.consume() != '(' or self.consume() != '(':
            raise SyntaxError("Expected '((group_by'")
        word = self.read_ident()
        if word != 'group_by':
            raise SyntaxError(f"Expected 'group_by', got '{word}'")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after group_by")
        keys = []
        if self.peek() != ')':
            keys.append(self.read_ident())
            while self.peek() == ',':
                self.consume()
                self.skip_whitespace()
                keys.append(self.read_ident())
        if self.consume() != ')':
            raise SyntaxError("Expected ')' after group_by keys")
        if self.consume() != ')':
            raise SyntaxError("Expected '))' to close group_by")
        self.skip_line()
        return GroupByStmt(keys)

    def parse_window(self) -> WindowStmt:
        self.skip_whitespace()
        if self.consume() != '(' or self.consume() != '(':
            raise SyntaxError("Expected '((window'")
        word = self.read_ident()
        if word != 'window':
            raise SyntaxError(f"Expected 'window', got '{word}'")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after window")
        num_str = self.read_number()
        num = float(num_str)
        self.skip_whitespace()
        unit = self.read_ident().lower()
        if self.consume() != ')':
            raise SyntaxError("Expected ')' after window size")
        if self.consume() != ')':
            raise SyntaxError("Expected '))' to close window")
        self.skip_line()

        if unit in ('m', 'minute', 'minutes'):
            minutes = num
        elif unit in ('h', 'hour', 'hours'):
            minutes = num * 60
        elif unit in ('d', 'day', 'days'):
            minutes = num * 1440
        elif unit in ('s', 'second', 'seconds'):
            minutes = num / 60
        elif unit in ('ms', 'millisecond', 'milliseconds'):
            minutes = num / 60000
        else:
            raise SyntaxError(f"Unknown time unit: {unit}")

        return WindowStmt(int(minutes))

    def parse_aggregate(self) -> AggregateStmt:
        self.skip_whitespace()
        if self.consume() != '(' or self.consume() != '(':
            raise SyntaxError("Expected '((aggregate'")
        word = self.read_ident()
        if word != 'aggregate':
            raise SyntaxError(f"Expected 'aggregate', got '{word}'")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after aggregate")

        fields = []
        while True:
            self.skip_whitespace()
            if self.peek() == ')':
                break

            expr = self.parse_expr()

            self.skip_whitespace()
            if self.peek() in ('as', ':'):
                if self.peek() == 'a':
                    for _ in range(2):
                        self.consume()
                else:
                    self.consume()

            self.skip_whitespace()
            alias = self.read_ident()

            func = AggregateFunc.SUM

            self.skip_whitespace()
            if self.peek() == ',':
                self.consume()
            elif self.peek() == ')':
                break

            fields.append(AggregateField(expr, alias, func, None))

        if self.consume() != ')':
            raise SyntaxError("Expected ')' after aggregate fields")
        if self.consume() != ')':
            raise SyntaxError("Expected '))' to close aggregate")
        self.skip_line()
        return AggregateStmt(fields)

    def parse_calc(self) -> CalcField:
        self.skip_whitespace()
        if self.peek() != 'c':
            raise SyntaxError(f"Expected 'calc' at line {self.line}")
        word = self.read_ident()
        if word != 'calc':
            raise SyntaxError(f"Expected 'calc', got '{word}'")

        self.skip_whitespace()
        name = self.read_ident()

        self.skip_whitespace()
        if self.consume() != '=':
            raise SyntaxError("Expected '=' after calc name")

        expr = self.parse_expr()

        self.skip_whitespace()
        if self.consume() != ':':
            raise SyntaxError("Expected ':' before type in calc")

        type_ = self.parse_type()

        self.skip_whitespace()
        if self.peek() != '@':
            raise SyntaxError("Expected '@stage(' in calc")
        self.consume()
        if self.consume() != 's' or self.read_ident() != 'stage':
            raise SyntaxError("Expected 'stage' in calc")
        if self.consume() != '(':
            raise SyntaxError("Expected '(' after @stage")

        stage_word = self.read_ident().lower()
        stage = 'pre' if stage_word == 'pre' else 'post_agg'

        if self.consume() != ')':
            raise SyntaxError("Expected ')' after stage word")
        if self.consume() != ')':
            raise SyntaxError("Expected ')' after @stage(...)")

        self.skip_line()
        return CalcField(name, expr, type_, stage)

    def parse_pipeline_body(self, schema_name: str) -> Tuple[Optional[Schema], Dict[str, Parameter], List[CalcField],
                                                           Optional[FilterStmt], List[ApplyStmt],
                                                           Optional[GroupByStmt], Optional[WindowStmt],
                                                           AggregateStmt, List[CalcField]]:
        params = {}
        pre_calcs = []
        filter_stmt = None
        apply_stmts = []
        group_by = None
        window = None
        aggregate = None
        post_agg_calcs = []

        # Parse params block if present
        self.skip_whitespace()
        if self.peek(6) == 'params':
            pos_save = self.pos
            word = self.read_ident()
            if word == 'params':
                if self.consume() != '{':
                    raise SyntaxError("Expected '{' after params")
                self.skip_line()
                params = self.parse_params_block()
            else:
                self.pos = pos_save

        # Parse statements until aggregate
        while self.peek() and self.peek() != ')':
            self.skip_whitespace()
            if not self.peek():
                break

            if self.peek(2) == '((':
                pos_save = self.pos
                # Read ahead to identify the statement type
                peek_chars = []
                while len(peek_chars) < 12 and self.peek() not in ' \n\r\t\v':
                    peek_chars.append(self.peek())
                peek_str = ''.join(peek_chars)

                if peek_str.startswith('((filter'):
                    filter_stmt = self.parse_filter()
                elif peek_str.startswith('((apply'):
                    apply_stmts.append(self.parse_apply())
                elif peek_str.startswith('((group_by'):
                    group_by = self.parse_group_by()
                elif peek_str.startswith('((window'):
                    window = self.parse_window()
                elif peek_str.startswith('((aggregate'):
                    break
                elif peek_str.startswith('((calc'):
                    # Reset position and parse calc
                    self.pos = pos_save
                    pre_calcs.append(self.parse_calc())
                else:
                    self.pos = pos_save
                    # Check if it's a calc statement without parentheses
                    self.skip_whitespace()
                    if self.peek() == 'c':
                        pre_calcs.append(self.parse_calc())
                    else:
                        raise SyntaxError(f"Unexpected statement: {peek_str} at line {self.line}")
            else:
                self.skip_whitespace()
                if self.peek() == 'c':
                    pre_calcs.append(self.parse_calc())
                elif self.peek() == '\n':
                    self.consume()
                elif self.peek() == '#':
                    self.skip_line()
                elif self.peek() == '}':
                    break
                else:
                    # Read the next token to understand what it is
                    next_char = self.peek(2)
                    if next_char and next_char[0].isalpha():
                        # It might be 'calc' without parentheses
                        ident = self.read_ident()
                        if ident == 'calc':
                            self.pos = self.pos - len('calc')
                            pre_calcs.append(self.parse_calc())
                        else:
                            raise SyntaxError(f"Unexpected identifier: {ident} at line {self.line}")
                    else:
                        raise SyntaxError(f"Unexpected character: {self.peek()} at line {self.line}")

        if self.peek(2) == '((':  # aggregate must start with '((aggregate'
            if self.source[self.pos:self.pos+12] == '((aggregate':
                aggregate = self.parse_aggregate()

        # Parse post-aggregation calculations
        while self.peek():
            self.skip_whitespace()
            if not self.peek():
                break
            if self.peek() == 'c':
                post_agg_calcs.append(self.parse_calc())
            elif self.peek() == '\n':
                self.consume()
            elif self.peek() == '#':
                self.skip_line()
            elif self.peek() == '}':
                self.consume()
                break
            else:
                self.skip_line()

        schema = self.schemas.get(schema_name)

        return schema, params, pre_calcs, filter_stmt, apply_stmts, group_by, window, aggregate, post_agg_calcs

    def parse(self) -> Tuple[Dict[str, Schema], Tuple[str, str, Dict[str, Parameter], List[CalcField],
                                                         Optional[FilterStmt], List[ApplyStmt],
                                                         Optional[GroupByStmt], Optional[WindowStmt],
                                                         AggregateStmt, List[CalcField]]]:
        schemas = {}
        pipeline = None

        while self.peek():
            self.skip_whitespace()
            if not self.peek():
                break

            if self.peek() == 's':
                name, base, fields = self.parse_schema_block()

                base_schema = None
                if base:
                    if base not in schemas:
                        raise SyntaxError(f"Base schema '{base}' not found before use")
                    base_schema = schemas[base]

                schema = Schema(name, base_schema)
                for field_name, field_type, calc_expr in fields:
                    try:
                        schema.add_field(field_name, field_type, calculated=(calc_expr is not None), expr=calc_expr)
                    except TypeError as e:
                        raise SyntaxError(f"Schema '{name}' field '{field_name}': {e}")

                schemas[name] = schema

            elif self.peek() == 'p':
                pos_save = self.pos
                word = self.read_ident()
                if word == 'pipeline':
                    self.skip_whitespace()
                    name = self.read_ident()

                    self.skip_whitespace()
                    if self.read_ident() != 'using':
                        raise SyntaxError("Expected 'using' after pipeline name")

                    self.skip_whitespace()
                    schema_name = self.read_ident()

                    self.skip_whitespace()
                    if self.consume() != '{':
                        raise SyntaxError("Expected '{' after pipeline header")
                    self.skip_line()

                    schema, params, pre_calcs, filter_stmt, apply_stmts, group_by, window, aggregate, post_agg_calcs = \
                        self.parse_pipeline_body(schema_name)

                    pipeline = (name, schema_name, params, pre_calcs, filter_stmt, apply_stmts,
                               group_by, window, aggregate, post_agg_calcs)
                    pipeline_schema = schema
                else:
                    self.pos = pos_save
                    raise SyntaxError(f"Expected 'pipeline', got '{word}'")
            else:
                if self.peek() == '#':
                    self.skip_line()
                elif self.peek() not in ' \t\n\r':
                    raise SyntaxError(f"Unexpected character: {self.peek()} at line {self.line}")
                else:
                    self.consume()

        if not pipeline:
            raise SyntaxError("No pipeline block found in DSL")

        return schemas, pipeline

# =============================================================================
# Pipeline Executor
# =============================================================================

class PipelineExecutor:
    def __init__(self, schemas: Dict[str, Schema], pipeline: Tuple,
                 params: Dict[str, Any], csv_headers: List[str],
                 timestamp_format: Optional[str] = None, tz: Optional[str] = None):
        self.schemas = schemas
        self.pipeline = pipeline
        self.params = params
        self.csv_headers = csv_headers
        self.timestamp_format = timestamp_format
        self.tz = tz

        # Unpack pipeline
        (name, schema_name, param_defs, pre_calcs, filter_stmt, apply_stmts,
         group_by, window, aggregate, post_agg_calcs) = pipeline

        self.name = name
        self.schema_name = schema_name
        self.pre_calcs = pre_calcs
        self.filter_stmt = filter_stmt
        self.apply_stmts = apply_stmts
        self.group_by = group_by
        self.window = window
        self.aggregate = aggregate
        self.post_agg_calcs = post_agg_calcs

        # Get schema
        self.schema = schemas.get(schema_name)
        if not self.schema:
            raise ValueError(f"Schema '{schema_name}' not found")

        # Build parameter dict with defaults
        self.resolved_params = {}
        for param_def in param_defs.values():
            name = param_def.name
            param_type = param_def.type
            default = param_def.default

            if name in params:
                # Override with CLI param
                val = params[name]
                if param_type:
                    # Type check
                    try:
                        if param_type == Type.INT:
                            val = int(val)
                        elif param_type == Type.FLOAT:
                            val = float(val)
                        elif param_type == Type.BOOL:
                            val = str(val).lower() in ('true', '1', 'yes', 'on')
                        elif param_type == Type.STRING:
                            val = str(val)
                        else:
                            val = str(val)
                    except (ValueError, TypeError):
                        raise TypeError(f"Cannot convert param '{name}' value '{val}' to type {param_type}")
                self.resolved_params[name] = val
            elif default is not None:
                self.resolved_params[name] = default
            else:
                raise ValueError(f"Parameter '{name}' not provided and has no default")

        # Validate all required params are provided
        for param_def in param_defs.values():
            if param_def.name not in self.resolved_params and param_def.default is None:
                raise ValueError(f"Parameter '{param_def.name}' not provided")

    def parse_timestamp(self, ts_str: str) -> datetime:
        if self.timestamp_format:
            return datetime.strptime(ts_str, self.timestamp_format)
        # Try common formats
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
        raise ValueError(f"Cannot parse timestamp: {ts_str}")

    def evaluate_expr(self, expr: Expr, ctx: Dict[str, Any], schema: Schema) -> Any:
        """Evaluate expression with schema context."""
        return expr.evaluate(ctx, self.resolved_params)

    def infer_expr_type(self, expr: Expr, schema: Schema) -> Type:
        """Infer expression type."""
        return expr.infer_type(schema, self.resolved_params)

    def execute(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Step 1: Parse timestamps if timestamp column exists
        timestamp_col = None
        for field_name in self.schema.get_physical_fields():
            if self.schema.get_field_type(field_name) == Type.TIMESTAMP:
                timestamp_col = field_name
                break

        if timestamp_col:
            for row in rows:
                if timestamp_col in row and row[timestamp_col]:
                    try:
                        row[timestamp_col] = self.parse_timestamp(str(row[timestamp_col]))
                    except ValueError:
                        pass  # Keep as string if parsing fails

        # Step 2: Apply schema-level calculated fields
        for field_name, field in self.schema.fields.items():
            if field.calculated and field.expr:
                for row in rows:
                    try:
                        row[field_name] = self.evaluate_expr(field.expr, row, self.schema)
                    except Exception as e:
                        raise RuntimeError(f"Error evaluating schema calc '{field_name}': {e}")

        # Step 3: Apply pipeline pre-calculated fields
        for calc in self.pre_calcs:
            for row in rows:
                try:
                    row[calc.name] = self.evaluate_expr(calc.expr, row, self.schema)
                except Exception as e:
                    raise RuntimeError(f"Error evaluating pre-calc '{calc.name}': {e}")

        # Step 4: Apply filter
        if self.filter_stmt:
            filtered_rows = []
            for row in rows:
                try:
                    result = self.evaluate_expr(self.filter_stmt.expr, row, self.schema)
                    if result:
                        filtered_rows.append(row)
                except Exception as e:
                    raise RuntimeError(f"Error in filter: {e}")
            rows = filtered_rows

        # Step 5: Apply windowing (if any)
        if self.window and timestamp_col:
            # Sort by timestamp
            rows.sort(key=lambda r: (r.get(timestamp_col) or '').zfill(20))

            # Create windows
            windowed_rows = []
            current_window_start = None
            current_window_end = None
            current_window_rows = []

            for row in rows:
                ts = row.get(timestamp_col)
                if not isinstance(ts, datetime):
                    continue

                # Calculate window boundaries
                window_start = ts - timedelta(minutes=(ts.minute % self.window.size_minutes),
                                              seconds=ts.second,
                                              microseconds=ts.microsecond)
                window_end = window_start + timedelta(minutes=self.window.size_minutes)

                if current_window_start != window_start:
                    # Emit previous window
                    for r in current_window_rows:
                        r['window_start'] = current_window_start
                        r['window_end'] = current_window_end
                    windowed_rows.extend(current_window_rows)

                    current_window_start = window_start
                    current_window_end = window_end
                    current_window_rows = []

                row['window_start'] = window_start
                row['window_end'] = window_end
                current_window_rows.append(row)

            # Emit last window
            for r in current_window_rows:
                r['window_start'] = current_window_start
                r['window_end'] = current_window_end
            windowed_rows.extend(current_window_rows)

            rows = windowed_rows

        # Step 6: Apply apply statements (create new columns)
        for apply_stmt in self.apply_stmts:
            for row in rows:
                try:
                    row[apply_stmt.name] = self.evaluate_expr(apply_stmt.expr, row, self.schema)
                except Exception as e:
                    raise RuntimeError(f"Error in apply '{apply_stmt.name}': {e}")

        # Step 7: Group by
        grouped_data = []
        if self.group_by:
            groups = defaultdict(list)
            for row in rows:
                key = tuple(str(row.get(k, '')) for k in self.group_by.keys)
                groups[key].append(row)

            for key_tuple, group_rows in groups.items():
                group_ctx = {}
                for i, key_name in enumerate(self.group_by.keys):
                    group_ctx[key_name] = key_tuple[i]
                grouped_data.append({
                    'key': key_tuple,
                    'rows': group_rows,
                    'ctx': group_ctx
                })
        else:
            # No group by - single group
            grouped_data.append({
                'key': (),
                'rows': rows,
                'ctx': {}
            })

        # Step 8: Aggregate
        results = []
        for group in grouped_data:
            result = dict(group['ctx'])

            # Add window info if any
            if self.window and group['rows']:
                result['window_start'] = group['rows'][0].get('window_start')
                result['window_end'] = group['rows'][0].get('window_end')

            for agg_field in self.aggregate.fields:
                try:
                    values = []
                    for row in group['rows']:
                        val = self.evaluate_expr(agg_field.expr, row, self.schema)
                        if val is not None:
                            values.append(val)

                    if agg_field.func == AggregateFunc.SUM:
                        result[agg_field.alias] = sum(values) if values else None
                    elif agg_field.func == AggregateFunc.COUNT:
                        result[agg_field.alias] = len(group['rows'])
                    elif agg_field.func == AggregateFunc.AVERAGE:
                        result[agg_field.alias] = sum(values) / len(values) if values else None
                    elif agg_field.func == AggregateFunc.MIN:
                        result[agg_field.alias] = min(values) if values else None
                    elif agg_field.func == AggregateFunc.MAX:
                        result[agg_field.alias] = max(values) if values else None
                    elif agg_field.func == AggregateFunc.FIRST:
                        result[agg_field.alias] = values[0] if values else None
                    elif agg_field.func == AggregateFunc.LAST:
                        result[agg_field.alias] = values[-1] if values else None
                except Exception as e:
                    raise RuntimeError(f"Error in aggregate '{agg_field.alias}': {e}")

            results.append(result)

        # Step 9: Sort results for lag/lead
        # Sort by window_start (if present), then by group keys
        def sort_key(r):
            ws = r.get('window_start', datetime.min)
            if isinstance(ws, datetime):
                ws = ws.timestamp()
            keys = [str(r.get(k, '')) for k in (self.group_by.keys if self.group_by else [])]
            return (ws, tuple(keys))

        results.sort(key=sort_key)

        # Step 10: Apply post-aggregation calculations
        for calc in self.post_agg_calcs:
            for i, result in enumerate(results):
                try:
                    result[calc.name] = self.evaluate_expr(calc.expr, result, self.schema)
                except Exception as e:
                    raise RuntimeError(f"Error in post-agg calc '{calc.name}': {e}")

        return results

    def get_output_keys(self) -> List[str]:
        """Get output keys in Normalization order."""
        keys = []

        # Window keys
        if self.window:
            keys.extend(['window_start', 'window_end'])

        # Group by keys
        if self.group_by:
            keys.extend(self.group_by.keys)

        # Aggregate aliases (lexicographic)
        if self.aggregate:
            agg_aliases = sorted([f.alias for f in self.aggregate.fields])
            keys.extend(agg_aliases)

        # Post-agg calc keys (lexicographic)
        post_agg_names = sorted([c.name for c in self.post_agg_calcs])
        keys.extend(post_agg_names)

        return keys

# =============================================================================
# CLI
# =============================================================================

def parse_csv(path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = []
        for row in reader:
            rows.append(dict(row))
        return headers, rows

def parse_params(param_strings: List[str]) -> Dict[str, Any]:
    params = {}
    for p in param_strings:
        if '=' in p:
            key, value = p.split('=', 1)
            params[key.strip()] = value.strip()
    return params

def exit_with_error(code: int, message: str):
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(code)

def main():
    parser = argparse.ArgumentParser(description='MTL - Metric Transformation Language')
    parser.add_argument('--csv', required=True, help='Path to CSV file')
    parser.add_argument('--dsl', required=True, help='Path to DSL file or - for stdin')
    parser.add_argument('--out', help='Output path (default: stdout)')
    parser.add_argument('--tz', help='IANA timezone')
    parser.add_argument('--timestamp-format', help='Timestamp format string')
    parser.add_argument('--strict', action='store_true', help='Enable strict mode')
    parser.add_argument('--param', action='append', default=[], help='Pipeline parameters (key=value)')

    args = parser.parse_args()

    # Parse parameters
    params = parse_params(args.param)

    # Read DSL
    if args.dsl == '-':
        dsl_source = sys.stdin.read()
    else:
        try:
            with open(args.dsl, 'r') as f:
                dsl_source = f.read()
        except FileNotFoundError:
            exit_with_error(1, f"DSL file not found: {args.dsl}")

    # Parse CSV
    try:
        csv_headers, csv_rows = parse_csv(args.csv)
    except FileNotFoundError:
        exit_with_error(1, f"CSV file not found: {args.csv}")
    except Exception as e:
        exit_with_error(1, f"CSV error: {e}")

    # Parse DSL
    try:
        mtl_parser = MTLParser(dsl_source, params)
        schemas, pipeline = mtl_parser.parse()
    except SyntaxError as e:
        exit_with_error(1, f"DSL syntax error: {e}")
    except ValueError as e:
        exit_with_error(2, f"Type error: {e}")
    except Exception as e:
        exit_with_error(1, f"DSL error: {e}")

    # Execute pipeline
    try:
        executor = PipelineExecutor(
            schemas, pipeline, params, csv_headers,
            timestamp_format=args.timestamp_format, tz=args.tz
        )
        results = executor.execute(csv_rows)
    except ValueError as e:
        exit_with_error(1, f"DSL error: {e}")
    except TypeError as e:
        exit_with_error(2, f"Type error: {e}")
    except RuntimeError as e:
        exit_with_error(4, f"Runtime error: {e}")
    except ZeroDivisionError as e:
        exit_with_error(4, f"Runtime error (division by zero): {e}")
    except Exception as e:
        exit_with_error(4, f"Runtime error: {e}")

    # Output results
    output_keys = executor.get_output_keys()

    output_lines = []
    # Header
    output_lines.append(','.join(output_keys))

    # Data rows
    for row in results:
        values = []
        for key in output_keys:
            val = row.get(key)
            if val is None:
                values.append('')
            elif isinstance(val, datetime):
                values.append(val.strftime('%Y-%m-%d %H:%M:%S'))
            elif isinstance(val, float):
                # Avoid scientific notation for large floats
                if val != val:  # NaN check
                    values.append('')
                else:
                    values.append(str(val))
            else:
                values.append(str(val))
        output_lines.append(','.join(values))

    output = '\n'.join(output_lines) + '\n'

    if args.out:
        with open(args.out, 'w') as f:
            f.write(output)
    else:
        print(output, end='')

if __name__ == '__main__':
    main()