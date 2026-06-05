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
            # Check for member access (e.g., params.key)
            result = Identifier(word)
            self._skip_whitespace()
            while self._consume('.'):
                self._skip_whitespace()
                # Parse member name
                member_start = self.pos
                while self.pos < self.length and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
                    self.pos += 1
                if member_start == self.pos:
                    raise ETLError("BAD_EXPR", "expected member name after '.'", "")
                member = self.expr[member_start:self.pos]
                result = MemberAccess(result, member)
                self._skip_whitespace()
            return result


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


class MemberAccess(ASTNode):
    """Represents member access (e.g., params.key)."""
    def __init__(self, obj: ASTNode, member: str):
        self.obj = obj
        self.member = member


def evaluate_expression(ast: ASTNode, row: Dict[str, Any], params: Optional[Dict[str, Any]] = None) -> Any:
    """
    Evaluate an expression AST against a row.
    Returns the result value.
    params is an optional dictionary of parameter values from call steps.
    """
    if isinstance(ast, Literal):
        return ast.value

    elif isinstance(ast, Identifier):
        # Missing identifiers resolve to null
        return row.get(ast.name)

    elif isinstance(ast, MemberAccess):
        # Handle params.key - only 'params' is supported
        if isinstance(ast.obj, Identifier) and ast.obj.name == "params":
            if params is None:
                return None
            return params.get(ast.member)
        # For other member access, try to get from row
        obj_val = evaluate_expression(ast.obj, row, params)
        if obj_val is None:
            return None
        if isinstance(obj_val, dict):
            return obj_val.get(ast.member)
        return None

    elif isinstance(ast, UnaryOp):
        operand = evaluate_expression(ast.operand, row, params)
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
        left = evaluate_expression(ast.left, row, params)
        right = evaluate_expression(ast.right, row, params)

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


def normalize_branch_step(step: Dict[str, Any], path: str) -> Dict[str, Any]:
    """
    Normalize a step within a branch.
    Similar to normalize_step but uses a custom path.
    """
    return normalize_step_with_path(step, path)


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Normalize a single pipeline step.
    Returns the normalized step or raises ETLError.
    """
    path = f"pipeline.steps[{index}]"
    return normalize_step_with_path(step, path)


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


def validate_defs(defs: Any) -> Dict[str, List[Dict[str, Any]]]:
    """
    Validate and normalize the defs section.
    Returns a dict mapping definition names to their normalized steps.
    """
    if defs is None:
        return {}

    if not isinstance(defs, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "defs must be an object",
            "defs"
        )

    normalized_defs = {}
    for name, defn in defs.items():
        if not isinstance(defn, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"definition '{name}' must be an object",
                f"defs[{name}]"
            )

        if "steps" not in defn:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"definition '{name}' requires 'steps' field",
                f"defs[{name}]"
            )

        steps = defn["steps"]
        if not isinstance(steps, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"definition '{name}' steps must be an array",
                f"defs[{name}].steps"
            )

        normalized_steps = []
        for i, step in enumerate(steps):
            normalized_steps.append(normalize_def_step(step, name, i))

        normalized_defs[name] = normalized_steps

    return normalized_defs


def validate_library(library: Any) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Validate and normalize the library section.
    Returns a dict mapping namespace -> definition name -> normalized steps.
    """
    if library is None:
        return {}

    if not isinstance(library, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "library must be an object",
            "library"
        )

    normalized_library = {}
    for ns, ns_def in library.items():
        if not isinstance(ns_def, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"namespace '{ns}' must be an object",
                f"library[{ns}]"
            )

        if "defs" not in ns_def:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"namespace '{ns}' requires 'defs' field",
                f"library[{ns}]"
            )

        defs = ns_def["defs"]
        if not isinstance(defs, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"namespace '{ns}' defs must be an object",
                f"library[{ns}].defs"
            )

        normalized_ns_defs = {}
        for def_name, defn in defs.items():
            if not isinstance(defn, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"definition '{def_name}' must be an object",
                    f"library[{ns}].defs[{def_name}]"
                )

            if "steps" not in defn:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"definition '{def_name}' requires 'steps' field",
                    f"library[{ns}].defs[{def_name}]"
                )

            steps = defn["steps"]
            if not isinstance(steps, list):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"definition '{def_name}' steps must be an array",
                    f"library[{ns}].defs[{def_name}].steps"
                )

            normalized_steps = []
            for i, step in enumerate(steps):
                path = f"library[{ns}].defs[{def_name}].steps[{i}]"
                normalized_steps.append(normalize_step_with_path(step, path, None))

            normalized_ns_defs[def_name] = normalized_steps

        normalized_library[ns] = normalized_ns_defs

    return normalized_library


