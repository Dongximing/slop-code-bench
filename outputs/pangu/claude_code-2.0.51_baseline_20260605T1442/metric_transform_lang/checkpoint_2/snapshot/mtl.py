#!/usr/bin/env python3
"""
MTL (Multi-Tenant Analytics Language) - File-based CLI for data transformation pipeline.
Extended version with schemas, inheritance, calculated fields, parameters, and lag/lead.
"""

import argparse
import csv
import json
import math
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import (
    Any, Callable, Dict, List, Optional, Set, Tuple, Union, get_type_hints
)


# =============================================================================
# Error Classes
# =============================================================================

class MTLException(Exception):
    """Base exception for MTL errors."""
    error_type: str
    exit_code: int = 1

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

    def stderr_message(self) -> str:
        return f"ERROR:{self.error_type}:{self.message}"


class BadDSLException(MTLException):
    error_type = "bad_dsl"
    exit_code = 1


class TypeErrorException(MTLException):
    error_type = "type_error"
    exit_code = 2


class BadIOException(MTLException):
    error_type = "bad_io"
    exit_code = 1


class RuntimeException(MTLException):
    error_type = "runtime_error"
    exit_code = 4


# =============================================================================
# Type System
# =============================================================================

class ValueType(ABC):
    """Base class for value types in MTL."""
    pass


class StringType(ValueType):
    pass


class NumberType(ValueType):
    pass


class IntType(ValueType):
    pass


class TimestampType(ValueType):
    pass


class BoolType(ValueType):
    pass


@dataclass
class MTLValue:
    """Represents a typed value in MTL."""
    value: Any
    type: ValueType

    def is_numeric(self) -> bool:
        return isinstance(self.type, NumberType)

    def is_int(self) -> bool:
        return isinstance(self.type, IntType)

    def is_string(self) -> bool:
        return isinstance(self.type, StringType)

    def is_timestamp(self) -> bool:
        return isinstance(self.type, TimestampType)

    def is_bool(self) -> bool:
        return isinstance(self.type, BoolType)

    def as_number(self) -> float:
        if self.is_timestamp():
            raise TypeError("Cannot convert timestamp to number")
        return float(self.value)

    def as_int(self) -> int:
        if self.is_int():
            return self.value
        if self.is_number():
            return int(self.value)
        raise TypeError(f"Cannot convert {type(self.type).__name__} to int")

    def as_string(self) -> str:
        return str(self.value)

    def as_timestamp(self) -> datetime:
        if self.is_timestamp():
            return self.value
        raise TypeError("Not a timestamp")

    def as_bool(self) -> bool:
        return bool(self.value)


def types_compatible(from_type: ValueType, to_type: ValueType) -> bool:
    """Check if from_type can be assigned to to_type."""
    if isinstance(from_type, type(to_type)):
        return True
    # Numeric widening: int -> NumberType (which includes float)
    if isinstance(from_type, IntType) and isinstance(to_type, NumberType):
        return True
    return False


def can_widen(from_type: ValueType, to_type: ValueType) -> bool:
    """Check if a type override is a valid widening."""
    if isinstance(from_type, type(to_type)):
        return True  # Same type is always allowed
    # int -> float widening is allowed
    if isinstance(from_type, IntType) and isinstance(to_type, NumberType):
        return True
    # float -> NumberType is same (float IS NumberType)
    if isinstance(from_type, NumberType) and isinstance(to_type, NumberType):
        return True
    return False


# =============================================================================
# Expression System
# =============================================================================

@dataclass
class Expression(ABC):
    """Base class for all MTL expressions."""
    @abstractmethod
    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        pass

    @abstractmethod
    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        pass

    @abstractmethod
    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        """Check if expression is deterministic given params."""
        return True


@dataclass
class LiteralExpr(Expression):
    value: MTLValue

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        return self.value

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        return self.value.type

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return True


@dataclass
class ColumnRefExpr(Expression):
    name: str

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        if self.name not in context:
            raise RuntimeException(f"Unknown column: {self.name}")
        return context[self.name]

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        if self.name not in schema:
            raise TypeErrorException(f"Unknown column: {self.name}")
        return schema[self.name]

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return True


@dataclass
class ParamRefExpr(Expression):
    name: str

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        # Params are passed via closure/capture, not context
        raise RuntimeException(f"Param '{self.name}' must be resolved before evaluation")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        # Type checked during pipeline setup
        return NumberType()  # placeholder

    def eval_with_params(self, params: Dict[str, MTLValue]) -> MTLValue:
        if self.name not in params:
            raise RuntimeException(f"Parameter '{self.name}' not provided")
        return params[self.name]

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.name in params


