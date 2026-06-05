#!/usr/bin/env python3
"""
ETL Pipeline Parser & Executor - Parses, validates, normalizes, and executes ETL pipeline specifications.
"""

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple, Union


class ETLError(Exception):
    """Custom exception for ETL pipeline errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


class ExpressionParser:
    """
    Recursive descent parser for ETL expressions.
    Supports: literals, identifiers, operators with proper precedence.
    """

    def __init__(self, expr: str):
        self.expr = expr
        self.pos = 0
        self.length = len(expr)

    def parse(self) -> 'ASTNode':
        """Parse the expression and return an AST."""
        result = self._parse_or()
        self._skip_whitespace()
        if self.pos < self.length:
            # Check what character is causing the issue
            remaining = self.expr[self.pos:]
            # Check for unsupported operators
            if '**' in remaining:
                raise ETLError("BAD_EXPR", "unsupported operator '**'", "")
            if '^^' in remaining:
                raise ETLError("BAD_EXPR", "unsupported operator '^^'", "")
            raise ETLError("BAD_EXPR", f"unexpected character '{remaining[0]}'", "")
        return result

    def _skip_whitespace(self):
        """Skip whitespace characters."""
        while self.pos < self.length and self.expr[self.pos] in ' \t\n\r':
            self.pos += 1

    def _peek(self, n: int = 1) -> str:
        """Peek at the next n characters."""
        return self.expr[self.pos:self.pos + n]

    def _match(self, s: str) -> bool:
        """Check if the next characters match the string."""
        return self.expr[self.pos:self.pos + len(s)] == s

    def _consume(self, s: str) -> bool:
        """Consume the string if it matches."""
        if self._match(s):
            self.pos += len(s)
            return True
        return False

    def _parse_or(self) -> 'ASTNode':
        """Parse logical OR (||)."""
        left = self._parse_and()
        self._skip_whitespace()
        while self._consume('||'):
            self._skip_whitespace()
            right = self._parse_and()
            left = BinaryOp('||', left, right)
            self._skip_whitespace()
        return left

    def _parse_and(self) -> 'ASTNode':
        """Parse logical AND (&&)."""
        left = self._parse_equality()
        self._skip_whitespace()
        while self._consume('&&'):
            self._skip_whitespace()
            right = self._parse_equality()
            left = BinaryOp('&&', left, right)
            self._skip_whitespace()
        return left

    def _parse_equality(self) -> 'ASTNode':
        """Parse equality operators (==, !=)."""
        left = self._parse_relational()
        self._skip_whitespace()
        while True:
            if self._consume('=='):
                self._skip_whitespace()
                right = self._parse_relational()
                left = BinaryOp('==', left, right)
            elif self._consume('!='):
                self._skip_whitespace()
                right = self._parse_relational()
                left = BinaryOp('!=', left, right)
            else:
                break
            self._skip_whitespace()
        return left

    def _parse_relational(self) -> 'ASTNode':
        """Parse relational operators (<, <=, >, >=)."""
        left = self._parse_additive()
        self._skip_whitespace()
        while True:
            if self._consume('<='):
                self._skip_whitespace()
                right = self._parse_additive()
                left = BinaryOp('<=', left, right)
            elif self._consume('>='):
                self._skip_whitespace()
                right = self._parse_additive()
                left = BinaryOp('>=', left, right)
            elif self._consume('<') and not self._match('<'):
                self._skip_whitespace()
                right = self._parse_additive()
                left = BinaryOp('<', left, right)
            elif self._consume('>') and not self._match('>'):
                self._skip_whitespace()
                right = self._parse_additive()
                left = BinaryOp('>', left, right)
            else:
                break
            self._skip_whitespace()
        return left

    def _parse_additive(self) -> 'ASTNode':
        """Parse additive operators (+, -)."""
        left = self._parse_multiplicative()
        self._skip_whitespace()
        while True:
            if self._consume('+'):
                self._skip_whitespace()
                right = self._parse_multiplicative()
                left = BinaryOp('+', left, right)
            elif self._consume('-') and not self._match('-'):
                self._skip_whitespace()
                right = self._parse_multiplicative()
                left = BinaryOp('-', left, right)
            else:
                break
            self._skip_whitespace()
        return left

    def _parse_multiplicative(self) -> 'ASTNode':
        """Parse multiplicative operators (*, /)."""
        left = self._parse_unary()
        self._skip_whitespace()
        while True:
            # Check for ** (unsupported)
            if self._match('**'):
                raise ETLError("BAD_EXPR", "unsupported operator '**'", "")
            if self._consume('*'):
                self._skip_whitespace()
                right = self._parse_unary()
                left = BinaryOp('*', left, right)
            elif self._consume('/'):
                self._skip_whitespace()
                right = self._parse_unary()
                left = BinaryOp('/', left, right)
            else:
                break
            self._skip_whitespace()
        return left

    def _parse_unary(self) -> 'ASTNode':
        """Parse unary operators (!, -)."""
        self._skip_whitespace()
        if self._consume('!'):
            self._skip_whitespace()
            operand = self._parse_unary()
            return UnaryOp('!', operand)
        if self._consume('-'):
            self._skip_whitespace()
            operand = self._parse_unary()
            return UnaryOp('-', operand)
        return self._parse_primary()

    def _parse_primary(self) -> 'ASTNode':
        """Parse primary expressions (literals, identifiers, parenthesized expressions)."""
        self._skip_whitespace()

        if self.pos >= self.length:
            raise ETLError("BAD_EXPR", "unexpected end of expression", "")

        # Parenthesized expression
        if self._consume('('):
            self._skip_whitespace()
            expr = self._parse_or()
            self._skip_whitespace()
            if not self._consume(')'):
                raise ETLError("BAD_EXPR", "expected ')'", "")
            return expr

        # String literal
        if self._peek() == '"':
            return self._parse_string()

        # Number literal
        if self._peek().isdigit() or (self._peek() == '-' and self.pos + 1 < self.length and self.expr[self.pos + 1].isdigit()):
            return self._parse_number()

        # Boolean/null literals or identifiers
        if self._peek().isalpha() or self._peek() == '_':
            return self._parse_identifier_or_literal()

        raise ETLError("BAD_EXPR", f"unexpected character '{self._peek()}'", "")

    def _parse_string(self) -> 'ASTNode':
        """Parse a string literal."""
        if not self._consume('"'):
            raise ETLError("BAD_EXPR", "expected string", "")

        result = []
        while self.pos < self.length and self.expr[self.pos] != '"':
            if self.expr[self.pos] == '\\':
                self.pos += 1
                if self.pos >= self.length:
                    raise ETLError("BAD_EXPR", "unterminated string", "")
                escape_char = self.expr[self.pos]
                if escape_char == 'n':
                    result.append('\n')
                elif escape_char == 't':
                    result.append('\t')
                elif escape_char == 'r':
                    result.append('\r')
                elif escape_char == '"':
                    result.append('"')
                elif escape_char == '\\':
                    result.append('\\')
                else:
                    result.append(escape_char)
            else:
                result.append(self.expr[self.pos])
            self.pos += 1

        if not self._consume('"'):
            raise ETLError("BAD_EXPR", "unterminated string", "")

        return Literal(''.join(result))

    def _parse_number(self) -> 'ASTNode':
        """Parse a number literal."""
        start = self.pos

        # Handle negative sign at start
        if self._peek() == '-':
            self.pos += 1

        while self.pos < self.length and self.expr[self.pos].isdigit():
            self.pos += 1

        if self.pos < self.length and self.expr[self.pos] == '.':
            self.pos += 1
            while self.pos < self.length and self.expr[self.pos].isdigit():
                self.pos += 1

        num_str = self.expr[start:self.pos]
        try:
            if '.' in num_str:
                return Literal(float(num_str))
            else:
                return Literal(int(num_str))
        except ValueError:
            raise ETLError("BAD_EXPR", f"invalid number '{num_str}'", "")

    def _parse_identifier_or_literal(self) -> 'ASTNode':
        """Parse an identifier or literal (true, false, null)."""
        start = self.pos
        while self.pos < self.length and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            self.pos += 1

        word = self.expr[start:self.pos]

        if word == 'true':
            return Literal(True)
        elif word == 'false':
            return Literal(False)
        elif word == 'null':
            return Literal(None)
        else:
            return Identifier(word)


class ASTNode:
    """Base class for AST nodes."""
    pass


class Literal(ASTNode):
    """Represents a literal value."""
    def __init__(self, value: Any):
        self.value = value


class Identifier(ASTNode):
    """Represents an identifier (variable reference)."""
    def __init__(self, name: str):
        self.name = name


class BinaryOp(ASTNode):
    """Represents a binary operation."""
    def __init__(self, op: str, left: ASTNode, right: ASTNode):
        self.op = op
        self.left = left
        self.right = right


class UnaryOp(ASTNode):
    """Represents a unary operation."""
    def __init__(self, op: str, operand: ASTNode):
        self.op = op
        self.operand = operand


def evaluate_expression(ast: ASTNode, row: Dict[str, Any]) -> Any:
    """
    Evaluate an expression AST against a row.
    Returns the result value.
    """
    if isinstance(ast, Literal):
        return ast.value

    elif isinstance(ast, Identifier):
        # Missing identifiers resolve to null
        return row.get(ast.name)

    elif isinstance(ast, UnaryOp):
        operand = evaluate_expression(ast.operand, row)
        if ast.op == '!':
            if operand is None:
                return True
            return not operand
        elif ast.op == '-':
            if operand is None:
                return None
            if isinstance(operand, (int, float)):
                return -operand
            return None

    elif isinstance(ast, BinaryOp):
        left = evaluate_expression(ast.left, row)
        right = evaluate_expression(ast.right, row)

        # Arithmetic operators
        if ast.op in ('+', '-', '*', '/'):
            if left is None or right is None:
                return None
            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                return None
            if ast.op == '+':
                return left + right
            elif ast.op == '-':
                return left - right
            elif ast.op == '*':
                return left * right
            elif ast.op == '/':
                if right == 0:
                    return None
                return left / right

        # Comparison operators
        elif ast.op in ('<', '<=', '>', '>='):
            # Type mismatch returns false
            if left is None or right is None:
                return False
            # Allow numeric comparisons between int and float
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                pass  # Valid numeric comparison
            elif type(left) != type(right):
                return False
            elif not isinstance(left, (int, float, str)):
                return False
            if ast.op == '<':
                return left < right
            elif ast.op == '<=':
                return left <= right
            elif ast.op == '>':
                return left > right
            elif ast.op == '>=':
                return left >= right

        # Equality operators
        elif ast.op == '==':
            # Type mismatch returns false
            if left is None and right is None:
                return True
            if left is None or right is None:
                return False
            # Allow numeric comparisons between int and float
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left == right
            if type(left) != type(right):
                return False
            return left == right

        elif ast.op == '!=':
            # Type mismatch returns false
            if left is None and right is None:
                return False
            if left is None or right is None:
                return True
            # Allow numeric comparisons between int and float
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left != right
            if type(left) != type(right):
                return False
            return left != right

        # Logical operators
        elif ast.op == '&&':
            # Null is falsy
            if left is None or not left:
                return False
            if right is None:
                return False
            return bool(right)

        elif ast.op == '||':
            # Null is falsy
            if left is not None and left:
                return True
            if right is not None and right:
                return True
            return False

    return None


def parse_expression(expr: str, path: str) -> ASTNode:
    """
    Parse an expression string and return an AST.
    Raises ETLError on parse failure.
    """
    try:
        parser = ExpressionParser(expr)
        ast = parser.parse()
        return ast
    except ETLError as e:
        # Re-raise with the path
        raise ETLError(e.error_code, e.message, path)


def create_error_response(error_code: str, message: str, path: str) -> Dict[str, Any]:
    """Create a standardized error response."""
    return {
        "status": "error",
        "error_code": error_code,
        "message": f"ETL_ERROR: {message}",
        "path": path
    }


def create_success_response(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Create a standardized success response for normalization."""
    return {
        "status": "ok",
        "normalized": normalized
    }


