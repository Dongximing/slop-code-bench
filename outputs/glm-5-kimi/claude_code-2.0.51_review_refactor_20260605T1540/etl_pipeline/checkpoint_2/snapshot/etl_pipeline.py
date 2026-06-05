#!/usr/bin/env python3
"""ETL Pipeline Parser - Validates and normalizes ETL pipeline specifications."""

import argparse
import json
import sys
import re
from typing import Any, Dict, List, Optional


# Supported operations and their required fields
SUPPORTED_OPS = {
    "select": ["columns"],
    "filter": ["where"],
    "map": ["as", "expr"],
    "rename": [],  # Special handling for from/to or mapping
    "limit": ["n"],
}

# Valid expression operators (for basic syntax validation)
EXPR_OPERATORS = {
    "+", "-", "*", "/", ">", ">=", "<", "<=", "==", "!=", "and", "or", "not"
}


class ETLError(Exception):
    """Custom exception for ETL pipeline errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = f"ETL_ERROR: {message}"
        self.path = path
        super().__init__(self.message)


def find_unsupported_operator(expr: str) -> Optional[str]:
    """
    Find unsupported operators in expression.
    Returns the unsupported operator string if found, None otherwise.
    """
    # Check for ** (power operator - unsupported)
    if '**' in expr:
        return '**'

    # Check for ^^ (power operator - unsupported)
    if '^^' in expr:
        return '^^'

    # Check for %% (modulo operator - unsupported)
    if '%%' in expr:
        return '%%'

    # Check for consecutive arithmetic operators (like ++, -- when not comparison)
    match = re.search(r'[+\-*/]{2,}', expr)
    if match:
        op = match.group()
        # Exclude valid two-char operators
        if op not in ('>=', '<=', '==', '!=', '&&', '||'):
            return op

    return None


def validate_expression(expr: str) -> tuple:
    """
    Basic validation of expression syntax.
    Returns (is_valid, unsupported_op) tuple.
    """
    if not expr or expr.isspace():
        return False, None

    # Check for unsupported operators
    unsupported = find_unsupported_operator(expr)
    if unsupported:
        return False, unsupported

    # Check for consecutive logical operators
    if re.search(r'\b(and|or|not)\s+(and|or|not)\b', expr, re.IGNORECASE):
        return False, None

    # Check for consecutive comparison operators
    if re.search(r'(>=|<=|==|!=|>|<)\s*(>=|<=|==|!=|>|<)', expr):
        return False, None

    return True, None


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """Normalize a single step object."""
    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "step must be an object",
            f"pipeline.steps[{index}]"
        )

    # Get and normalize operation name
    if "op" not in step:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'op'",
            f"pipeline.steps[{index}]"
        )

    op_raw = step.get("op")
    if not isinstance(op_raw, str):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op must be a string",
            f"pipeline.steps[{index}].op"
        )

    op = op_raw.strip().lower()

    if not op:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "op cannot be empty or whitespace-only",
            f"pipeline.steps[{index}].op"
        )

    # Check if operation is supported
    if op not in SUPPORTED_OPS:
        raise ETLError(
            "UNKNOWN_OP",
            f"unsupported op '{op}'",
            f"pipeline.steps[{index}].op"
        )

    # Create normalized step with op first
    normalized = {"op": op}

    # Process based on operation type
    if op == "select":
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'columns'",
                f"pipeline.steps[{index}]"
            )

        columns = step["columns"]
        if not isinstance(columns, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "columns must be an array",
                f"pipeline.steps[{index}].columns"
            )

        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"column name must be a string",
                    f"pipeline.steps[{index}].columns[{i}]"
                )

        # Preserve column names exactly (spaces are significant)
        normalized["columns"] = columns

    elif op == "filter":
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'where'",
                f"pipeline.steps[{index}]"
            )

        where_raw = step["where"]
        if not isinstance(where_raw, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "where must be a string",
                f"pipeline.steps[{index}].where"
            )

        where = where_raw.strip()
        if not where:
            raise ETLError(
                "BAD_EXPR",
                "where expression is empty or whitespace-only",
                f"pipeline.steps[{index}].where"
            )

        is_valid, unsupported = validate_expression(where)
        if not is_valid:
            if unsupported:
                raise ETLError(
                    "BAD_EXPR",
                    f"unsupported operator '{unsupported}'",
                    f"pipeline.steps[{index}].where"
                )
            else:
                raise ETLError(
                    "BAD_EXPR",
                    "invalid expression syntax",
                    f"pipeline.steps[{index}].where"
                )

        normalized["where"] = where

    elif op == "map":
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'as'",
                f"pipeline.steps[{index}]"
            )

        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'expr'",
                f"pipeline.steps[{index}]"
            )

        as_raw = step["as"]
        if not isinstance(as_raw, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as must be a string",
                f"pipeline.steps[{index}].as"
            )

        as_name = as_raw.strip()
        if not as_name:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "as is empty or whitespace-only",
                f"pipeline.steps[{index}].as"
            )

        expr_raw = step["expr"]
        if not isinstance(expr_raw, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr must be a string",
                f"pipeline.steps[{index}].expr"
            )

        expr = expr_raw.strip()
        if not expr:
            raise ETLError(
                "BAD_EXPR",
                "expr is empty or whitespace-only",
                f"pipeline.steps[{index}].expr"
            )

        is_valid, unsupported = validate_expression(expr)
        if not is_valid:
            if unsupported:
                raise ETLError(
                    "BAD_EXPR",
                    f"unsupported operator '{unsupported}'",
                    f"pipeline.steps[{index}].expr"
                )
            else:
                raise ETLError(
                    "BAD_EXPR",
                    "invalid expression syntax",
                    f"pipeline.steps[{index}].expr"
                )

        normalized["as"] = as_name
        normalized["expr"] = expr

    elif op == "rename":
        # Check for from/to or mapping
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if has_mapping:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "mapping must be an object",
                    f"pipeline.steps[{index}].mapping"
                )

            normalized_mapping = {}
            for key, value in mapping.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping key must be a string",
                        f"pipeline.steps[{index}].mapping"
                    )
                if not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping value must be a string",
                        f"pipeline.steps[{index}].mapping[{key}]"
                    )
                # Preserve mapping keys/values exactly
                normalized_mapping[key] = value

            normalized["mapping"] = normalized_mapping

        elif has_from_to:
            from_raw = step["from"]
            to_raw = step["to"]

            if not isinstance(from_raw, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from must be a string",
                    f"pipeline.steps[{index}].from"
                )

            if not isinstance(to_raw, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to must be a string",
                    f"pipeline.steps[{index}].to"
                )

            from_name = from_raw.strip()
            to_name = to_raw.strip()

            if not from_name:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "from is empty or whitespace-only",
                    f"pipeline.steps[{index}].from"
                )

            if not to_name:
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "to is empty or whitespace-only",
                    f"pipeline.steps[{index}].to"
                )

            # Convert from/to to mapping
            normalized["mapping"] = {from_name: to_name}

        else:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename requires either 'from' and 'to' fields or 'mapping' field",
                f"pipeline.steps[{index}]"
            )

    elif op == "limit":
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'n'",
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

    # Sort remaining fields alphabetically (op is already first)
    sorted_normalized = {"op": normalized["op"]}
    for key in sorted(k for k in normalized.keys() if k != "op"):
        sorted_normalized[key] = normalized[key]

    return sorted_normalized


def validate_and_normalize(data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize the entire pipeline."""
    # Check for pipeline field
    if "pipeline" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'pipeline'",
            "pipeline"
        )

    pipeline = data["pipeline"]
    if not isinstance(pipeline, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "pipeline must be an object",
            "pipeline"
        )

    # Check for steps field
    if "steps" not in pipeline:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'steps'",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "steps must be an array",
            "pipeline.steps"
        )

    # Check for dataset field
    if "dataset" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'dataset'",
            "dataset"
        )

    dataset = data["dataset"]
    if not isinstance(dataset, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "dataset must be an array",
            "dataset"
        )

    # Validate each dataset element is an object
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"dataset[{i}] must be an object",
                f"dataset[{i}]"
            )

    # Normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_steps.append(normalize_step(step, i))

    return {"steps": normalized_steps}