def parse_library_ref(ref: str) -> Tuple[str, str]:
    """
    Parse a library reference in format 'ns:name'.
    Returns (namespace, name) tuple.
    Raises ETLError if format is invalid.
    """
    parts = ref.split(':')
    if len(parts) != 2:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            f"invalid reference format '{ref}', expected 'ns:name'",
            ""
        )
    return parts[0], parts[1]


def validate_and_resolve_ref(ref: str, params: Any, library: Dict[str, Dict[str, List[Dict[str, Any]]]], path: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Validate a library reference and resolve it to steps with bound params.
    Returns (normalized_steps, params).
    Raises ETLError if namespace or definition not found.
    """
    # Parse reference
    ns, name = parse_library_ref(ref)

    # Check namespace exists
    if ns not in library:
        raise ETLError(
            "UNKNOWN_NAMESPACE",
            f"namespace '{ns}' not found in library",
            path
        )

    # Check definition exists in namespace
    if name not in library[ns]:
        raise ETLError(
            "UNKNOWN_LIB_REF",
            f"definition '{name}' not found in namespace '{ns}'",
            path
        )

    # Validate params
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "params must be an object",
            f"{path}.params"
        )

    # Return the definition's steps and the params
    return library[ns][name], params


def validate_compose(compose: Any, library: Dict[str, Dict[str, List[Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """
    Validate and normalize the compose section.
    Returns a list of normalized steps (flattened from all compose items).
    """
    if compose is None:
        return []

    if not isinstance(compose, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "compose must be an array",
            "compose"
        )

    all_normalized_steps = []

    for i, item in enumerate(compose):
        path = f"compose[{i}]"

        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "compose item must be an object",
                path
            )

        # Each item must have either 'ref' or 'steps', not both
        has_ref = "ref" in item
        has_steps = "steps" in item

        if not has_ref and not has_steps:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "compose item must have either 'ref' or 'steps'",
                path
            )

        if has_ref and has_steps:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "compose item cannot have both 'ref' and 'steps'",
                path
            )

        if has_ref:
            # Handle library reference
            ref = item["ref"]
            if not isinstance(ref, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ref must be a string",
                    f"{path}.ref"
                )

            if is_empty_after_trim(ref):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ref cannot be empty",
                    f"{path}.ref"
                )

            params = item.get("params", {})
            steps, params = validate_and_resolve_ref(ref.strip(), params, library, f"{path}.ref")

            # Store params with each step for execution
            for step in steps:
                # Copy step and add params for later execution
                step_with_params = dict(step)
                if params:
                    step_with_params["_lib_params"] = params
                all_normalized_steps.append(step_with_params)
        else:
            # Handle inline steps
            steps = item["steps"]
            if not isinstance(steps, list):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "steps must be an array",
                    f"{path}.steps"
                )

            for j, step in enumerate(steps):
                step_path = f"{path}.steps[{j}]"
                normalized = normalize_step_with_path(step, step_path, None)
                all_normalized_steps.append(normalized)

    return all_normalized_steps


def normalize_def_step(step: Dict[str, Any], def_name: str, index: int) -> Dict[str, Any]:
    """
    Normalize a step within a definition.
    Uses defs[Name].steps[k] path format.
    """
    path = f"defs[{def_name}].steps[{index}]"
    return normalize_step_with_path(step, path, def_name)


def normalize_step_with_path(step: Dict[str, Any], path: str, current_def: Optional[str] = None) -> Dict[str, Any]:
    """
    Normalize a single pipeline step with a custom path.
    current_def is the name of the definition being processed (for recursion detection).
    """
    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "step must be an object",
            path
        )

    if "op" not in step:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'op'",
            path
        )

    op_value = step.get("op")
    if not isinstance(op_value, str):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op must be a string",
            f"{path}.op"
        )

    if is_empty_after_trim(op_value):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op cannot be empty",
            f"{path}.op"
        )

    op_normalized = op_value.strip().lower()

    valid_ops = {"select", "filter", "map", "rename", "limit", "branch", "call"}

    if op_normalized not in valid_ops:
        raise ETLError(
            "UNKNOWN_OP",
            f"unsupported op '{op_normalized}'",
            f"{path}.op"
        )

    normalized = {"op": op_normalized}

    if op_normalized == "select":
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "select requires 'columns' field",
                path
            )

        columns = step["columns"]
        if not isinstance(columns, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "columns must be an array",
                f"{path}.columns"
            )

        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "column names must be strings",
                    f"{path}.columns[{i}]"
                )

        normalized["columns"] = columns

    elif op_normalized == "filter":
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "filter requires 'where' field",
                path
            )

        where = step["where"]
        if not isinstance(where, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "where must be a string",
                f"{path}.where"
            )

        if is_empty_after_trim(where):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "where cannot be empty",
                f"{path}.where"
            )

        parse_expression(where, f"{path}.where")
        normalized["where"] = where.strip()

    elif op_normalized == "map":
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "map requires 'as' field",
                path
            )

        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "map requires 'expr' field",
                path
            )

        as_value = step["as"]
        if not isinstance(as_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as must be a string",
                f"{path}.as"
            )

        if is_empty_after_trim(as_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as cannot be empty",
                f"{path}.as"
            )

        expr = step["expr"]
        if not isinstance(expr, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr must be a string",
                f"{path}.expr"
            )

        if is_empty_after_trim(expr):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr cannot be empty",
                f"{path}.expr"
            )

        parse_expression(expr, f"{path}.expr")
        normalized["as"] = as_value.strip()
        normalized["expr"] = expr.strip()

    elif op_normalized == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if has_from_to and has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename cannot have both from/to and mapping",
                path
            )

        if not has_from_to and not has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename requires either from/to or mapping",
                path
            )

        if has_from_to:
            from_value = step["from"]
            to_value = step["to"]

            if not isinstance(from_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from must be a string",
                    f"{path}.from"
                )

            if not isinstance(to_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to must be a string",
                    f"{path}.to"
                )

            if is_empty_after_trim(from_value):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from cannot be empty",
                    f"{path}.from"
                )

            if is_empty_after_trim(to_value):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to cannot be empty",
                    f"{path}.to"
                )

            normalized["mapping"] = {from_value.strip(): to_value.strip()}
        else:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "mapping must be an object",
                    f"{path}.mapping"
                )

            for key, value in mapping.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping keys must be strings",
                        f"{path}.mapping"
                    )
                if not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping values must be strings",
                        f"{path}.mapping"
                    )

            normalized["mapping"] = mapping

    elif op_normalized == "limit":
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "limit requires 'n' field",
                path
            )

        n = step["n"]
        if not isinstance(n, int) or isinstance(n, bool):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be an integer",
                f"{path}.n"
            )

        if n < 0:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be >= 0",
                f"{path}.n"
            )

        normalized["n"] = n

    elif op_normalized == "branch":
        if "branches" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "branch requires 'branches' field",
                path
            )

        branches = step["branches"]
        if not isinstance(branches, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "branches must be an array",
                f"{path}.branches"
            )

        if len(branches) == 0:
            raise ETLError(
                "MALFORMED_STEP",
                "branches must be non-empty",
                f"{path}.branches"
            )

        otherwise_count = 0
        normalized_branches = []

        for branch_idx, branch in enumerate(branches):
            if not isinstance(branch, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "branch must be an object",
                    f"{path}.branches[{branch_idx}]"
                )

            if "when" not in branch:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "branch requires 'when' field",
                    f"{path}.branches[{branch_idx}]"
                )

            when = branch["when"]
            if not isinstance(when, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "when must be a string",
                    f"{path}.branches[{branch_idx}].when"
                )

            is_otherwise = when.strip() == "otherwise"

            if is_otherwise:
                otherwise_count += 1
                if otherwise_count > 1:
                    raise ETLError(
                        "MALFORMED_STEP",
                        "only one 'otherwise' branch allowed",
                        f"{path}.branches"
                    )
            else:
                if is_empty_after_trim(when):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "when cannot be empty",
                        f"{path}.branches[{branch_idx}].when"
                    )
                parse_expression(when, f"{path}.branches[{branch_idx}].when")

            if "steps" not in branch:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "branch requires 'steps' field",
                    f"{path}.branches[{branch_idx}]"
                )

            branch_steps = branch["steps"]
            if not isinstance(branch_steps, list):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "steps must be an array",
                    f"{path}.branches[{branch_idx}].steps"
                )

            normalized_branch_steps = []
            for step_idx, branch_step in enumerate(branch_steps):
                branch_path = f"{path}.branches[{branch_idx}].steps[{step_idx}]"
                normalized_branch_steps.append(
                    normalize_step_with_path(branch_step, branch_path, current_def)
                )

            normalized_branch = {"when": "otherwise" if is_otherwise else when.strip(), "steps": normalized_branch_steps}

            if "id" in branch:
                branch_id = branch["id"]
                if not isinstance(branch_id, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "id must be a string",
                        f"{path}.branches[{branch_idx}].id"
                    )
                normalized_branch["id"] = branch_id

            normalized_branches.append(normalized_branch)

        for i, branch in enumerate(normalized_branches):
            if branch["when"] == "otherwise" and i != len(normalized_branches) - 1:
                raise ETLError(
                    "MALFORMED_STEP",
                    "'otherwise' branch must be last",
                    f"{path}.branches"
                )

        normalized["branches"] = normalized_branches

        merge = step.get("merge", {"strategy": "concat"})
        if not isinstance(merge, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "merge must be an object",
                f"{path}.merge"
            )

        strategy = merge.get("strategy", "concat")
        if not isinstance(strategy, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "merge.strategy must be a string",
                f"{path}.merge.strategy"
            )

        if strategy != "concat":
            raise ETLError(
                "MALFORMED_STEP",
                f"unsupported merge strategy '{strategy}'",
                f"{path}.merge.strategy"
            )

        normalized["merge"] = {"strategy": "concat"}

    elif op_normalized == "call":
        if "name" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "call requires 'name' field",
                path
            )

        name = step["name"]
        if not isinstance(name, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "name must be a string",
                f"{path}.name"
            )

        if is_empty_after_trim(name):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "name cannot be empty",
                f"{path}.name"
            )

        # Validate name format: ^[A-Za-z_][A-Za-z0-9_]*$
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"invalid name format '{name}'",
                f"{path}.name"
            )

        normalized["name"] = name

        # params defaults to {} if missing
        params = step.get("params", {})
        if not isinstance(params, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "params must be an object",
                f"{path}.params"
            )

        # Validate params values are JSON scalars or arrays (no nested objects)
        for key, value in params.items():
            if not isinstance(key, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "params keys must be strings",
                    f"{path}.params"
                )
            if isinstance(value, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "params values cannot be nested objects",
                    f"{path}.params"
                )
            # Allow scalars and arrays
            if isinstance(value, list):
                # Check array elements are scalars (not objects)
                for elem in value:
                    if isinstance(elem, dict):
                        raise ETLError(
                            "SCHEMA_VALIDATION_FAILED",
                            "params array values cannot contain objects",
                            f"{path}.params"
                        )

        normalized["params"] = params

    result = {"op": normalized["op"]}
    for key in sorted(normalized.keys()):
        if key != "op":
            result[key] = normalized[key]

    return result


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


def execute_select(data: List[Dict[str, Any]], columns: List[str], step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
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


def execute_filter(data: List[Dict[str, Any]], where: str, step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute a filter operation.
    Keep only rows where the expression evaluates to true.
    """
    ast = parse_expression(where, f"pipeline.steps[{step_index}].where")

    result = []
    for row in data:
        try:
            value = evaluate_expression(ast, row, params)
            if value is True:
                result.append(row)
        except Exception:
            # Execution errors during filter just exclude the row
            pass
    return result


def execute_map(data: List[Dict[str, Any]], as_field: str, expr: str, step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute a map operation.
    Add or overwrite a field with the evaluated expression.
    """
    ast = parse_expression(expr, f"pipeline.steps[{step_index}].expr")

    result = []
    for row in data:
        new_row = dict(row)
        try:
            value = evaluate_expression(ast, row, params)
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


def execute_branch(data: List[Dict[str, Any]], branches: List[Dict[str, Any]], step_index: int, params: Optional[Dict[str, Any]] = None, defs: Optional[Dict[str, List[Dict[str, Any]]]] = None, call_stack: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Execute a branch operation.
    Routes rows to branches based on first-match-wins semantics.
    Concatenates results in branch declaration order.
    """
    if call_stack is None:
        call_stack = []
    # Prepare results for each branch
    branch_results = [[] for _ in branches]

    for row in data:
        matched = False
        for branch_idx, branch in enumerate(branches):
            when = branch["when"]

            # Check if this is an 'otherwise' branch
            if when == "otherwise":
                # otherwise matches everything that didn't match earlier
                if not matched:
                    branch_results[branch_idx].append(dict(row))
                    matched = True
            else:
                # Evaluate the condition
                ast = parse_expression(when, f"pipeline.steps[{step_index}].branches[{branch_idx}].when")
                try:
                    value = evaluate_expression(ast, row, params)
                    if value is True:
                        branch_results[branch_idx].append(dict(row))
                        matched = True
                        break  # First-match-wins
                except Exception:
                    # Execution error during evaluation - treat as false
                    pass

    # Execute branch steps for each branch's rows
    final_results = []
    for branch_idx, (branch, branch_rows) in enumerate(zip(branches, branch_results)):
        if branch_rows:
            # Execute the branch's sub-steps
            branch_steps = branch["steps"]
            try:
                # Execute branch steps with proper path context
                result_rows, _, _ = execute_branch_steps(
                    branch_steps,
                    branch_rows,
                    step_index,
                    branch_idx,
                    params,
                    defs,
                    call_stack
                )
                final_results.extend(result_rows)
            except ETLError:
                raise

    return final_results


def execute_call(data: List[Dict[str, Any]], name: str, params: Dict[str, Any], defs: Dict[str, List[Dict[str, Any]]], path: str, call_stack: List[str]) -> List[Dict[str, Any]]:
    """
    Execute a call operation.
    Invokes a sub-pipeline with parameter binding.
    Detects and forbids recursion.
    """
    # Check for recursion
    if name in call_stack:
        raise ETLError(
            "RECURSION_FORBIDDEN",
            f"recursive call to '{name}' detected",
            path
        )

    # Check if definition exists
    if name not in defs:
        raise ETLError(
            "UNKNOWN_DEF",
            f"definition '{name}' not found",
            path
        )

    # Get the definition's steps
    def_steps = defs[name]

    # Execute the definition's steps with the bound params and updated call stack
    new_call_stack = call_stack + [name]
    result = data
    for step_idx, step in enumerate(def_steps):
        step_path = f"defs[{name}].steps[{step_idx}]"
        op = step["op"]

        try:
            if op == "select":
                result = execute_branch_select(result, step["columns"], step_path, params)
            elif op == "filter":
                result = execute_branch_filter(result, step["where"], step_path, params)
            elif op == "map":
                result = execute_branch_map(result, step["as"], step["expr"], step_path, params)
            elif op == "rename":
                result = execute_branch_rename(result, step["mapping"], step_path)
            elif op == "limit":
                result = result[:step["n"]]
            elif op == "branch":
                # Nested branch - pass the current step index context
                result = execute_branch_with_path(result, step["branches"], step_path, params, defs, new_call_stack)
            elif op == "call":
                # Nested call
                nested_name = step["name"]
                nested_params = step.get("params", {})
                result = execute_call(result, nested_name, nested_params, defs, step_path, new_call_stack)
        except ETLError:
            raise
        except Exception as e:
            raise ETLError(
                "EXECUTION_FAILED",
                str(e),
                step_path
            )

    return result


def execute_branch_with_path(data: List[Dict[str, Any]], branches: List[Dict[str, Any]], path: str, params: Optional[Dict[str, Any]] = None, defs: Optional[Dict[str, List[Dict[str, Any]]]] = None, call_stack: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Execute a branch operation with a custom path.
    """
    if call_stack is None:
        call_stack = []
    if defs is None:
        defs = {}

    # Prepare results for each branch
    branch_results = [[] for _ in branches]

    for row in data:
        matched = False
        for branch_idx, branch in enumerate(branches):
            when = branch["when"]

            # Check if this is an 'otherwise' branch
            if when == "otherwise":
                if not matched:
                    branch_results[branch_idx].append(dict(row))
                    matched = True
            else:
                ast = parse_expression(when, f"{path}.branches[{branch_idx}].when")
                try:
                    value = evaluate_expression(ast, row, params)
                    if value is True:
                        branch_results[branch_idx].append(dict(row))
                        matched = True
                        break
                except Exception:
                    pass

    final_results = []
    for branch_idx, (branch, branch_rows) in enumerate(zip(branches, branch_results)):
        if branch_rows:
            branch_steps = branch["steps"]
            try:
                result_rows, _, _ = execute_branch_steps(
                    branch_steps,
                    branch_rows,
                    path,
                    branch_idx,
                    params,
                    defs,
                    call_stack
                )
                final_results.extend(result_rows)
            except ETLError:
                raise

    return final_results


def execute_branch_steps(steps: List[Dict[str, Any]], data: List[Dict[str, Any]], step_index_or_path, branch_idx: int, params: Optional[Dict[str, Any]] = None, defs: Optional[Dict[str, List[Dict[str, Any]]]] = None, call_stack: Optional[List[str]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Execute steps within a branch with proper error path handling.
    step_index_or_path can be an int (for pipeline.steps[k]) or a string path (for defs[Name].steps[k]).
    """
    if call_stack is None:
        call_stack = []
    if defs is None:
        defs = {}

    rows_in = len(data)
    result = data

    for step_idx, step in enumerate(steps):
        op = step["op"]
        # Build path based on whether step_index is int or string
        if isinstance(step_index_or_path, int):
            path = f"pipeline.steps[{step_index_or_path}].branches[{branch_idx}].steps[{step_idx}]"
        else:
            path = f"{step_index_or_path}.branches[{branch_idx}].steps[{step_idx}]"

        try:
            if op == "select":
                result = execute_branch_select(result, step["columns"], path, params)
            elif op == "filter":
                result = execute_branch_filter(result, step["where"], path, params)
            elif op == "map":
                result = execute_branch_map(result, step["as"], step["expr"], path, params)
            elif op == "rename":
                result = execute_branch_rename(result, step["mapping"], path)
            elif op == "limit":
                result = result[:step["n"]]
            elif op == "branch":
                # Nested branch
                if isinstance(step_index_or_path, int):
                    result = execute_branch(result, step["branches"], step_index_or_path, params, defs, call_stack)
                else:
                    result = execute_branch_with_path(result, step["branches"], path, params, defs, call_stack)
            elif op == "call":
                # Call within a branch
                call_name = step["name"]
                call_params = step.get("params", {})
                result = execute_call(result, call_name, call_params, defs, path, call_stack)
        except ETLError:
            raise
        except Exception as e:
            raise ETLError(
                "EXECUTION_FAILED",
                str(e),
                path
            )

    rows_out = len(result)
    return result, rows_in, rows_out


def execute_branch_select(data: List[Dict[str, Any]], columns: List[str], path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute a select operation within a branch.
    """
    result = []
    for row in data:
        new_row = {}
        for col_idx, col in enumerate(columns):
            if col not in row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"column '{col}' not found in row",
                    f"{path}.columns[{col_idx}]"
                )
            new_row[col] = row[col]
        result.append(new_row)
    return result


def execute_branch_filter(data: List[Dict[str, Any]], where: str, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute a filter operation within a branch.
    """
    ast = parse_expression(where, f"{path}.where")

    result = []
    for row in data:
        try:
            value = evaluate_expression(ast, row, params)
            if value is True:
                result.append(row)
        except Exception:
            pass
    return result


def execute_branch_map(data: List[Dict[str, Any]], as_field: str, expr: str, path: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Execute a map operation within a branch.
    """
    ast = parse_expression(expr, f"{path}.expr")

    result = []
    for row in data:
        new_row = dict(row)
        try:
            value = evaluate_expression(ast, row, params)
            new_row[as_field] = value
        except Exception as e:
            raise ETLError(
                "EXECUTION_FAILED",
                f"expression evaluation failed: {str(e)}",
                f"{path}.expr"
            )
        result.append(new_row)
    return result


def execute_branch_rename(data: List[Dict[str, Any]], mapping: Dict[str, str], path: str) -> List[Dict[str, Any]]:
    """
    Execute a rename operation within a branch.
    """
    result = []
    for row in data:
        new_row = dict(row)
        for src_col, tgt_col in mapping.items():
            if src_col not in new_row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"column '{src_col}' not found in row",
                    f"{path}.mapping.{src_col}"
                )
            value = new_row[src_col]
            del new_row[src_col]
            new_row[tgt_col] = value
        result.append(new_row)
    return result


def execute_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]], defs: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Execute the normalized pipeline steps on the dataset.
    Returns the result data, rows_in, and rows_out.
    """
    if defs is None:
        defs = {}

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
            elif op == "branch":
                data = execute_branch(data, step["branches"], step_index, None, defs, [])
            elif op == "call":
                call_name = step["name"]
                call_params = step.get("params", {})
                path = f"pipeline.steps[{step_index}].name"
                data = execute_call(data, call_name, call_params, defs, path, [])
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


def execute_compose_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]], defs: Optional[Dict[str, List[Dict[str, Any]]]] = None, library: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None) -> Tuple[List[Dict[str, Any]], int, int]:
    """
    Execute pipeline steps from compose with library params support.
    Steps may have _lib_params attached from library references.
    Returns the result data, rows_in, and rows_out.
    """
    if defs is None:
        defs = {}
    if library is None:
        library = {}

    rows_in = len(dataset)
    data = dataset

    for step_index, step in enumerate(steps):
        op = step["op"]
        # Get library params if attached (from library reference)
        lib_params = step.get("_lib_params")

        try:
            if op == "select":
                data = execute_select_with_params(data, step["columns"], step_index, lib_params)
            elif op == "filter":
                data = execute_filter_with_params(data, step["where"], step_index, lib_params)
            elif op == "map":
                data = execute_map_with_params(data, step["as"], step["expr"], step_index, lib_params)
            elif op == "rename":
                data = execute_rename(data, step["mapping"], step_index)
            elif op == "limit":
                data = execute_limit(data, step["n"])
            elif op == "branch":
                data = execute_branch(data, step["branches"], step_index, lib_params, defs, [])
            elif op == "call":
                call_name = step["name"]
                call_params = step.get("params", {})
                path = f"pipeline.steps[{step_index}].name"
                data = execute_call(data, call_name, call_params, defs, path, [])
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


def execute_select_with_params(data: List[Dict[str, Any]], columns: List[str], step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Execute select with params support (params not used in select but for consistency)."""
    return execute_select(data, columns, step_index, params)


def execute_filter_with_params(data: List[Dict[str, Any]], where: str, step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Execute filter with params support."""
    ast = parse_expression(where, f"pipeline.steps[{step_index}].where")

    result = []
    for row in data:
        try:
            value = evaluate_expression(ast, row, params)
            if value is True:
                result.append(row)
        except Exception:
            pass
    return result


def execute_map_with_params(data: List[Dict[str, Any]], as_field: str, expr: str, step_index: int, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Execute map with params support."""
    ast = parse_expression(expr, f"pipeline.steps[{step_index}].expr")

    result = []
    for row in data:
        new_row = dict(row)
        try:
            value = evaluate_expression(ast, row, params)
            new_row[as_field] = value
        except Exception as e:
            raise ETLError(
                "EXECUTION_FAILED",
                f"expression evaluation failed: {str(e)}",
                f"pipeline.steps[{step_index}].expr"
            )
        result.append(new_row)
    return result


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

    # Check for required fields - dataset is always required
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

    # Check for mutual exclusivity: compose and pipeline.steps
    has_compose = "compose" in data
    has_pipeline = "pipeline" in data

    if has_compose and has_pipeline:
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            "compose and pipeline are mutually exclusive",
            ""
        )

    if not has_compose and not has_pipeline:
        return create_error_response(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'pipeline' or 'compose'",
            ""
        )

    # Validate and normalize defs (optional)
    defs = None
    if "defs" in data:
        try:
            defs = validate_defs(data["defs"])
        except ETLError as e:
            return create_error_response(e.error_code, e.message, e.path)

    # Validate and normalize library (optional)
    library = None
    if "library" in data:
        try:
            library = validate_library(data["library"])
        except ETLError as e:
            return create_error_response(e.error_code, e.message, e.path)

    # Process based on input type
    if has_compose:
        # Handle compose mode
        try:
            normalized_steps = validate_compose(data["compose"], library if library else {})
        except ETLError as e:
            return create_error_response(e.error_code, e.message, e.path)

        # For normalize mode, remove _lib_params markers from output
        if not execute:
            output_steps = []
            for step in normalized_steps:
                step_copy = dict(step)
                if "_lib_params" in step_copy:
                    del step_copy["_lib_params"]
                output_steps.append(step_copy)
            return create_success_response({"steps": output_steps})

        # Execute with compose params
        try:
            result_data, rows_in, rows_out = execute_compose_pipeline(normalized_steps, data["dataset"], defs, library if library else {})
            return create_execution_response(result_data, rows_in, rows_out)
        except ETLError as e:
            return create_error_response(e.error_code, e.message, e.path)
    else:
        # Handle traditional pipeline mode
        try:
            normalized_steps = validate_pipeline(data["pipeline"])
        except ETLError as e:
            return create_error_response(e.error_code, e.message, e.path)

        # If not executing, return normalized
        if not execute:
            return create_success_response({"steps": normalized_steps})

        # Execute the pipeline
        try:
            result_data, rows_in, rows_out = execute_pipeline(normalized_steps, data["dataset"], defs)
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