def create_execution_response(data: List[Dict[str, Any]], rows_in: int, rows_out: int) -> Dict[str, Any]:
    """Create a standardized success response for execution."""
    return {
        "status": "ok",
        "data": data,
        "metrics": {
            "rows_in": rows_in,
            "rows_out": rows_out
        }
    }


def is_empty_after_trim(value: Optional[str]) -> bool:
    """Check if a string is None or empty/whitespace-only after trimming."""
    if value is None:
        return True
    return len(value.strip()) == 0


def validate_expression(expr: str) -> bool:
    """
    Validate an expression for obvious syntax errors.
    Returns True if valid, False if invalid.
    """
    if is_empty_after_trim(expr):
        return False

    expr = expr.strip()

    # Check for unsupported operators
    if '**' in expr:
        return False
    if '^^' in expr:
        return False

    # Check for mismatched parentheses
    paren_count = 0
    for char in expr:
        if char == '(':
            paren_count += 1
        elif char == ')':
            paren_count -= 1
            if paren_count < 0:
                return False
    if paren_count != 0:
        return False

    return True


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Normalize a single pipeline step.
    Returns the normalized step or raises ETLError.
    """
    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "step must be an object",
            f"pipeline.steps[{index}]"
        )

    # Get the operation name
    if "op" not in step:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'op'",
            f"pipeline.steps[{index}]"
        )

    op_value = step.get("op")
    if not isinstance(op_value, str):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op must be a string",
            f"pipeline.steps[{index}].op"
        )

    if is_empty_after_trim(op_value):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op cannot be empty",
            f"pipeline.steps[{index}].op"
        )

    # Normalize operation name: trim and lowercase
    op_normalized = op_value.strip().lower()

    # Known operations
    valid_ops = {"select", "filter", "map", "rename", "limit"}

    if op_normalized not in valid_ops:
        raise ETLError(
            "UNKNOWN_OP",
            f"unsupported op '{op_normalized}'",
            f"pipeline.steps[{index}].op"
        )

    # Build normalized step
    normalized = {"op": op_normalized}

    # Validate and normalize based on operation type
    if op_normalized == "select":
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "select requires 'columns' field",
                f"pipeline.steps[{index}]"
            )

        columns = step["columns"]
        if not isinstance(columns, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "columns must be an array",
                f"pipeline.steps[{index}].columns"
            )

        # Validate each column is a string
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "column names must be strings",
                    f"pipeline.steps[{index}].columns[{i}]"
                )

        # Column names are preserved exactly (not trimmed)
        normalized["columns"] = columns

    elif op_normalized == "filter":
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "filter requires 'where' field",
                f"pipeline.steps[{index}]"
            )

        where = step["where"]
        if not isinstance(where, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "where must be a string",
                f"pipeline.steps[{index}].where"
            )

        if is_empty_after_trim(where):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "where cannot be empty",
                f"pipeline.steps[{index}].where"
            )

        # Validate expression - use the parser to get detailed error
        try:
            parse_expression(where, f"pipeline.steps[{index}].where")
        except ETLError as e:
            raise

        # Normalize: trim
        normalized["where"] = where.strip()

    elif op_normalized == "map":
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "map requires 'as' field",
                f"pipeline.steps[{index}]"
            )

        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "map requires 'expr' field",
                f"pipeline.steps[{index}]"
            )

        as_value = step["as"]
        if not isinstance(as_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as must be a string",
                f"pipeline.steps[{index}].as"
            )

        if is_empty_after_trim(as_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as cannot be empty",
                f"pipeline.steps[{index}].as"
            )

        expr = step["expr"]
        if not isinstance(expr, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr must be a string",
                f"pipeline.steps[{index}].expr"
            )

        if is_empty_after_trim(expr):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr cannot be empty",
                f"pipeline.steps[{index}].expr"
            )

        # Validate expression - use the parser to get detailed error
        try:
            parse_expression(expr, f"pipeline.steps[{index}].expr")
        except ETLError as e:
            raise

        # Normalize: trim
        normalized["as"] = as_value.strip()
        normalized["expr"] = expr.strip()

    elif op_normalized == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if has_from_to and has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename cannot have both from/to and mapping",
                f"pipeline.steps[{index}]"
            )

        if not has_from_to and not has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename requires either from/to or mapping",
                f"pipeline.steps[{index}]"
            )

        if has_from_to:
            from_value = step["from"]
            to_value = step["to"]

            if not isinstance(from_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from must be a string",
                    f"pipeline.steps[{index}].from"
                )

            if not isinstance(to_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to must be a string",
                    f"pipeline.steps[{index}].to"
                )

            if is_empty_after_trim(from_value):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from cannot be empty",
                    f"pipeline.steps[{index}].from"
                )

            if is_empty_after_trim(to_value):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to cannot be empty",
                    f"pipeline.steps[{index}].to"
                )

            # Convert from/to to mapping (trim keys)
            # Mapping keys/values are preserved exactly after trimming
            normalized["mapping"] = {from_value.strip(): to_value.strip()}

        else:  # has_mapping
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "mapping must be an object",
                    f"pipeline.steps[{index}].mapping"
                )

            # Validate all keys and values are strings
            for key, value in mapping.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping keys must be strings",
                        f"pipeline.steps[{index}].mapping"
                    )
                if not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping values must be strings",
                        f"pipeline.steps[{index}].mapping"
                    )

            # Preserve mapping exactly (no trimming of keys/values in mapping object)
            normalized["mapping"] = mapping

    elif op_normalized == "limit":
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "limit requires 'n' field",
                f"pipeline.steps[{index}]"
            )

        n = step["n"]
        if not isinstance(n, int) or isinstance(n, bool):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be an integer",
                f"pipeline.steps[{index}].n"
            )

        if n < 0:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be >= 0",
                f"pipeline.steps[{index}].n"
            )

        normalized["n"] = n

    # Sort keys: op first, then alphabetically
    result = {"op": normalized["op"]}
    for key in sorted(normalized.keys()):
        if key != "op":
            result[key] = normalized[key]

    return result


def validate_pipeline(pipeline: Any) -> List[Dict[str, Any]]:
    """
    Validate and normalize the pipeline.
    Returns the list of normalized steps or raises ETLError.
    """
    if not isinstance(pipeline, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "pipeline must be an object",
            "pipeline"
        )

    if "steps" not in pipeline:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "pipeline requires 'steps' field",
            "pipeline"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "steps must be an array",
            "pipeline.steps"
        )

    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_steps.append(normalize_step(step, i))

    return normalized_steps


def validate_dataset(dataset: Any) -> None:
    """
    Validate the dataset.
    Raises ETLError if invalid.
    """
    if not isinstance(dataset, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "dataset must be an array",
            "dataset"
        )

    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "dataset elements must be objects",
                f"dataset[{i}]"
            )


def execute_select(data: List[Dict[str, Any]], columns: List[str], step_index: int) -> List[Dict[str, Any]]:
    """
    Execute a select operation.
    Columns must exist; missing column -> MISSING_COLUMN error.
    """
    result = []
    for row_idx, row in enumerate(data):
        new_row = {}
        for col_idx, col in enumerate(columns):
            if col not in row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"column '{col}' not found in row",
                    f"pipeline.steps[{step_index}].columns[{col_idx}]"
                )
            new_row[col] = row[col]
        result.append(new_row)
    return result


def execute_filter(data: List[Dict[str, Any]], where: str, step_index: int) -> List[Dict[str, Any]]:
    """
    Execute a filter operation.
    Keep only rows where the expression evaluates to true.
    """
    ast = parse_expression(where, f"pipeline.steps[{step_index}].where")

    result = []
    for row in data:
        try:
            value = evaluate_expression(ast, row)
            if value is True:
                result.append(row)
        except Exception:
            # Execution errors during filter just exclude the row
            pass
    return result


def execute_map(data: List[Dict[str, Any]], as_field: str, expr: str, step_index: int) -> List[Dict[str, Any]]:
    """
    Execute a map operation.
    Add or overwrite a field with the evaluated expression.
    """
    ast = parse_expression(expr, f"pipeline.steps[{step_index}].expr")

    result = []
    for row in data:
        new_row = dict(row)
        try:
            value = evaluate_expression(ast, row)
            new_row[as_field] = value
        except Exception as e:
            # Execution error
            raise ETLError(
                "EXECUTION_FAILED",
                f"expression evaluation failed: {str(e)}",
                f"pipeline.steps[{step_index}].expr"
            )
        result.append(new_row)
    return result


def execute_rename(data: List[Dict[str, Any]], mapping: Dict[str, str], step_index: int) -> List[Dict[str, Any]]:
    """
    Execute a rename operation.
    Source column must exist; missing source -> MISSING_COLUMN error.
    """
    result = []
    for row in data:
        new_row = dict(row)
        # Apply mappings in iteration order
        for src_col, tgt_col in mapping.items():
            if src_col not in new_row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"column '{src_col}' not found in row",
                    f"pipeline.steps[{step_index}].mapping.{src_col}"
                )
            # Get the value, remove old key, add new key
            value = new_row[src_col]
            del new_row[src_col]
            new_row[tgt_col] = value
        result.append(new_row)
    return result


def execute_limit(data: List[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
    """
    Execute a limit operation.
    Return only the first n rows.
    """
    return data[:n]


def execute_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Execute the normalized pipeline steps on the dataset.
    Returns the result data, rows_in, and rows_out.
    """
    rows_in = len(dataset)
    data = dataset

    for step_index, step in enumerate(steps):
        op = step["op"]

        try:
            if op == "select":
                data = execute_select(data, step["columns"], step_index)
            elif op == "filter":
                data = execute_filter(data, step["where"], step_index)
            elif op == "map":
                data = execute_map(data, step["as"], step["expr"], step_index)
            elif op == "rename":
                data = execute_rename(data, step["mapping"], step_index)
            elif op == "limit":
                data = execute_limit(data, step["n"])
        except ETLError:
            raise
        except Exception as e:
            raise ETLError(
                "EXECUTION_FAILED",
                str(e),
                f"pipeline.steps[{step_index}]"
            )

    rows_out = len(data)
    return data, rows_in, rows_out