# ============================================================================
# Expression Parser and Evaluator
# ============================================================================

class Token:
    """Represents a token in the expression language."""
    def __init__(self, type_: str, value: Any, pos: int):
        self.type = type_
        self.value = value
        self.pos = pos

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, {self.pos})"


class ExpressionTokenizer:
    """Tokenizer for the expression language."""

    def __init__(self, expr: str):
        self.expr = expr
        self.pos = 0
        self.tokens = []

    def tokenize(self) -> List[Token]:
        """Tokenize the expression string."""
        while self.pos < len(self.expr):
            self._skip_whitespace()
            if self.pos >= len(self.expr):
                break

            char = self.expr[self.pos]

            # String literals
            if char == '"':
                self.tokens.append(self._read_string())
            # Numbers
            elif char.isdigit() or (char == '-' and self.pos + 1 < len(self.expr) and self.expr[self.pos + 1].isdigit()):
                self.tokens.append(self._read_number())
            # Identifiers and keywords
            elif char.isalpha() or char == '_':
                self.tokens.append(self._read_identifier())
            # Two-character operators
            elif self.pos + 1 < len(self.expr) and self.expr[self.pos:self.pos+2] in ('>=', '<=', '==', '!=', '&&', '||'):
                op = self.expr[self.pos:self.pos+2]
                self.tokens.append(Token('OP', op, self.pos))
                self.pos += 2
            # Single-character operators and parentheses
            elif char in '+-*/!><()':
                self.tokens.append(Token('OP', char, self.pos))
                self.pos += 1
            # Unknown character
            else:
                raise ETLError("BAD_EXPR", f"unexpected character '{char}'", "")

        return self.tokens

    def _skip_whitespace(self):
        while self.pos < len(self.expr) and self.expr[self.pos].isspace():
            self.pos += 1

    def _read_string(self) -> Token:
        start = self.pos
        self.pos += 1  # Skip opening quote
        result = []

        while self.pos < len(self.expr):
            char = self.expr[self.pos]
            if char == '"':
                self.pos += 1  # Skip closing quote
                return Token('STRING', ''.join(result), start)
            elif char == '\\':
                self.pos += 1
                if self.pos < len(self.expr):
                    escaped = self.expr[self.pos]
                    if escaped == 'n':
                        result.append('\n')
                    elif escaped == 't':
                        result.append('\t')
                    elif escaped == 'r':
                        result.append('\r')
                    elif escaped == '\\':
                        result.append('\\')
                    elif escaped == '"':
                        result.append('"')
                    else:
                        result.append(escaped)
                    self.pos += 1
            else:
                result.append(char)
                self.pos += 1

        raise ETLError("BAD_EXPR", "unterminated string", "")

    def _read_number(self) -> Token:
        start = self.pos
        result = []

        # Handle negative sign
        if self.expr[self.pos] == '-':
            result.append('-')
            self.pos += 1

        # Integer part
        while self.pos < len(self.expr) and self.expr[self.pos].isdigit():
            result.append(self.expr[self.pos])
            self.pos += 1

        # Decimal part
        if self.pos < len(self.expr) and self.expr[self.pos] == '.':
            result.append('.')
            self.pos += 1
            while self.pos < len(self.expr) and self.expr[self.pos].isdigit():
                result.append(self.expr[self.pos])
                self.pos += 1

        num_str = ''.join(result)
        if '.' in num_str:
            return Token('NUMBER', float(num_str), start)
        else:
            return Token('NUMBER', int(num_str), start)

    def _read_identifier(self) -> Token:
        start = self.pos
        result = []

        while self.pos < len(self.expr) and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            result.append(self.expr[self.pos])
            self.pos += 1

        name = ''.join(result)

        # Check for keywords
        if name == 'true':
            return Token('BOOL', True, start)
        elif name == 'false':
            return Token('BOOL', False, start)
        elif name == 'null':
            return Token('NULL', None, start)
        else:
            return Token('IDENT', name, start)