@dataclass
class BinaryOpExpr(Expression):
    left: Expression
    op: str
    right: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        lv = self.left.eval(context)
        rv = self.right.eval(context)

        if self.op in ("<", "<=", ">", ">=", "==", "!="):
            return self._compare(lv, rv)
        elif self.op == "&":
            return MTLValue(lv.as_bool() and rv.as_bool(), BoolType())
        elif self.op == "|":
            return MTLValue(lv.as_bool() or rv.as_bool(), BoolType())
        elif self.op in ("+", "-", "*", "/", "**"):
            if not (lv.is_numeric() and rv.is_numeric()):
                raise TypeErrorException(f"Numeric operation '{self.op}' requires numeric operands")
            result = self._numeric_op(lv.as_number(), rv.as_number(), self.op)
            if math.isnan(result) or math.isinf(result):
                raise RuntimeException(f"Numeric operation resulted in NaN/Inf: {self.op}")
            return MTLValue(result, NumberType())
        elif self.op == "in":
            if isinstance(lv.value, str) and isinstance(rv.value, str):
                return MTLValue(lv.value in rv.value, BoolType())
            else:
                raise TypeErrorException("'in' requires string literal on left and string column on right")
        else:
            raise RuntimeException(f"Unknown operator: {self.op}")

    def _compare(self, lv: MTLValue, rv: MTLValue) -> MTLValue:
        # Allow comparison between compatible numeric types
        if lv.is_numeric() and rv.is_numeric():
            result = self._numeric_compare(lv.as_number(), rv.as_number(), self.op)
            return MTLValue(result, BoolType())
        if lv.is_string() and rv.is_string():
            result = self._string_compare(lv.value, rv.value, self.op)
            return MTLValue(result, BoolType())
        if lv.is_timestamp() and rv.is_timestamp():
            result = self._timestamp_compare(lv.value, rv.value, self.op)
            return MTLValue(result, BoolType())
        if lv.is_bool() and rv.is_bool():
            result = self._general_compare(lv.as_bool(), rv.as_bool(), self.op)
            return MTLValue(result, BoolType())
        raise TypeErrorException(f"Cannot compare {type(lv.type).__name__} with {type(rv.type).__name__}")

    def _string_compare(self, a: str, b: str, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _numeric_compare(self, a: float, b: float, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _timestamp_compare(self, a: datetime, b: datetime, op: str) -> bool:
        if op == "<": return a < b
        if op == "<=": return a <= b
        if op == ">": return a > b
        if op == ">=": return a >= b
        if op == "==": return a == b
        if op == "!=": return a != b
        return False

    def _general_compare(self, a: Any, b: Any, op: str) -> bool:
        if op == "==": return a == b
        if op == "!=": return a != b
        raise TypeErrorException(f"Cannot use '{op}' for boolean comparison")

    def _numeric_op(self, a: float, b: float, op: str) -> float:
        if op == "+": return a + b
        if op == "-": return a - b
        if op == "*": return a * b
        if op == "/":
            if b == 0:
                raise RuntimeException("Division by zero")
            return a / b
        if op == "**": return a ** b
        raise RuntimeException(f"Unknown numeric operator: {op}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        lt = self.left.get_type(schema)
        rt = self.right.get_type(schema)

        if self.op in ("<", "<=", ">", ">=", "==", "!=", "in"):
            return BoolType()
        elif self.op in ("&", "|"):
            if not (isinstance(lt, BoolType) and isinstance(rt, BoolType)):
                raise TypeErrorException(f"Logical operation requires boolean operands")
            return BoolType()
        elif self.op in ("+", "-", "*", "/", "**"):
            if not (lt.is_numeric() and rt.is_numeric()):
                raise TypeErrorException(f"Arithmetic operation requires numeric operands")
            return NumberType()
        else:
            raise TypeErrorException(f"Unknown operator: {self.op}")

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.left.is_deterministic(params) and self.right.is_deterministic(params)


@dataclass
class UnaryOpExpr(Expression):
    op: str
    operand: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        val = self.operand.eval(context)
        if self.op == "!":
            if not val.is_bool():
                raise TypeErrorException(f"'!' requires boolean operand, got {type(val.type).__name__}")
            return MTLValue(not val.as_bool(), BoolType())
        else:
            raise RuntimeException(f"Unknown unary operator: {self.op}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        if self.op == "!":
            t = self.operand.get_type(schema)
            if not isinstance(t, BoolType):
                raise TypeErrorException(f"'!' requires boolean operand")
            return BoolType()
        raise TypeErrorException(f"Unknown unary operator: {self.op}")

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.operand.is_deterministic(params)


@dataclass
class TransformExpr(Expression):
    """Represents a transform function call like len(col), day(col), etc."""
    func: str
    arg: Expression
    digits: Optional[Expression] = None

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        arg_val = self.arg.eval(context)

        if self.func == "len":
            if not arg_val.is_string():
                raise TypeErrorException("len() requires string argument")
            return MTLValue(len(arg_val.value), NumberType())

        elif self.func in ("day", "month", "year"):
            if not arg_val.is_timestamp():
                raise TypeErrorException(f"{self.func}() requires timestamp argument")
            ts = arg_val.as_timestamp()
            if self.func == "day":
                return MTLValue(ts.day, IntType())
            elif self.func == "month":
                return MTLValue(ts.month, IntType())
            else:
                return MTLValue(ts.year, IntType())

        elif self.func == "round":
            if not arg_val.is_numeric():
                raise TypeErrorException("round() requires numeric argument")
            digits_val = 0
            if self.digits is not None:
                d = self.digits.eval(context)
                if not d.is_numeric():
                    raise TypeErrorException("round() digits must be integer")
                digits_val = int(d.as_number())
                if digits_val != d.as_number():
                    raise TypeErrorException("round() digits must be integer")
            val = arg_val.as_number()
            return MTLValue(round_half_away_from_zero(val, digits_val), NumberType())

        elif self.func == "starts_with":
            if not (isinstance(arg_val.value, str)):
                raise TypeErrorException("starts_with() requires string argument")
            # Second arg is passed differently - needs special handling
            raise RuntimeException("starts_with() requires two arguments")

        elif self.func == "ends_with":
            if not (isinstance(arg_val.value, str)):
                raise TypeErrorException("ends_with() requires string argument")
            raise RuntimeException("ends_with() requires two arguments")

        elif self.func == "contains":
            if not (isinstance(arg_val.value, str)):
                raise TypeErrorException("contains() requires string argument")
            raise RuntimeException("contains() requires two arguments")

        else:
            raise RuntimeException(f"Unknown transform function: {self.func}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        arg_type = self.arg.get_type(schema)

        if self.func == "len":
            if not isinstance(arg_type, StringType):
                raise TypeErrorException("len() requires string argument")
            return IntType()

        elif self.func in ("day", "month", "year"):
            if not isinstance(arg_type, TimestampType):
                raise TypeErrorException(f"{self.func}() requires timestamp argument")
            return IntType()

        elif self.func == "round":
            if not isinstance(arg_type, NumberType):
                raise TypeErrorException("round() requires numeric argument")
            if self.digits is not None:
                digits_type = self.digits.get_type(schema)
                if not digits_type.is_numeric():
                    raise TypeErrorException("round() digits must be integer")
            return NumberType()

        else:
            raise TypeErrorException(f"Unknown transform function: {self.func}")

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.arg.is_deterministic(params) and (self.digits.is_deterministic(params) if self.digits else True)


@dataclass
class LagLeadExpr(Expression):
    """Represents lag/lead function for post-aggregation."""
    func: str  # 'lag' or 'lead'
    expr: Expression
    k: int
    is_post_agg: bool = False

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        if not self.is_post_agg:
            raise RuntimeException(f"{self.func}() can only be used in post_agg stage")
        if "_laglead_index" not in context:
            raise RuntimeException(f"{self.func}() requires sorted results")
        idx = context["_laglead_index"].as_int()
        results = context["_laglead_results"].value

        target_idx = idx + (self.k if self.func == "lead" else -self.k)
        if target_idx < 0 or target_idx >= len(results):
            return MTLValue(None, NumberType())  # Null for out of bounds

        result_val = results[target_idx]
        expr_name = self.expr.name if hasattr(self.expr, 'name') else None
        if expr_name and expr_name in result_val:
            val = result_val[expr_name]
            if isinstance(val, (int, float)):
                return MTLValue(val, NumberType())
            return MTLValue(val, StringType() if isinstance(val, str) else NumberType())
        return MTLValue(None, NumberType())

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        return self.expr.get_type(schema)

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return True


@dataclass
class CoalesceExpr(Expression):
    """Represents coalesce(a, b) function."""
    left: Expression
    right: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        lv = self.left.eval(context)
        rv = self.right.eval(context)

        # Check if left is null/None
        if lv.value is not None:
            return lv
        return rv

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        lt = self.left.get_type(schema)
        rt = self.right.get_type(schema)
        if isinstance(lt, type(rt)):
            return lt
        raise TypeErrorException(f"coalesce() types must match: {type(lt).__name__} vs {type(rt).__name__}")

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.left.is_deterministic(params) and self.right.is_deterministic(params)


@dataclass
class CastExpr(Expression):
    """Represents cast(expr as type)."""
    expr: Expression
    target_type: ValueType

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        val = self.expr.eval(context)
        return self._cast_value(val, self.target_type)

    def _cast_value(self, val: MTLValue, target: ValueType) -> MTLValue:
        if isinstance(target, type(val.type)):
            return val
        if isinstance(target, NumberType) and val.is_numeric():
            return MTLValue(val.as_number(), NumberType())
        if isinstance(target, IntType) and val.is_numeric():
            return MTLValue(int(val.as_number()), IntType())
        if isinstance(target, StringType):
            return MTLValue(val.as_string(), StringType())
        if isinstance(target, BoolType):
            return MTLValue(val.as_bool(), BoolType())
        if isinstance(target, TimestampType) and val.is_string():
            # Try to parse as timestamp
            raise RuntimeException(f"Cannot cast string to timestamp: {val.value}")
        raise TypeErrorException(f"Cannot cast {type(val.type).__name__} to {type(target).__name__}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        # Just verify expr can be cast to target type
        src_type = self.expr.get_type(schema)
        if not self._can_cast(src_type, self.target_type):
            raise TypeErrorException(f"Cannot cast {type(src_type).__name__} to {type(self.target_type).__name__}")
        return self.target_type

    def _can_cast(self, src: ValueType, target: ValueType) -> bool:
        if isinstance(target, type(src)):
            return True
        if isinstance(target, NumberType) and src.is_numeric():
            return True
        if isinstance(target, IntType) and src.is_numeric():
            return True
        if isinstance(target, StringType):
            return True
        if isinstance(target, BoolType):
            return True
        return False

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.expr.is_deterministic(params)


@dataclass
class BinaryStringFuncExpr(Expression):
    """Represents binary string functions: starts_with, ends_with, contains."""
    func: str
    left: Expression
    right: Expression

    def eval(self, context: Dict[str, MTLValue]) -> MTLValue:
        lv = self.left.eval(context)
        rv = self.right.eval(context)

        if not isinstance(lv.value, str):
            raise TypeErrorException(f"{self.func}() requires string as first argument")
        if not isinstance(rv.value, str):
            raise TypeErrorException(f"{self.func}() requires string as second argument")

        if self.func == "starts_with":
            return MTLValue(lv.value.startswith(rv.value), BoolType())
        elif self.func == "ends_with":
            return MTLValue(lv.value.endswith(rv.value), BoolType())
        elif self.func == "contains":
            return MTLValue(rv.value in lv.value, BoolType())
        else:
            raise RuntimeException(f"Unknown string function: {self.func}")

    def get_type(self, schema: Dict[str, ValueType]) -> ValueType:
        lt = self.left.get_type(schema)
        rt = self.right.get_type(schema)
        if not isinstance(lt, StringType):
            raise TypeErrorException(f"{self.func}() requires string as first argument")
        if not isinstance(rt, StringType):
            raise TypeErrorException(f"{self.func}() requires string as second argument")
        return BoolType()

    def is_deterministic(self, params: Dict[str, Any]) -> bool:
        return self.left.is_deterministic(params) and self.right.is_deterministic(params)


def round_half_away_from_zero(value: float, decimals: int) -> float:
    """Round half away from zero (like Python 3's round)."""
    if decimals == 0:
        return round(value)
    multiplier = 10 ** decimals
    return round(value * multiplier) / multiplier


# =============================================================================
# Schema System
# =============================================================================

@dataclass
class SchemaField:
    """Represents a field in a schema."""
    name: str
    type: ValueType
    expr: Optional[Expression] = None  # Calculated field expression
    is_calculated: bool = False


@dataclass
class Schema:
    """Represents a named schema with fields and inheritance."""
    name: str
    base_name: Optional[str]  # None if no base
    fields: Dict[str, SchemaField] = field(default_factory=dict)
    calculated_fields: Dict[str, SchemaField] = field(default_factory=dict)

    def get_field(self, name: str) -> Optional[SchemaField]:
        return self.fields.get(name)

    def get_type(self, name: str) -> Optional[ValueType]:
        field = self.fields.get(name)
        return field.type if field else None


class SchemaRegistry:
    """Manages all schemas and resolves inheritance."""

    def __init__(self):
        self.schemas: Dict[str, Schema] = {}

    def add_schema(self, schema: Schema):
        """Add a schema to the registry."""
        if schema.name in self.schemas:
            raise BadDSLException(f"Duplicate schema name: {schema.name}")
        self.schemas[schema.name] = schema

    def resolve_inheritance(self, schema_name: str) -> Schema:
        """Resolve a schema with full inheritance."""
        visited = set()
        return self._resolve_inheritance_impl(schema_name, visited)

    def _resolve_inheritance_impl(self, schema_name: str, visited: Set[str]) -> Schema:
        """Recursively resolve inheritance with cycle detection."""
        if schema_name in visited:
            raise BadDSLException(f"Circular inheritance detected involving: {schema_name}")
        visited.add(schema_name)

        if schema_name not in self.schemas:
            raise BadDSLException(f"Unknown schema: {schema_name}")

        schema = self.schemas[schema_name]

        # Create resolved schema by copying and inheriting
        resolved = Schema(schema.name, schema.base_name)

        # Inherit from base
        if schema.base_name:
            base_resolved = self._resolve_inheritance_impl(schema.base_name, visited.copy())
            # Copy base fields
            for name, field in base_resolved.fields.items():
                resolved.fields[name] = field

        # Add/override with child fields
        for name, field in schema.fields.items():
            if name in resolved.fields:
                # Check type override compatibility
                base_field = resolved.fields[name]
                if not can_widen(base_field.type, field.type):
                    raise TypeErrorException(
                        f"Illegal type override for field '{name}': "
                        f"{type(base_field.type).__name__} -> {type(field.type).__name__}"
                    )
            resolved.fields[name] = field

        return resolved


# =============================================================================
# Pipeline System
# =============================================================================

@dataclass
class PipelineParam:
    """Represents a pipeline parameter."""
    name: str
    type: ValueType
    default: Optional[MTLValue] = None
    provided: bool = False
    value: Optional[MTLValue] = None


@dataclass
class CalcStatement:
    """Represents a calc statement in pipeline."""
    name: str
    expr: Expression
    type: ValueType
    stage: str  # 'pre' or 'post_agg'


@dataclass
class Pipeline:
    """Represents a complete pipeline with schema, params, and statements."""
    name: str
    schema_name: str
    params: Dict[str, PipelineParam] = field(default_factory=dict)
    filter_expr: Optional[Expression] = None
    apply_stmts: List[Tuple[Expression, str]] = field(default_factory=list)
    group_by_cols: List[str] = field(default_factory=list)
    window_duration: Optional[str] = None
    aggregate_spec: Optional[Dict] = None
    calc_stmts: List[CalcStatement] = field(default_factory=list)


# =============================================================================
# DSL Parser (Extended)
# =============================================================================

class TokenType(Enum):
    IDENTIFIER = 1
    STRING_LITERAL = 2
    NUMBER = 3
    BOOL = 4
    LPAREN = 5
    RPAREN = 6
    LBRACE = 7
    RBRACE = 8
    LBRACKET = 9
    RBRACKET = 10
    COMMA = 7
    EQ = 8  # "as" or assignment
    COLON = 11
    AT = 12
    COMMENT = 9
    WHITESPACE = 10
    UNKNOWN = 11
    STAR = 12
    SCHEMA = 13
    PIPELINE = 14
    EXTENDS = 15
    USING = 16
    CALC = 17
    PARAMS = 18
    STAGE = 19


@dataclass
class Token:
    type: TokenType
    value: str
    line: int
    col: int


class Lexer:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.line = 1
        self.col = 1
        self.tokens: List[Token] = []

    def tokenize(self) -> List[Token]:
        keywords = {
            "true": TokenType.BOOL, "false": TokenType.BOOL,
            "as": TokenType.EQ, "schema": TokenType.SCHEMA,
            "pipeline": TokenType.PIPELINE, "extends": TokenType.EXTENDS,
            "using": TokenType.USING, "calc": TokenType.CALC,
            "params": TokenType.PARAMS, "stage": TokenType.STAGE,
            "string": TokenType.IDENTIFIER, "int": TokenType.IDENTIFIER,
            "float": TokenType.IDENTIFIER, "bool": TokenType.IDENTIFIER,
            "timestamp": TokenType.IDENTIFIER, "pre": TokenType.IDENTIFIER,
            "post_agg": TokenType.IDENTIFIER,
        }
        operators = {
            "(": TokenType.LPAREN,
            ")": TokenType.RPAREN,
            "{": TokenType.LBRACE,
            "}": TokenType.RBRACE,
            "[": TokenType.LBRACKET,
            "]": TokenType.RBRACKET,
            ",": TokenType.COMMA,
        }

        while self.pos < len(self.text):
            char = self.text[self.pos]

            # Skip whitespace
            if char.isspace():
                if char == "\n":
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.pos += 1
                continue

            # Comments
            if char == "#":
                while self.pos < len(self.text) and self.text[self.pos] != "\n":
                    self.pos += 1
                continue

            # Identifiers and keywords
            if char.isalpha() or char == "_":
                start = self.pos
                while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == "_"):
                    self.pos += 1
                value = self.text[start:self.pos]
                token_type = keywords.get(value.lower(), TokenType.IDENTIFIER)
                self.tokens.append(Token(token_type, value, self.line, self.col - len(value)))
                self.col += len(value)
                continue

            # Numbers
            if char.isdigit() or (char == "." and self.pos + 1 < len(self.text) and self.text[self.pos + 1].isdigit()):
                start = self.pos
                has_dot = char == "."
                if not has_dot:
                    self.pos += 1
                while self.pos < len(self.text):
                    if self.text[self.pos].isdigit():
                        self.pos += 1
                    elif self.text[self.pos] == "." and not has_dot:
                        has_dot = True
                        self.pos += 1
                    else:
                        break
                value = self.text[start:self.pos]
                self.tokens.append(Token(TokenType.NUMBER, value, self.line, self.col))
                self.col += len(value)
                continue

            # String literals (single quotes)
            if char == "'":
                start = self.pos
                self.pos += 1
                while self.pos < len(self.text) and self.text[self.pos] != "'":
                    if self.text[self.pos] == "\\":
                        self.pos += 2
                    else:
                        self.pos += 1
                if self.pos >= len(self.text):
                    raise BadDSLException(f"Unterminated string literal at line {self.line}")
                self.pos += 1
                value = self.text[start + 1:self.pos - 1]
                self.tokens.append(Token(TokenType.STRING_LITERAL, value, self.line, self.col))
                self.col += len(value) + 2
                continue

            # Multi-character operators
            if char in ("<", ">", "="):
                if self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "=":
                    value = char + "="
                    self.tokens.append(Token(TokenType.IDENTIFIER, value, self.line, self.col))
                    self.pos += 2
                    self.col += 2
                    continue
                elif char == "!" and self.pos + 1 < len(self.text) and self.text[self.pos + 1] == "=":
                    value = "!="
                    self.tokens.append(Token(TokenType.IDENTIFIER, value, self.line, self.col))
                    self.pos += 2
                    self.col += 2
                    continue

            # Colon and At
            if char == ":":
                self.tokens.append(Token(TokenType.COLON, ":", self.line, self.col))
                self.pos += 1
                self.col += 1
                continue
            if char == "@":
                self.tokens.append(Token(TokenType.AT, "@", self.line, self.col))
                self.pos += 1
                self.col += 1
                continue

            # Single char tokens
            if char in operators:
                self.tokens.append(Token(operators[char], char, self.line, self.col))
                self.pos += 1
                self.col += 1
                continue

            # Star
            if char == "*":
                self.tokens.append(Token(TokenType.STAR, "*", self.line, self.col))
                self.pos += 1
                self.col += 1
                continue

            # Unknown character
            self.tokens.append(Token(TokenType.UNKNOWN, char, self.line, self.col))
            self.pos += 1
            self.col += 1

        return self.tokens


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0
        self.schemas: Dict[str, Schema] = {}
        self.pipeline: Optional[Pipeline] = None

    def peek(self) -> Optional[Token]:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type: Optional[TokenType] = None, expected_value: Optional[str] = None) -> Token:
        token = self.peek()
        if token is None:
            raise BadDSLException(f"Unexpected end of input")
        if expected_type is not None and token.type != expected_type:
            raise BadDSLException(f"Expected {expected_type}, got {token.type} at line {token.line}")
        if expected_value is not None and token.value != expected_value:
            raise BadDSLException(f"Expected '{expected_value}', got '{token.value}' at line {token.line}")
        self.pos += 1
        return token

    def parse(self) -> Tuple[Dict[str, Schema], Pipeline]:
        """Parse the entire DSL, returning schemas and pipeline."""
        # Parse schema blocks first
        while self.peek() and self.peek().type == TokenType.SCHEMA:
            self.parse_schema_block()

        # Check if we have a pipeline block
        if self.peek() and self.peek().type == TokenType.PIPELINE:
            self.parse_pipeline_block()
        else:
            # Backward compatibility: treat as implicit pipeline with auto-generated schema
            self.parse_backward_compatible_pipeline()

        # Ensure end of input
        if self.peek():
            raise BadDSLException(f"Unexpected token after pipeline: {self.peek().value}")

        return self.schemas, self.pipeline

    def parse_backward_compatible_pipeline(self):
        """Parse backward compatible format (no schema/pipeline keywords)."""
        # Create an implicit schema from the CSV data
        # For now, use a basic schema
        from copy import copy

        # Parse statements directly
        pipeline = Pipeline("ImplicitPipeline", "__implicit__")

        # Parse statements until end
        while self.peek():
            stmt = self.parse_pipeline_statement()
            self._add_pipeline_statement(pipeline, stmt)

        # Create an implicit schema that captures all fields used
        implicit_schema = Schema("__implicit__", None)

        # Add fields based on usage
        # This is a simplified approach - in practice, we'd infer from CSV
        # For now, just add common fields
        pipeline.schema_name = "__implicit__"
        self.schemas["__implicit__"] = implicit_schema
        self.pipeline = pipeline

    def parse_schema_block(self):
        """Parse a schema block."""
        self.consume(TokenType.SCHEMA, "schema")
        name_token = self.consume(TokenType.IDENTIFIER)
        name = name_token.value

        base_name = None
        if self.peek() and self.peek().type == TokenType.EXTENDS:
            self.consume(TokenType.EXTENDS)
            base_token = self.consume(TokenType.IDENTIFIER)
            base_name = base_token.value

        self.consume(TokenType.LBRACE)

        schema_obj = Schema(name, base_name)

        while self.peek() and self.peek().type != TokenType.RBRACE:
            field = self.parse_schema_field()
            if field.name in schema_obj.fields:
                raise BadDSLException(f"Duplicate field '{field.name}' in schema '{name}'")
            schema_obj.fields[field.name] = field

        self.consume(TokenType.RBRACE)

        if name in self.schemas:
            raise BadDSLException(f"Duplicate schema name: {name}")
        self.schemas[name] = schema_obj

    def parse_schema_field(self) -> SchemaField:
        """Parse a field declaration in a schema."""
        name_token = self.consume(TokenType.IDENTIFIER)
        name = name_token.value

        self.consume(TokenType.COLON)

        type_token = self.consume(TokenType.IDENTIFIER)
        type_str = type_token.value.lower()

        if type_str == "string":
            field_type = StringType()
        elif type_str == "int":
            field_type = IntType()
        elif type_str == "float":
            field_type = NumberType()
        elif type_str == "bool":
            field_type = BoolType()
        elif type_str == "timestamp":
            field_type = TimestampType()
        else:
            raise BadDSLException(f"Unknown type: {type_str}")

        expr = None
        is_calculated = False

        if self.peek() and self.peek().type == TokenType.EQ:
            self.consume(TokenType.EQ)
            expr = self.parse_expression()
            is_calculated = True

        return SchemaField(name, field_type, expr, is_calculated)

    def parse_pipeline_block(self):
        """Parse a pipeline block."""
        self.consume(TokenType.PIPELINE, "pipeline")
        name_token = self.consume(TokenType.IDENTIFIER)
        name = name_token.value

        self.consume(TokenType.USING, "using")
        schema_token = self.consume(TokenType.IDENTIFIER)
        schema_name = schema_token.value

        self.consume(TokenType.LBRACE)

        pipeline = Pipeline(name, schema_name)

        # Parse params block if present
        if self.peek() and self.peek().type == TokenType.PARAMS:
            self.parse_params_block(pipeline)

        # Parse statements
        while self.peek() and self.peek().type != TokenType.RBRACE:
            stmt = self.parse_pipeline_statement()
            self._add_pipeline_statement(pipeline, stmt)

        self.consume(TokenType.RBRACE)

        if schema_name not in self.schemas:
            raise BadDSLException(f"Unknown schema referenced by pipeline: {schema_name}")

        self.pipeline = pipeline

    def parse_params_block(self, pipeline: Pipeline):
        """Parse the params block."""
        self.consume(TokenType.PARAMS, "params")
        self.consume(TokenType.LBRACE)

        while self.peek() and self.peek().type != TokenType.RBRACE:
            name_token = self.consume(TokenType.IDENTIFIER)
            name = name_token.value

            param_type = StringType()  # Default type
            default = None

            if self.peek() and self.peek().type == TokenType.COLON:
                self.consume(TokenType.COLON)
                type_token = self.consume(TokenType.IDENTIFIER)
                type_str = type_token.value.lower()
                if type_str == "string":
                    param_type = StringType()
                elif type_str == "int":
                    param_type = IntType()
                elif type_str == "float":
                    param_type = NumberType()
                elif type_str == "bool":
                    param_type = BoolType()
                elif type_str == "timestamp":
                    param_type = TimestampType()
                else:
                    raise BadDSLException(f"Unknown param type: {type_str}")

            if self.peek() and self.peek().type == TokenType.EQ:
                self.consume(TokenType.EQ)
                default = self.parse_literal()
                # Type check default
                if not types_compatible(default.type, param_type):
                    raise TypeErrorException(
                        f"Default value type {type(default.type).__name__} incompatible with param type {type(param_type).__name__}"
                    )

            pipeline.params[name] = PipelineParam(name, param_type, default)

        self.consume(TokenType.RBRACE)

    def parse_literal(self) -> MTLValue:
        """Parse a literal value."""
        token = self.peek()
        if token.type == TokenType.STRING_LITERAL:
            self.consume()
            return MTLValue(token.value, StringType())
        if token.type == TokenType.NUMBER:
            self.consume()
            val = float(token.value)
            if val == int(val):
                return MTLValue(int(val), IntType())
            return MTLValue(val, NumberType())
        if token.type == TokenType.BOOL:
            self.consume()
            return MTLValue(token.value.lower() == "true", BoolType())
        raise BadDSLException(f"Expected literal, got {token.value}")

    def parse_pipeline_statement(self) -> Dict:
        """Parse a statement within a pipeline block."""
        token = self.peek()
        if token.type == TokenType.CALC:
            return self.parse_calc()
        elif token.type == TokenType.IDENTIFIER:
            if token.value == "filter":
                return self.parse_filter()
            elif token.value == "apply":
                return self.parse_apply()
            elif token.value == "group_by":
                return self.parse_group_by()
            elif token.value == "window":
                return self.parse_window()
            elif token.value == "aggregate":
                return self.parse_aggregate()

        raise BadDSLException(f"Unknown pipeline statement: {token.value}")

    def parse_calc(self) -> Dict:
        """Parse a calc statement."""
        self.consume(TokenType.CALC, "calc")
        name_token = self.consume(TokenType.IDENTIFIER)
        name = name_token.value

        self.consume(TokenType.EQ)
        expr = self.parse_expression()

        self.consume(TokenType.COLON)
        type_token = self.consume(TokenType.IDENTIFIER)
        type_str = type_token.value.lower()

        if type_str == "string":
            calc_type = StringType()
        elif type_str == "int":
            calc_type = IntType()
        elif type_str == "float":
            calc_type = NumberType()
        elif type_str == "bool":
            calc_type = BoolType()
        elif type_str == "timestamp":
            calc_type = TimestampType()
        else:
            raise BadDSLException(f"Unknown type: {type_str}")

        self.consume(TokenType.AT)
        self.consume(TokenType.STAGE, "stage")
        self.consume(TokenType.LPAREN)
        stage_token = self.consume(TokenType.IDENTIFIER)
        stage = stage_token.value.lower()
        if stage not in ("pre", "post_agg"):
            raise BadDSLException(f"Invalid stage: {stage}")
        self.consume(TokenType.RPAREN)

        return {
            "type": "calc",
            "name": name,
            "expr": expr,
            "type": calc_type,
            "stage": stage
        }

    def _add_pipeline_statement(self, pipeline: Pipeline, stmt: Dict):
        """Add a parsed statement to the pipeline."""
        stmt_type = stmt["type"]

        if stmt_type == "filter":
            if pipeline.filter_expr:
                raise BadDSLException("Multiple filter statements not allowed")
            pipeline.filter_expr = stmt["expr"]
        elif stmt_type == "apply":
            pipeline.apply_stmts.append((stmt["expr"], stmt["new_col"]))
        elif stmt_type == "group_by":
            if pipeline.group_by_cols:
                raise BadDSLException("Multiple group_by statements not allowed")
            pipeline.group_by_cols = stmt["cols"]
        elif stmt_type == "window":
            if pipeline.window_duration:
                raise BadDSLException("Multiple window statements not allowed")
            pipeline.window_duration = stmt["duration"]
        elif stmt_type == "aggregate":
            if pipeline.aggregate_spec:
                raise BadDSLException("Multiple aggregate statements not allowed")
            pipeline.aggregate_spec = stmt
        elif stmt_type == "calc":
            pipeline.calc_stmts.append(CalcStatement(
                name=stmt["name"],
                expr=stmt["expr"],
                type=stmt["type"],
                stage=stmt["stage"]
            ))

    def parse_filter(self) -> Dict:
        self.consume(TokenType.IDENTIFIER, "filter")
        self.consume(TokenType.LPAREN)
        expr = self.parse_boolean_expr()
        self.consume(TokenType.RPAREN)
        return {"type": "filter", "expr": expr}

    def parse_apply(self) -> Dict:
        self.consume(TokenType.IDENTIFIER, "apply")
        self.consume(TokenType.LPAREN)
        expr = self.parse_expression()
        self.consume(TokenType.COMMA)
        new_col = self.consume(TokenType.IDENTIFIER)
        self.consume(TokenType.RPAREN)
        return {"type": "apply", "expr": expr, "new_col": new_col.value}

    def parse_group_by(self) -> Dict:
        self.consume(TokenType.IDENTIFIER, "group_by")
        self.consume(TokenType.LPAREN)
        cols = []

        if self.peek().type == TokenType.LBRACKET:
            self.consume(TokenType.LBRACKET)
            cols.append(self.consume(TokenType.IDENTIFIER).value)
            while self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA)
                cols.append(self.consume(TokenType.IDENTIFIER).value)
            self.consume(TokenType.RBRACKET)
        else:
            cols.append(self.consume(TokenType.IDENTIFIER).value)

        self.consume(TokenType.RPAREN)
        return {"type": "group_by", "cols": cols}

    def parse_window(self) -> Dict:
        self.consume(TokenType.IDENTIFIER, "window")
        self.consume(TokenType.LPAREN)
        duration = self.consume(TokenType.IDENTIFIER)
        self.consume(TokenType.RPAREN)
        return {"type": "window", "duration": duration.value}

    def parse_aggregate(self) -> Dict:
        self.consume(TokenType.IDENTIFIER, "aggregate")
        aggs = []

        while True:
            func = self.consume(TokenType.IDENTIFIER)
            self.consume(TokenType.LPAREN)

            peek = self.peek()
            if peek is None or peek.type != TokenType.STAR:
                col = self.consume(TokenType.IDENTIFIER)
                arg = ColumnRefExpr(col.value)
            else:
                self.consume(TokenType.STAR)
                arg = LiteralExpr(MTLValue("*", StringType()))

            self.consume(TokenType.RPAREN)
            self.consume(TokenType.EQ, "as")
            alias = self.consume(TokenType.IDENTIFIER)

            aggs.append({"func": func.value, "arg": arg, "alias": alias.value})

            peek = self.peek()
            if peek is None or peek.type != TokenType.COMMA:
                break
            self.consume(TokenType.COMMA)

        return {"type": "aggregate", "aggs": aggs}

    def parse_boolean_expr(self) -> Expression:
        return self.parse_or_expr()

    def parse_or_expr(self) -> Expression:
        left = self.parse_and_expr()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "|":
            self.consume(TokenType.IDENTIFIER, "|")
            right = self.parse_and_expr()
            left = BinaryOpExpr(left, "|", right)
        return left

    def parse_and_expr(self) -> Expression:
        left = self.parse_not_expr()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "&":
            self.consume(TokenType.IDENTIFIER, "&")
            right = self.parse_not_expr()
            left = BinaryOpExpr(left, "&", right)
        return left

    def parse_not_expr(self) -> Expression:
        if self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value == "!":
            self.consume(TokenType.IDENTIFIER, "!")
            operand = self.parse_not_expr()
            return UnaryOpExpr("!", operand)
        return self.parse_comparison()

    def parse_comparison(self) -> Expression:
        left = self.parse_additive()

        if self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op_val = self.peek().value
            if op_val in ("<", ">", "==", "!=", "<=", ">=", "in"):
                op = self.consume(TokenType.IDENTIFIER).value
                right = self.parse_additive()
                return BinaryOpExpr(left, op, right)

        return left

    def parse_additive(self) -> Expression:
        left = self.parse_term()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op = self.peek().value
            if op in ("+", "-"):
                self.consume(TokenType.IDENTIFIER)
                right = self.parse_term()
                left = BinaryOpExpr(left, op, right)
            else:
                break
        return left

    def parse_term(self) -> Expression:
        left = self.parse_factor()
        while self.peek() and self.peek().type == TokenType.IDENTIFIER:
            op = self.peek().value
            if op in ("*", "/", "**"):
                self.consume(TokenType.IDENTIFIER)
                right = self.parse_factor()
                left = BinaryOpExpr(left, op, right)
            else:
                break
        return left

    def parse_factor(self) -> Expression:
        if self.peek() is None:
            raise BadDSLException("Unexpected end of input")

        token = self.peek()

        # Parenthesized expression
        if token.type == TokenType.LPAREN:
            self.consume(TokenType.LPAREN)
            expr = self.parse_boolean_expr()
            self.consume(TokenType.RPAREN)
            return expr

        # Function call: func(arg) or func(arg, arg)
        if token.type == TokenType.IDENTIFIER and self.peek_plus(1) and self.peek_plus(1).type == TokenType.LPAREN:
            func_name = self.consume(TokenType.IDENTIFIER).value
            self.consume(TokenType.LPAREN)

            # Check for special functions
            if func_name in ("lag", "lead"):
                arg = self.parse_expression()
                self.consume(TokenType.COMMA)
                k_expr = self.parse_expression()
                self.consume(TokenType.RPAREN)
                # Validate k is int
                if not (isinstance(k_expr, LiteralExpr) and isinstance(k_expr.type, IntType)):
                    raise TypeErrorException(f"{func_name}() requires integer k")
                return LagLeadExpr(func_name, arg, k_expr.value.value)

            if func_name in ("starts_with", "ends_with", "contains"):
                arg1 = self.parse_expression()
                self.consume(TokenType.COMMA)
                arg2 = self.parse_expression()
                self.consume(TokenType.RPAREN)
                return BinaryStringFuncExpr(func_name, arg1, arg2)

            if func_name == "coalesce":
                arg1 = self.parse_expression()
                self.consume(TokenType.COMMA)
                arg2 = self.parse_expression()
                self.consume(TokenType.RPAREN)
                return CoalesceExpr(arg1, arg2)

            if func_name == "cast":
                arg = self.parse_expression()
                self.consume(TokenType.COMMA)
                type_token = self.consume(TokenType.IDENTIFIER)
                type_str = type_token.value.lower()
                if type_str == "string":
                    target_type = StringType()
                elif type_str == "int":
                    target_type = IntType()
                elif type_str == "float":
                    target_type = NumberType()
                elif type_str == "bool":
                    target_type = BoolType()
                elif type_str == "timestamp":
                    target_type = TimestampType()
                else:
                    raise BadDSLException(f"Unknown type in cast: {type_str}")
                self.consume(TokenType.RBRACKET if self.peek().type == TokenType.RBRACKET else TokenType.RPAREN)
                # Check for AS keyword if present
                if self.peek() and self.peek().type == TokenType.IDENTIFIER and self.peek().value.lower() == "as":
                    self.consume(TokenType.IDENTIFIER)  # consume 'as'
                return CastExpr(arg, target_type)

            # Regular transform function
            arg = self.parse_expression()
            digits = None
            if self.peek().type == TokenType.COMMA:
                self.consume(TokenType.COMMA)
                digits = self.parse_expression()
            self.consume(TokenType.RPAREN)
            return TransformExpr(func_name, arg, digits)

        # param("name")
        if token.type == TokenType.IDENTIFIER and token.value == "param":
            self.consume(TokenType.IDENTIFIER)  # consume 'param'
            self.consume(TokenType.LPAREN)
            name_token = self.consume(TokenType.STRING_LITERAL)
            name = name_token.value
            self.consume(TokenType.RPAREN)
            return ParamRefExpr(name)

        # Literal values
        if token.type == TokenType.STRING_LITERAL:
            self.consume()
            return LiteralExpr(MTLValue(token.value, StringType()))

        if token.type == TokenType.NUMBER:
            self.consume()
            val = float(token.value)
            if val == int(val):
                return LiteralExpr(MTLValue(int(val), IntType()))
            return LiteralExpr(MTLValue(val, NumberType()))

        if token.type == TokenType.BOOL:
            self.consume()
            return LiteralExpr(MTLValue(token.value.lower() == "true", BoolType()))

        # Identifier (column reference)
        if token.type == TokenType.IDENTIFIER:
            name = self.consume(TokenType.IDENTIFIER).value
            return ColumnRefExpr(name)

        raise BadDSLException(f"Unexpected token: {token.value} at line {token.line}")

    def peek_plus(self, offset: int) -> Optional[Token]:
        if self.pos + offset < len(self.tokens):
            return self.tokens[self.pos + offset]
        return None

    def parse_expression(self) -> Expression:
        """Parse a general expression."""
        return self.parse_boolean_expr()


def parse_dsl(dsl_text: str) -> Tuple[Dict[str, Schema], Pipeline]:
    """Parse DSL text into schemas and pipeline."""
    lexer = Lexer(dsl_text)
    tokens = lexer.tokenize()

    # Filter out comments and whitespace for parsing
    clean_tokens = [t for t in tokens if t.type not in (TokenType.COMMENT, TokenType.WHITESPACE)]

    parser = Parser(clean_tokens)
    return parser.parse()


# =============================================================================
# Data Processing Engine (Extended)
# =============================================================================

@dataclass
class Row:
    """A row of data with typed values."""
    values: Dict[str, MTLValue]
    original_index: int = 0


class MTLEngine:
    def __init__(self, csv_path: str, dsl_path: str, tz: str = "UTC",
                 timestamp_format: Optional[str] = None,
                 cli_params: Optional[Dict[str, str]] = None):
        self.tz = tz
        self.timestamp_format = timestamp_format
        self.resolved_schema: Optional[Schema] = None
        self.rows: List[Row] = []
        self.params: Dict[str, MTLValue] = {}
        self.pipeline: Optional[Pipeline] = None

        # Parse DSL
        if dsl_path == "-":
            dsl_text = sys.stdin.read()
        else:
            try:
                with open(dsl_path, 'r') as f:
                    dsl_text = f.read()
            except IOError as e:
                raise BadIOException(f"Cannot read DSL file: {e}")

        schemas, pipeline = parse_dsl(dsl_text)
        self.pipeline = pipeline

        # Resolve schema inheritance
        schema_registry = SchemaRegistry()
        for schema in schemas.values():
            schema_registry.add_schema(schema)

        if pipeline.schema_name not in schemas:
            raise BadDSLException(f"Schema '{pipeline.schema_name}' not found")

        # Resolve with full inheritance
        self.resolved_schema = schema_registry.resolve_inheritance(pipeline.schema_name)

        # Process parameters
        self._process_parameters(pipeline, cli_params or {})

        # Validate pipeline uses resolved schema
        self._validate_pipeline()

        # Load CSV
        self._load_csv(csv_path)

    def _process_parameters(self, pipeline: Pipeline, cli_params: Dict[str, str]):
        """Process and validate pipeline parameters."""
        # First, collect provided params from CLI
        provided_from_cli: Dict[str, MTLValue] = {}

        for name, value_str in cli_params.items():
            if name not in pipeline.params:
                raise BadDSLException(f"Unknown parameter: {name}")

            param = pipeline.params[name]
            try:
                if isinstance(param.type, StringType):
                    provided_from_cli[name] = MTLValue(value_str, StringType())
                elif isinstance(param.type, IntType):
                    provided_from_cli[name] = MTLValue(int(value_str), IntType())
                elif isinstance(param.type, NumberType):
                    provided_from_cli[name] = MTLValue(float(value_str), NumberType())
                elif isinstance(param.type, BoolType):
                    provided_from_cli[name] = MTLValue(value_str.lower() in ("true", "1", "yes"), BoolType())
                else:
                    raise TypeErrorException(f"Cannot parse value for parameter {name}")
            except ValueError:
                raise TypeErrorException(f"Cannot parse '{value_str}' as {type(param.type).__name__} for parameter {name}")

        # Check for required params without defaults
        for name, param in pipeline.params.items():
            if name in provided_from_cli:
                self.params[name] = provided_from_cli[name]
            elif param.default is not None:
                self.params[name] = param.default
            else:
                raise BadDSLException(f"Parameter '{name}' must be provided via --param")

    def _validate_pipeline(self):
        """Validate pipeline references and types."""
        schema = self.resolved_schema

        # Validate group_by columns exist
        for col in self.pipeline.group_by_cols:
            if col not in schema.fields:
                # Check if it's a calculated field
                calc_field = next((c for c in self.pipeline.calc_stmts if c.stage == "pre" and c.name == col), None)
                if calc_field is None:
                    raise BadDSLException(f"Unknown column in group_by: {col}")

        # Validate calc statements
        pre_calc_names = set()
        post_calc_names = set()

        for calc in self.pipeline.calc_stmts:
            if calc.stage == "pre":
                if calc.name in pre_calc_names:
                    raise BadDSLException(f"Duplicate calc name: {calc.name}")
                pre_calc_names.add(calc.name)
            else:  # post_agg
                if calc.name in post_calc_names:
                    raise BadDSLException(f"Duplicate calc name: {calc.name}")
                post_calc_names.add(calc.name)

            # Validate expression type matches declared type
            try:
                expr_type = calc.expr.get_type(schema.fields)
                if not types_compatible(expr_type, calc.type):
                    raise TypeErrorException(
                        f"Calc '{calc.name}' type mismatch: expression has {type(expr_type).__name__}, "
                        f"declared as {type(calc.type).__name__}"
                    )
            except Exception as e:
                if isinstance(e, (BadDSLException, TypeErrorException)):
                    raise
                raise TypeErrorException(f"Calc '{calc.name}' type error: {e}")

        # Validate post_agg calcs don't reference pre-stage-only constructs incorrectly
        for calc in self.pipeline.calc_stmts:
            if calc.stage == "post_agg":
                # Check for aggregate aliases
                if self.pipeline.aggregate_spec:
                    for agg in self.pipeline.aggregate_spec["aggs"]:
                        if agg["alias"] in get_expr_vars(calc.expr):
                            # This is OK - post_agg can reference aggregates
                            pass

    def _load_csv(self, csv_path: str):
        """Load CSV and create rows with schema-applied types."""
        try:
            with open(csv_path, 'r', newline='') as f:
                reader = csv.DictReader(f)
                if reader.fieldnames is None:
                    raise BadIOException("CSV file has no header")

                # Get schema fields - these are the expected physical columns
                schema_fields = {name: field for name, field in self.resolved_schema.fields.items()
                                if not field.is_calculated}

                # Read all rows
                row_idx = 0
                for row_dict in reader:
                    row_values = {}

                    # Apply schema types to CSV data
                    for col, field in schema_fields.items():
                        val_str = row_dict.get(col, "")
                        row_values[col] = self._parse_value(field.type, val_str, col)

                    # Add calculated fields from schema (pre-stage)
                    for field_name, field in self.resolved_schema.fields.items():
                        if field.is_calculated and field.expr:
                            try:
                                result = field.expr.eval(row_values)
                                row_values[field_name] = result
                            except Exception as e:
                                raise BadDSLException(f"Schema calc '{field_name}' failed: {e}")

                    self.rows.append(Row(row_values, row_idx))
                    row_idx += 1

        except FileNotFoundError:
            raise BadIOException(f"CSV file not found: {csv_path}")
        except IOError as e:
            raise BadIOException(f"Cannot read CSV file: {e}")

    def _parse_value(self, col_type: ValueType, val_str: str, col_name: str) -> MTLValue:
        """Parse a string value according to its schema type."""
        if val_str == "" or val_str is None:
            # Return null-like value
            return MTLValue(None, col_type)

        if isinstance(col_type, TimestampType):
            return self._parse_timestamp(val_str)
        elif isinstance(col_type, (IntType, NumberType)):
            try:
                if '.' in val_str or 'e' in val_str.lower():
                    return MTLValue(float(val_str), NumberType())
                else:
                    return MTLValue(int(val_str), IntType())
            except ValueError:
                raise TypeErrorException(f"Cannot parse '{val_str}' as number for column {col_name}")
        else:
            return MTLValue(val_str, StringType())

    def _parse_timestamp(self, ts_str: str) -> MTLValue:
        """Parse a timestamp string."""
        try:
            if 'T' in ts_str or (ts_str.count('-') == 2 and ts_str.count(':') >= 2):
                if '+' in ts_str or (ts_str.count('-') > 2):
                    ts_str = ts_str.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(ts_str.replace('+00:00', '+00:00'))
                else:
                    dt = datetime.fromisoformat(ts_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
            else:
                if self.timestamp_format:
                    dt = datetime.strptime(ts_str, self.timestamp_format)
                else:
                    dt = datetime.fromisoformat(ts_str)
                if dt.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    tz_obj = ZoneInfo(self.tz)
                    dt = dt.replace(tzinfo=tz_obj)

            return MTLValue(dt, TimestampType())
        except Exception as e:
            raise TypeErrorException(f"Cannot parse timestamp '{ts_str}': {e}")

    def execute(self) -> Any:
        """Execute the transformation pipeline."""
        pipeline = self.pipeline
        schema = self.resolved_schema

        # Build the full schema with pre-calc fields
        current_schema = dict(schema.fields)

        # Apply pre-stage calcs
        current_rows = self.rows
        for calc in pipeline.calc_stmts:
            if calc.stage == "pre":
                # Add calc to schema
                current_schema[calc.name] = SchemaField(calc.name, calc.type, calc.expr, True)

        # Apply filter
        if pipeline.filter_expr:
            current_rows = self._apply_filter(current_rows, pipeline.filter_expr, current_schema)

        # Apply apply statements
        for expr, new_col in pipeline.apply_stmts:
            current_rows = self._apply_apply(current_rows, expr, new_col, current_schema)
            current_schema[new_col] = SchemaField(new_col, expr.get_type(current_schema), None, False)

        # Apply windowing
        if pipeline.window_duration:
            current_rows = self._apply_window(current_rows, pipeline.window_duration)
            current_schema["window_start"] = SchemaField("window_start", TimestampType(), None, False)
            current_schema["window_end"] = SchemaField("window_end", TimestampType(), None, False)

        # Apply grouping and aggregation
        if not pipeline.aggregate_spec:
            raise BadDSLException("Missing required aggregate statement")

        results = self._apply_aggregation(
            current_rows, pipeline.aggregate_spec,
            pipeline.group_by_cols, pipeline.window_duration, current_schema
        )

        # Apply post-aggregation calcs
        post_calc_stmts = [c for c in pipeline.calc_stmts if c.stage == "post_agg"]
        if post_calc_stmts:
            results = self._apply_post_agg_calcs(results, post_calc_stmts, pipeline.group_by_cols)

        # Sort results
        results = self._sort_results(results, pipeline.group_by_cols, pipeline.window_duration)

        return results

    def _apply_filter(self, rows: List[Row], expr: Expression, schema: Dict[str, SchemaField]) -> List[Row]:
        """Filter rows based on expression."""
        filtered = []
        for row in rows:
            try:
                # Resolve param references
                context = self._resolve_expr_params(row.values, expr)
                result = expr.eval(context)
                if result.as_bool():
                    filtered.append(row)
            except RuntimeException:
                raise
            except Exception as e:
                raise TypeErrorException(f"Filter evaluation error: {e}")
        return filtered

    def _apply_apply(self, rows: List[Row], expr: Expression, new_col: str,
                     schema: Dict[str, SchemaField]) -> List[Row]:
        """Apply transform and add new column."""
        for row in rows:
            try:
                context = self._resolve_expr_params(row.values, expr)
                result = expr.eval(context)
                row.values[new_col] = result
            except RuntimeException:
                raise
            except Exception as e:
                raise TypeErrorException(f"Apply evaluation error: {e}")
        return rows

    def _resolve_expr_params(self, values: Dict[str, MTLValue], expr: Expression) -> Dict[str, MTLValue]:
        """Resolve param references in an expression to actual values."""
        # For now, we'll handle params during evaluation
        # The expression should be evaluated with params resolved
        return values

    def _apply_apply_with_params(self, rows: List[Row], expr: Expression, new_col: str,
                                  schema: Dict[str, SchemaField]) -> List[Row]:
        """Apply transform with parameter resolution."""
        for row in rows:
            try:
                # Replace param references with actual values
                resolved_expr = self._resolve_params_in_expr(expr)
                result = resolved_expr.eval(row.values)
                row.values[new_col] = result
            except RuntimeException:
                raise
            except Exception as e:
                raise TypeErrorException(f"Apply evaluation error: {e}")
        return rows

    def _resolve_params_in_expr(self, expr: Expression) -> Expression:
        """Replace ParamRefExpr with literal values."""
        if isinstance(expr, ParamRefExpr):
            if expr.name not in self.params:
                raise RuntimeException(f"Parameter '{expr.name}' not provided")
            return LiteralExpr(self.params[expr.name])

        if isinstance(expr, BinaryOpExpr):
            return BinaryOpExpr(
                self._resolve_params_in_expr(expr.left),
                expr.op,
                self._resolve_params_in_expr(expr.right)
            )

        if isinstance(expr, UnaryOpExpr):
            return UnaryOpExpr(expr.op, self._resolve_params_in_expr(expr.operand))

        if isinstance(expr, TransformExpr):
            return TransformExpr(
                expr.func,
                self._resolve_params_in_expr(expr.arg),
                self._resolve_params_in_expr(expr.digits) if expr.digits else None
            )

        if isinstance(expr, CoalesceExpr):
            return CoalesceExpr(
                self._resolve_params_in_expr(expr.left),
                self._resolve_params_in_expr(expr.right)
            )

        if isinstance(expr, CastExpr):
            return CastExpr(
                self._resolve_params_in_expr(expr.expr),
                expr.target_type
            )

        if isinstance(expr, BinaryStringFuncExpr):
            return BinaryStringFuncExpr(
                expr.func,
                self._resolve_params_in_expr(expr.left),
                self._resolve_params_in_expr(expr.right)
            )

        return expr

    def _apply_window(self, rows: List[Row], duration: str) -> List[Row]:
        """Add window_start and window_end columns."""
        match = self._parse_duration(duration)
        unit_multipliers = {"m": 60, "h": 3600, "d": 86400}
        duration_seconds = match[0] * unit_multipliers[match[1]]

        epoch = datetime(1970, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

        for row in rows:
            ts_val = row.values.get("timestamp")
            if ts_val is None or ts_val.value is None:
                row.values["window_start"] = MTLValue(None, TimestampType())
                row.values["window_end"] = MTLValue(None, TimestampType())
                continue

            ts = ts_val.as_timestamp()
            if ts.tzinfo is None:
                from zoneinfo import ZoneInfo
                tz_obj = ZoneInfo(self.tz)
                ts = ts.replace(tzinfo=tz_obj)
            ts_utc = ts.astimezone(timezone.utc)

            diff_seconds = (ts_utc - epoch).total_seconds()
            window_start_seconds = int(diff_seconds // duration_seconds) * duration_seconds
            window_start = epoch + timedelta(seconds=window_start_seconds)
            window_end = window_start + timedelta(seconds=duration_seconds)

            row.values["window_start"] = MTLValue(window_start, TimestampType())
            row.values["window_end"] = MTLValue(window_end, TimestampType())

        return rows

    def _parse_duration(self, duration: str) -> Tuple[int, str]:
        """Parse duration literal like '5m', '1h', '7d'."""
        if duration.endswith('m'):
            return int(duration[:-1]), 'm'
        elif duration.endswith('h'):
            return int(duration[:-1]), 'h'
        elif duration.endswith('d'):
            return int(duration[:-1]), 'd'
        else:
            raise BadDSLException(f"Invalid duration literal: {duration}")

    def _apply_aggregation(self, rows: List[Row], aggregate_spec: Dict,
                           group_by_cols: List[str], window_duration: Optional[str],
                           schema: Dict[str, SchemaField]) -> List[Dict]:
        """Apply aggregation and return result."""
        if not rows:
            if not group_by_cols and not window_duration:
                return [self._build_empty_aggregates(aggregate_spec)]
            else:
                return []

        groups: Dict[Tuple, List[Row]] = {}

        if group_by_cols:
            for row in rows:
                key_values = []
                for col in group_by_cols:
                    val = row.values.get(col)
                    if val is not None and val.value is not None:
                        key_values.append(val.value)
                    else:
                        key_values.append(None)
                key = tuple(key_values)
                if key not in groups:
                    groups[key] = []
                groups[key].append(row)
        else:
            groups[()] = rows

        results = []

        for key, group_rows in groups.items():
            result_obj: Dict[str, Any] = {}

            # Add group-by keys
            if group_by_cols:
                for i, col in enumerate(group_by_cols):
                    val = key[i]
                    if val is not None:
                        result_obj[col] = val
                    else:
                        result_obj[col] = None

            # Add window keys
            if window_duration:
                ws = group_rows[0].values.get("window_start")
                we = group_rows[0].values.get("window_end")
                if ws and ws.value:
                    result_obj["window_start"] = ws.value.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    result_obj["window_start"] = None
                if we and we.value:
                    result_obj["window_end"] = we.value.strftime("%Y-%m-%dT%H:%M:%SZ")
                else:
                    result_obj["window_end"] = None

            # Compute aggregates
            for agg in aggregate_spec["aggs"]:
                alias = agg["alias"]
                func = agg["func"]
                arg_expr = agg["arg"]

                if isinstance(arg_expr, LiteralExpr) and arg_expr.value.value == "*":
                    if func == "count":
                        result_obj[alias] = len(group_rows)
                    else:
                        raise TypeErrorException(f"Aggregate {func}(*) is not valid")
                else:
                    col_expr = arg_expr

                    if isinstance(col_expr, ColumnRefExpr):
                        col_name = col_expr.name
                    else:
                        col_name = getattr(col_expr, 'name', str(col_expr))

                    values = []
                    for row in group_rows:
                        if col_name in row.values:
                            val = row.values[col_name]
                            if val and val.is_numeric():
                                values.append(val.as_number())

                    if not values:
                        if func == "count":
                            result_obj[alias] = 0
                        else:
                            result_obj[alias] = None
                    else:
                        result_obj[alias] = self._compute_aggregate(func, values)

            results.append(result_obj)

        return results

    def _compute_aggregate(self, func: str, values: List[float]) -> Any:
        """Compute aggregate function on values."""
        n = len(values)

        if func == "sum":
            return sum(values)
        elif func == "average":
            return sum(values) / n
        elif func == "median":
            sorted_vals = sorted(values)
            mid = n // 2
            if n % 2 == 1:
                return sorted_vals[mid]
            else:
                return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2
        elif func == "count":
            return n
        elif func == "std":
            if n == 0:
                return None
            mean = sum(values) / n
            variance = sum((x - mean) ** 2 for x in values) / n
            return math.sqrt(variance)
        elif func == "var":
            if n == 0:
                return None
            mean = sum(values) / n
            return sum((x - mean) ** 2 for x in values) / n
        elif func == "min":
            return min(values)
        elif func == "max":
            return max(values)
        else:
            raise TypeErrorException(f"Unknown aggregate function: {func}")

    def _build_empty_aggregates(self, aggregate_spec: Dict) -> Dict:
        """Build empty aggregate result."""
        result = {}
        for agg in aggregate_spec["aggs"]:
            alias = agg["alias"]
            func = agg["func"]
            if func == "count":
                result[alias] = 0
            else:
                result[alias] = None
        return result

    def _apply_post_agg_calcs(self, results: List[Dict], calc_stmts: List[CalcStatement],
                               group_by_cols: List[str]) -> List[Dict]:
        """Apply post-aggregation calculated fields."""
        # First, create a wrapper that includes lag/lead support

        # Build context for each row
        for idx, result in enumerate(results):
            # Create a context with lag/lead support
            context = {}

            # Add all result keys to context
            for key, val in result.items():
                if isinstance(val, (int, float)):
                    context[key] = MTLValue(val, NumberType() if isinstance(val, float) else IntType())
                elif isinstance(val, str):
                    context[key] = MTLValue(val, StringType())
                elif val is None:
                    context[key] = MTLValue(None, NumberType())

            # Add lag/lead metadata
            context["_laglead_index"] = MTLValue(idx, IntType())
            context["_laglead_results"] = MTLValue(results, StringType())  # Pass results array

            # Evaluate each calc
            for calc in calc_stmts:
                try:
                    resolved_expr = self._resolve_params_in_expr(calc.expr)
                    result_val = resolved_expr.eval(context)
                    result[calc.name] = result_val.value
                except Exception as e:
                    raise RuntimeException(f"Post-agg calc '{calc.name}' failed: {e}")

        return results

    def _sort_results(self, results: List[Dict], group_by_cols: List[str],
                      window_duration: Optional[str]) -> List[Dict]:
        """Sort results according to normalization rules."""

        def sort_key(item: Dict) -> Tuple:
            key_parts = []

            if window_duration:
                ws = item.get("window_start", "")
                key_parts.append(ws if ws is not None else "")

            if group_by_cols:
                for col in group_by_cols:
                    val = item.get(col)
                    if val is None:
                        key_parts.append((1, ""))
                    elif isinstance(val, str):
                        key_parts.append((0, val))
                    elif isinstance(val, (int, float)):
                        key_parts.append((2, val))
                    else:
                        key_parts.append((3, str(val)))

            return tuple(key_parts)

        return sorted(results, key=sort_key)


def get_expr_vars(expr: Expression) -> Set[str]:
    """Extract variable names referenced in an expression."""
    vars: Set[str] = set()

    if isinstance(expr, ColumnRefExpr):
        vars.add(expr.name)
    elif isinstance(expr, BinaryOpExpr):
        vars.update(get_expr_vars(expr.left))
        vars.update(get_expr_vars(expr.right))
    elif isinstance(expr, UnaryOpExpr):
        vars.update(get_expr_vars(expr.operand))
    elif isinstance(expr, TransformExpr):
        vars.update(get_expr_vars(expr.arg))
        if expr.digits:
            vars.update(get_expr_vars(expr.digits))
    elif isinstance(expr, CoalesceExpr):
        vars.update(get_expr_vars(expr.left))
        vars.update(get_expr_vars(expr.right))
    elif isinstance(expr, CastExpr):
        vars.update(get_expr_vars(expr.expr))
    elif isinstance(expr, BinaryStringFuncExpr):
        vars.update(get_expr_vars(expr.left))
        vars.update(get_expr_vars(expr.right))

    return vars


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="MTL - Multi-Tenant Analytics Language CLI (Extended)"
    )
    parser.add_argument("--csv", required=True, help="Path to input CSV file")
    parser.add_argument("--dsl", required=True, help="Path to DSL file (use '-' for STDIN)")
    parser.add_argument("--out", default=None, help="Output file (default: STDOUT)")
    parser.add_argument("--tz", default="UTC", help="Default timezone (default: UTC)")
    parser.add_argument("--timestamp-format", default=None,
                        help="Timestamp format (default: ISO 8601)")
    parser.add_argument("--param", action="append", default=[],
                        help="Parameter in format key=value (can be used multiple times)")

    args = parser.parse_args()

    # Parse CLI params
    cli_params: Dict[str, str] = {}
    for param_str in args.param:
        if '=' not in param_str:
            print(f"ERROR:bad_dsl:Invalid param format: {param_str}", file=sys.stderr)
            sys.exit(1)
        key, value = param_str.split('=', 1)
        cli_params[key] = value

    try:
        engine = MTLEngine(
            csv_path=args.csv,
            dsl_path=args.dsl,
            tz=args.tz,
            timestamp_format=args.timestamp_format,
            cli_params=cli_params
        )

        result = engine.execute()

        # Output
        output_json = json.dumps(result, indent=2)

        if args.out:
            with open(args.out, 'w') as f:
                f.write(output_json)
        else:
            print(output_json)

        sys.exit(0)

    except MTLException as e:
        print(e.stderr_message(), file=sys.stderr)
        sys.exit(e.exit_code)
    except Exception as e:
        print(f"ERROR:unexpected:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