def process_request(input_json: str, execute: bool = False) -> Dict[str, Any]:
    """
    Process the ETL pipeline request.
    Returns the response dictionary.
    """
    try:
        data = json.loads(input_json)
    except json.JSONDecodeError as e:
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            f"invalid JSON: {str(e)}",
            ""
        )

    if not isinstance(data, dict):
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            "request must be a JSON object",
            ""
        )

    # Check for required fields
    if "pipeline" not in data:
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'pipeline'",
            ""
        )

    if "dataset" not in data:
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'dataset'",
            ""
        )

    # Validate dataset
    try:
        validate_dataset(data["dataset"])
    except ETLError as e:
        return create_error_response(e.error_code, e.message, e.path)

    # Validate and normalize pipeline
    try:
        normalized_steps = validate_pipeline(data["pipeline"])
    except ETLError as e:
        return create_error_response(e.error_code, e.message, e.path)

    # If not executing, return normalized
    if not execute:
        return create_success_response({"steps": normalized_steps})

    # Execute the pipeline
    try:
        result_data, rows_in, rows_out = execute_pipeline(normalized_steps, data["dataset"])
        return create_execution_response(result_data, rows_in, rows_out)
    except ETLError as e:
        return create_error_response(e.error_code, e.message, e.path)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='ETL Pipeline Parser & Executor')
    parser.add_argument('--execute', action='store_true', default=False,
                        help='Execute the pipeline (default: just validate and normalize)')
    args = parser.parse_args()

    try:
        input_data = sys.stdin.read()
    except Exception as e:
        response = create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            f"failed to read input: {str(e)}",
            ""
        )
        print(json.dumps(response))
        sys.exit(1)

    response = process_request(input_data, execute=args.execute)
    print(json.dumps(response))

    if response["status"] == "error":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