class ExpressionParser:
    """Parser for the expression language with proper precedence."""

    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def parse(self):
        """Parse the expression and return an AST."""
        if not self.tokens:
            raise ETLError("BAD_EXPR", "empty expression", "")
        result = self._parse_or()
        if self.pos < len(self.tokens):
            raise ETLError("BAD_EXPR", f"unexpected token '{self.tokens[self.pos].value}'", "")
        return result

    def _current(self) -> Optional[Token]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _advance(self):
        self.pos += 1

    def _parse_or(self):
        """Parse || operator (lowest precedence)."""
        left = self._parse_and()

        while self._current() and self._current().type == 'OP' and self._current().value == '||':
            op = self._current()
            self._advance()
            right = self._parse_and()
            left = ('binop', op.value, left, right)

        return left

    def _parse_and(self):
        """Parse && operator."""
        left = self._parse_equality()

        while self._current() and self._current().type == 'OP' and self._current().value == '&&':
            op = self._current()
            self._advance()
            right = self._parse_equality()
            left = ('binop', op.value, left, right)

        return left

    def _parse_equality(self):
        """Parse == and != operators."""
        left = self._parse_comparison()

        while self._current() and self._current().type == 'OP' and self._current().value in ('==', '!='):
            op = self._current()
            self._advance()
            right = self._parse_comparison()
            left = ('binop', op.value, left, right)

        return left

    def _parse_comparison(self):
        """Parse <, <=, >, >= operators."""
        left = self._parse_additive()

        while self._current() and self._current().type == 'OP' and self._current().value in ('<', '<=', '>', '>='):
            op = self._current()
            self._advance()
            right = self._parse_additive()
            left = ('binop', op.value, left, right)

        return left

    def _parse_additive(self):
        """Parse + and - operators."""
        left = self._parse_multiplicative()

        while self._current() and self._current().type == 'OP' and self._current().value in ('+', '-'):
            op = self._current()
            self._advance()
            right = self._parse_multiplicative()
            left = ('binop', op.value, left, right)

        return left

    def _parse_multiplicative(self):
        """Parse * and / operators."""
        left = self._parse_unary()

        while self._current() and self._current().type == 'OP' and self._current().value in ('*', '/'):
            op = self._current()
            self._advance()
            right = self._parse_unary()
            left = ('binop', op.value, left, right)

        return left

    def _parse_unary(self):
        """Parse unary operators (!, -)."""
        if self._current() and self._current().type == 'OP' and self._current().value in ('!', '-'):
            op = self._current()
            self._advance()
            operand = self._parse_unary()
            return ('unary', op.value, operand)

        return self._parse_primary()

    def _parse_primary(self):
        """Parse primary expressions (literals, identifiers, parentheses)."""
        token = self._current()

        if token is None:
            raise ETLError("BAD_EXPR", "unexpected end of expression", "")

        # Parenthesized expression
        if token.type == 'OP' and token.value == '(':
            self._advance()
            expr = self._parse_or()
            if not self._current() or self._current().type != 'OP' or self._current().value != ')':
                raise ETLError("BAD_EXPR", "missing closing parenthesis", "")
            self._advance()
            return expr

        # Literals
        if token.type == 'NUMBER':
            self._advance()
            return ('literal', token.value)
        elif token.type == 'STRING':
            self._advance()
            return ('literal', token.value)
        elif token.type == 'BOOL':
            self._advance()
            return ('literal', token.value)
        elif token.type == 'NULL':
            self._advance()
            return ('literal', None)
        elif token.type == 'IDENT':
            self._advance()
            return ('ident', token.value)

        raise ETLError("BAD_EXPR", f"unexpected token '{token.value}'", "")


class ExpressionEvaluator:
    """Evaluates an expression AST against a row context."""

    def evaluate(self, ast, row: Dict[str, Any]) -> Any:
        """Evaluate the expression AST with the given row context."""
        if ast[0] == 'literal':
            return ast[1]

        elif ast[0] == 'ident':
            name = ast[1]
            return row.get(name)  # Missing identifiers return None

        elif ast[0] == 'unary':
            op = ast[1]
            operand = self.evaluate(ast[2], row)

            if op == '!':
                if operand is None:
                    return True
                return not self._is_truthy(operand)
            elif op == '-':
                if operand is None:
                    return None
                if not isinstance(operand, (int, float)):
                    return None
                return -operand

        elif ast[0] == 'binop':
            op = ast[1]
            left = self.evaluate(ast[2], row)
            right = self.evaluate(ast[3], row)

            return self._eval_binop(op, left, right)

        raise ETLError("BAD_EXPR", f"unknown AST node type: {ast[0]}", "")

    def _is_truthy(self, value: Any) -> bool:
        """Determine if a value is truthy."""
        if value is None:
            return False
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return len(value) > 0
        return True

    def _eval_binop(self, op: str, left: Any, right: Any) -> Any:
        """Evaluate a binary operation."""

        # Logical operators
        if op == '&&':
            if left is None or not self._is_truthy(left):
                return False
            return self._is_truthy(right)

        if op == '||':
            if left is not None and self._is_truthy(left):
                return True
            return self._is_truthy(right) if right is not None else False

        # Arithmetic operators
        if op in ('+', '-', '*', '/'):
            if left is None or right is None:
                return None

            if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                return None

            if op == '+':
                return left + right
            elif op == '-':
                return left - right
            elif op == '*':
                return left * right
            elif op == '/':
                if right == 0:
                    return None
                return left / right

        # Comparison operators
        if op in ('==', '!=', '<', '<=', '>', '>='):
            return self._eval_comparison(op, left, right)

        raise ETLError("BAD_EXPR", f"unsupported operator '{op}'", "")

    def _eval_comparison(self, op: str, left: Any, right: Any) -> bool:
        """Evaluate a comparison operation."""

        # Null comparisons return false
        if left is None or right is None:
            return False

        # Type mismatch returns false
        if type(left) != type(right):
            # Special case: int and float are compatible
            if not (isinstance(left, (int, float)) and isinstance(right, (int, float))):
                return False

        # Equality
        if op == '==':
            return left == right
        elif op == '!=':
            return left != right

        # Ordering comparisons
        if op in ('<', '<=', '>', '>='):
            try:
                if op == '<':
                    return left < right
                elif op == '<=':
                    return left <= right
                elif op == '>':
                    return left > right
                elif op == '>=':
                    return left >= right
            except TypeError:
                return False

        return False


def parse_and_evaluate_expression(expr: str, row: Dict[str, Any], path: str) -> Any:
    """Parse and evaluate an expression with error handling."""
    try:
        tokenizer = ExpressionTokenizer(expr)
        tokens = tokenizer.tokenize()
        parser = ExpressionParser(tokens)
        ast = parser.parse()
        evaluator = ExpressionEvaluator()
        return evaluator.evaluate(ast, row)
    except ETLError:
        raise
    except Exception as e:
        raise ETLError("BAD_EXPR", str(e), path)


# ============================================================================
# Pipeline Execution
# ============================================================================

def execute_step(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a single pipeline step on the dataset."""

    op = step["op"]

    if op == "select":
        return execute_select(step, dataset, step_index)
    elif op == "filter":
        return execute_filter(step, dataset, step_index)
    elif op == "map":
        return execute_map(step, dataset, step_index)
    elif op == "rename":
        return execute_rename(step, dataset, step_index)
    elif op == "limit":
        return execute_limit(step, dataset, step_index)
    else:
        raise ETLError("EXECUTION_FAILED", f"unknown operation '{op}'", f"pipeline.steps[{step_index}]")


def execute_select(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a select operation."""
    columns = step["columns"]
    result = []

    for row_idx, row in enumerate(dataset):
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


def execute_filter(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a filter operation."""
    where = step["where"]
    path = f"pipeline.steps[{step_index}].where"
    result = []

    for row in dataset:
        try:
            value = parse_and_evaluate_expression(where, row, path)
            if value is True:
                result.append(row.copy())
        except ETLError:
            raise

    return result


def execute_map(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a map operation."""
    as_name = step["as"]
    expr = step["expr"]
    path = f"pipeline.steps[{step_index}].expr"
    result = []

    for row in dataset:
        new_row = row.copy()
        value = parse_and_evaluate_expression(expr, row, path)
        new_row[as_name] = value
        result.append(new_row)

    return result


def execute_rename(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a rename operation."""
    mapping = step["mapping"]
    result = []

    for row in dataset:
        new_row = row.copy()

        # Apply mappings in iteration order
        for src, dst in mapping.items():
            if src not in new_row:
                raise ETLError(
                    "MISSING_COLUMN",
                    f"column '{src}' not found in row",
                    f"pipeline.steps[{step_index}].mapping.{src}"
                )

            value = new_row[src]
            del new_row[src]
            new_row[dst] = value

        result.append(new_row)

    return result


def execute_limit(step: Dict[str, Any], dataset: List[Dict[str, Any]], step_index: int) -> List[Dict[str, Any]]:
    """Execute a limit operation."""
    n = step["n"]
    return dataset[:n]


def execute_pipeline(steps: List[Dict[str, Any]], dataset: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Execute the entire pipeline."""
    result = dataset

    for i, step in enumerate(steps):
        result = execute_step(step, result, i)

    return result


def main():
    """Main entry point for the ETL pipeline parser."""
    parser = argparse.ArgumentParser(description='ETL Pipeline Parser')
    parser.add_argument('--execute', action='store_true', default=False,
                        help='Execute the pipeline instead of just normalizing')
    args = parser.parse_args()

    try:
        # Read JSON from STDIN
        input_data = sys.stdin.read()
        if not input_data.strip():
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "empty input",
                ""
            )

        try:
            data = json.loads(input_data)
        except json.JSONDecodeError as e:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"invalid JSON: {str(e)}",
                ""
            )

        if not isinstance(data, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "input must be a JSON object",
                ""
            )

        # Validate and normalize
        normalized = validate_and_normalize(data)

        if not args.execute:
            # Checkpoint 1 behavior: return normalized
            output = {
                "status": "ok",
                "normalized": normalized
            }
        else:
            # Checkpoint 2 behavior: execute the pipeline
            dataset = data.get("dataset", [])
            result = execute_pipeline(normalized["steps"], dataset)

            output = {
                "status": "ok",
                "data": result,
                "metrics": {
                    "rows_in": len(dataset),
                    "rows_out": len(result)
                }
            }

        print(json.dumps(output, indent=2))
        sys.exit(0)

    except ETLError as e:
        # Output error response
        output = {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }

        print(json.dumps(output, indent=2))
        sys.exit(1)

    except Exception as e:
        # Unexpected error
        output = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: unexpected error: {str(e)}",
            "path": ""
        }

        print(json.dumps(output, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
