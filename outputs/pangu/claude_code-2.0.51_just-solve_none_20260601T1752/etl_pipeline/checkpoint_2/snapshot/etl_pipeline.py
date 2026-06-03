#!/usr/bin/env python3
"""
ETL Pipeline Executor

Reads a JSON pipeline specification from STDIN, validates/normalizes,
and either:
  - Returns normalized pipeline (--execute=false, default)
  - Executes the pipeline on the dataset and returns data + metrics (--execute=true)
"""

import argparse
import json
import operator
import re
import sys
from typing import Any


# Valid operations and their required fields
VALID_OPS = {
    "select": {"required": {"columns"}, "optional": set()},
    "filter": {"required": {"where"}, "optional": set()},
    "map": {"required": {"as", "expr"}, "optional": set()},
    "rename": {"required": set(), "optional": set(), "exclusive_groups": [
        {"from", "to"},
        {"mapping"}
    ]},
    "limit": {"required": {"n"}, "optional": set()},
}


# Parser for expression validation and evaluation
class ExprParser:
    """
    Simple recursive descent parser for ETL expressions.
    Supports: literals, identifiers, arithmetic, comparison, boolean, parentheses.
    """

    def __init__(self, expr: str):
        self.expr = expr.replace(' ', '')  # Remove spaces
        self.pos = 0
        self.length = len(self.expr)

    def parse_atom(self) -> Any:
        """Parse a literal (number, string, true, false, null) or identifier."""
        if self.pos >= self.length:
            raise ValueError("unexpected end of expression")

        ch = self.expr[self.pos]

        # String literal: "..."
        if ch == '"':
            return self.parse_string()

        # Boolean true
        if self.expr.startswith('true', self.pos):
            self.pos += 4
            return True

        # Boolean false
        if self.expr.startswith('false', self.pos):
            self.pos += 5
            return False

        # Null
        if self.expr.startswith('null', self.pos):
            self.pos += 4
            return None

        # Number (integer or float)
        if ch.isdigit() or (ch == '-' and self.pos + 1 < self.length and self.expr[self.pos + 1].isdigit()):
            return self.parse_number()

        # Identifier (field name: a-z, A-Z, _, starting with letter or underscore)
        if ch.isalpha() or ch == '_':
            return self.parse_identifier()

        # Parenthesized expression
        if ch == '(':
            self.pos += 1
            expr = self.parse_expr(0)
            if self.pos >= self.length or self.expr[self.pos] != ')':
                raise ValueError("missing closing parenthesis")
            self.pos += 1
            return expr

        raise ValueError(f"unexpected character: {ch}")

    def parse_string(self) -> str:
        """Parse a double-quoted string literal."""
        if self.expr[self.pos] != '"':
            raise ValueError("expected string literal")
        self.pos += 1
        start = self.pos
        while self.pos < self.length and self.expr[self.pos] != '"':
            self.pos += 1
        if self.pos >= self.length:
            raise ValueError("unterminated string literal")
        result = self.expr[start:self.pos]
        self.pos += 1  # Skip closing quote
        return result

    def parse_number(self) -> (int, float):
        """Parse an integer or floating-point number."""
        start = self.pos
        if self.expr[self.pos] == '-':
            self.pos += 1
        while self.pos < self.length and self.expr[self.pos].isdigit():
            self.pos += 1
        if self.pos < self.length and self.expr[self.pos] == '.':
            self.pos += 1
            while self.pos < self.length and self.expr[self.pos].isdigit():
                self.pos += 1
        num_str = self.expr[start:self.pos]
        if '.' in num_str:
            return float(num_str)
        return int(num_str)

    def parse_identifier(self) -> str:
        """Parse an identifier (field name)."""
        start = self.pos
        while self.pos < self.length and (self.expr[self.pos].isalnum() or self.expr[self.pos] == '_'):
            self.pos += 1
        return self.expr[start:self.pos]

    def parse_unary(self) -> Any:
        """Parse unary expressions: ! expr, - expr"""
        if self.pos < self.length and self.expr[self.pos] == '!':
            self.pos += 1
            operand = self.parse_unary()
            return ('!', operand)
        if self.pos < self.length and self.expr[self.pos] == '-':
            self.pos += 1
            operand = self.parse_unary()
            return ('neg', operand)
        return self.parse_atom()

    def parse_mul_div(self) -> Any:
        """Parse * and / operations"""
        expr = self.parse_unary()
        while self.pos < self.length:
            if self.expr[self.pos] == '*':
                self.pos += 1
                right = self.parse_unary()
                expr = ('*', expr, right)
            elif self.expr[self.pos] == '/':
                self.pos += 1
                right = self.parse_unary()
                expr = ('/', expr, right)
            else:
                break
        return expr

    def parse_add_sub(self) -> Any:
        """Parse + and - operations"""
        expr = self.parse_mul_div()
        while self.pos < self.length:
            if self.expr[self.pos] == '+':
                self.pos += 1
                right = self.parse_mul_div()
                expr = ('+', expr, right)
            elif self.expr[self.pos] == '-':
                self.pos += 1
                right = self.parse_mul_div()
                expr = ('-', expr, right)
            else:
                break
        return expr

    def parse_cmp(self) -> Any:
        """Parse comparison operations"""
        expr = self.parse_add_sub()
        while self.pos < self.length:
            if self.expr[self.pos] == '<':
                if self.pos + 1 < self.length and self.expr[self.pos + 1] == '=':
                    self.pos += 2
                    right = self.parse_add_sub()
                    expr = ('<=', expr, right)
                else:
                    self.pos += 1
                    right = self.parse_add_sub()
                    expr = ('<', expr, right)
            elif self.expr[self.pos] == '>':
                if self.pos + 1 < self.length and self.expr[self.pos + 1] == '=':
                    self.pos += 2
                    right = self.parse_add_sub()
                    expr = ('>=', expr, right)
                else:
                    self.pos += 1
                    right = self.parse_add_sub()
                    expr = ('>', expr, right)
            elif self.expr.startswith('==', self.pos):
                self.pos += 2
                right = self.parse_add_sub()
                expr = ('==', expr, right)
            elif self.expr.startswith('!=', self.pos):
                self.pos += 2
                right = self.parse_add_sub()
                expr = ('!=', expr, right)
            else:
                break
        return expr

    def parse_and(self) -> Any:
        """Parse && operations"""
        expr = self.parse_cmp()
        while self.pos < self.length:
            if self.expr.startswith('&&', self.pos):
                self.pos += 2
                right = self.parse_cmp()
                expr = ('&&', expr, right)
            elif self.expr.startswith('and', self.pos):
                # Allow 'and' as an alternative to '&&'
                self.pos += 3
                right = self.parse_cmp()
                expr = ('&&', expr, right)
            else:
                break
        return expr

    def parse_or(self) -> Any:
        """Parse || operations"""
        expr = self.parse_and()
        while self.pos < self.length:
            if self.expr.startswith('||', self.pos):
                self.pos += 2
                right = self.parse_and()
                expr = ('||', expr, right)
            elif self.expr.startswith('or', self.pos):
                # Allow 'or' as an alternative to '||'
                self.pos += 2
                right = self.parse_and()
                expr = ('||', expr, right)
            else:
                break
        return expr

    def parse_expr(self, min_precedence=0) -> Any:
        """Parse an expression with proper precedence."""
        return self.parse_or()

    def parse(self) -> Any:
        """Parse the entire expression."""
        try:
            result = self.parse_expr()
            if self.pos < self.length:
                raise ValueError(f"unexpected trailing characters: {self.expr[self.pos:]}")
            return result
        except ValueError as e:
            raise ValueError(f"expression parse error: {e}")


class ExprEvaluator:
    """
    Evaluate an AST produced by ExprParser against a row (dict).
    """

    def __init__(self, ast):
        self.ast = ast

    def evaluate(self, row: dict) -> Any:
        """Evaluate the AST against a single row."""
        return self._eval(self.ast, row)

    def _eval(self, node, row):
        if isinstance(node, (int, float, str, bool, type(None))):
            return node

        if isinstance(node, tuple):
            op = node[0]

            if op == '!':
                val = self._eval(node[1], row)
                return not self._to_bool(val)

            if op == 'neg':
                val = self._eval(node[1], row)
                if val is None:
                    return None
                if not isinstance(val, (int, float)):
                    return None
                return -val

            if op in ('+', '-', '*', '/'):
                left = self._eval(node[1], row)
                right = self._eval(node[2], row)
                # Null handling: any operand null -> null
                if left is None or right is None:
                    return None
                if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
                    return None
                if op == '+':
                    return left + right
                if op == '-':
                    return left - right
                if op == '*':
                    return left * right
                if op == '/':
                    if right == 0:
                        return None
                    return left / right

            if op in ('==', '!=', '<', '<=', '>', '>='):
                left = self._eval(node[1], row)
                right = self._eval(node[2], row)
                # Null in comparisons returns false
                if left is None or right is None:
                    return op == '!='
                # Type mismatch returns false
                if type(left) != type(right):
                    return False
                if op == '==':
                    return left == right
                if op == '!=':
                    return left != right
                if op == '<':
                    return left < right
                if op == '<=':
                    return left <= right
                if op == '>':
                    return left > right
                if op == '>=':
                    return left >= right

            if op == '&&':
                left = self._eval(node[1], row)
                if not self._to_bool(left):
                    return False
                right = self._eval(node[2], row)
                return self._to_bool(right)

            if op == '||':
                left = self._eval(node[1], row)
                if self._to_bool(left):
                    return True
                right = self._eval(node[2], row)
                return self._to_bool(right)

            raise ValueError(f"unknown operator: {op}")

        # String identifier -> field lookup
        if isinstance(node, str):
            return row.get(node)

        raise ValueError(f"invalid AST node: {node}")

    @staticmethod
    def _to_bool(val) -> bool:
        """Convert value to boolean; None is falsy."""
        return bool(val)


def is_valid_expression(expr: str, allow_unknown_ops=True) -> bool:
    """
    Check if an expression has valid syntax using ExprParser.
    Returns True if the expression parses successfully.
    """
    if not expr or not isinstance(expr, str):
        return False

    expr = expr.strip()
    if not expr:
        return False

    try:
        parser = ExprParser(expr)
        parser.parse()
        return True
    except Exception:
        return False


def validate_and_normalize_step(step: dict[str, Any], index: int) -> dict[str, Any]:
    """
    Validate and normalize a single step.

    Args:
        step: The step object to validate
        index: The 0-based index of the step (for error messages)

    Returns:
        The normalized step object

    Raises:
        ValueError: With message, error_code, and path attributes for errors
    """
    # Check op field exists
    if "op" not in step:
        raise ValueError(
            "ETL_ERROR: missing required field 'op'",
            "SCHEMA_VALIDATION_FAILED",
            f"pipeline.steps[{index}].op"
        )

    op_raw = step["op"]

    # Normalize op: trim and lowercase
    if not isinstance(op_raw, str):
        raise ValueError(
            f"ETL_ERROR: op must be a string, got {type(op_raw).__name__}",
            "SCHEMA_VALIDATION_FAILED",
            f"pipeline.steps[{index}].op"
        )

    op = op_raw.strip().lower()

    if not op:
        raise ValueError(
            "ETL_ERROR: op cannot be empty or whitespace-only",
            "SCHEMA_VALIDATION_FAILED",
            f"pipeline.steps[{index}].op"
        )

    # Check if op is valid
    if op not in VALID_OPS:
        raise ValueError(
            f"ETL_ERROR: unsupported op '{op}'",
            "UNKNOWN_OP",
            f"pipeline.steps[{index}].op"
        )

    op_config = VALID_OPS[op]

    # Collect step data (excluding op and unknown keys)
    step_data = {}

    for key, value in step.items():
        if key == "op":
            continue

        # Drop unknown keys
        if key not in op_config["required"] and key not in op_config["optional"]:
            # Check if it's part of an exclusive group
            in_exclusive_group = False
            for group in op_config.get("exclusive_groups", []):
                if key in group:
                    in_exclusive_group = True
                    break
            if not in_exclusive_group:
                continue

        step_data[key] = value

    # Validate exclusive groups for rename
    if op == "rename":
        has_from_to = "from" in step_data and "to" in step_data
        has_mapping = "mapping" in step_data

        if has_from_to and has_mapping:
            raise ValueError(
                "ETL_ERROR: rename cannot have both 'from'/'to' and 'mapping'",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}]"
            )
        if not has_from_to and not has_mapping:
            raise ValueError(
                "ETL_ERROR: rename requires either 'from'/'to' or 'mapping'",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}]"
            )

    # Validate required fields
    for field in op_config["required"]:
        # For rename, check exclusive group
        if op == "rename":
            if not ("from" in step_data and "to" in step_data) and "mapping" not in step_data:
                raise ValueError(
                    "ETL_ERROR: rename requires either 'from'/'to' or 'mapping'",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}]"
                )
            continue

        if field not in step_data:
            raise ValueError(
                f"ETL_ERROR: missing required field '{field}'",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].{field}"
            )

    # Normalize the step data
    normalized = {"op": op}

    if op == "select":
        columns = step_data["columns"]
        if not isinstance(columns, list):
            raise ValueError(
                "ETL_ERROR: columns must be an array",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].columns"
            )
        # Preserve column names exactly as-is (no trimming)
        normalized["columns"] = columns

    elif op == "filter":
        where = step_data["where"]
        if not isinstance(where, str):
            raise ValueError(
                "ETL_ERROR: where must be a string",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].where"
            )
        where = where.strip()
        if not where:
            raise ValueError(
                "ETL_ERROR: where cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].where"
            )
        if not is_valid_expression(where):
            raise ValueError(
                f"ETL_ERROR: invalid expression syntax in where clause",
                "BAD_EXPR",
                f"pipeline.steps[{index}].where"
            )
        normalized["where"] = where

    elif op == "map":
        as_field = step_data["as"]
        if not isinstance(as_field, str):
            raise ValueError(
                "ETL_ERROR: as must be a string",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].as"
            )
        as_field = as_field.strip()
        if not as_field:
            raise ValueError(
                "ETL_ERROR: as cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].as"
            )
        normalized["as"] = as_field

        expr = step_data["expr"]
        if not isinstance(expr, str):
            raise ValueError(
                "ETL_ERROR: expr must be a string",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].expr"
            )
        expr = expr.strip()
        if not expr:
            raise ValueError(
                "ETL_ERROR: expr cannot be empty",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].expr"
            )
        if not is_valid_expression(expr):
            raise ValueError(
                "ETL_ERROR: invalid expression syntax in expr",
                "BAD_EXPR",
                f"pipeline.steps[{index}].expr"
            )
        normalized["expr"] = expr

    elif op == "rename":
        if "from" in step_data and "to" in step_data:
            from_val = step_data["from"]
            to_val = step_data["to"]
            if not isinstance(from_val, str):
                raise ValueError(
                    "ETL_ERROR: from must be a string",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}].from"
                )
            if not isinstance(to_val, str):
                raise ValueError(
                    "ETL_ERROR: to must be a string",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}].to"
                )
            # Trim from/to before converting to mapping
            from_val = from_val.strip()
            to_val = to_val.strip()
            if not from_val:
                raise ValueError(
                    "ETL_ERROR: from cannot be empty",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}].from"
                )
            if not to_val:
                raise ValueError(
                    "ETL_ERROR: to cannot be empty",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}].to"
                )
            normalized["mapping"] = {from_val: to_val}
        else:
            mapping = step_data["mapping"]
            if not isinstance(mapping, dict):
                raise ValueError(
                    "ETL_ERROR: mapping must be an object",
                    "SCHEMA_VALIDATION_FAILED",
                    f"pipeline.steps[{index}].mapping"
                )
            # Preserve mapping keys/values exactly
            normalized["mapping"] = mapping

    elif op == "limit":
        n = step_data["n"]
        if not isinstance(n, int):
            raise ValueError(
                "ETL_ERROR: n must be an integer",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].n"
            )
        if n < 0:
            raise ValueError(
                "ETL_ERROR: n must be >= 0",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{index}].n"
            )
        normalized["n"] = n

    # Sort keys: op first, then others alphabetically
    sorted_normalized = {"op": normalized["op"]}
    for key in sorted(normalized.keys()):
        if key != "op":
            sorted_normalized[key] = normalized[key]

    return sorted_normalized


def validate_and_normalize_pipeline(pipeline: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and normalize the entire pipeline.

    Args:
        pipeline: The pipeline object containing a "steps" array

    Returns:
        The normalized pipeline with steps

    Raises:
        ValueError: With message, error_code, and path attributes for errors
    """
    if not isinstance(pipeline, dict):
        raise ValueError(
            "ETL_ERROR: pipeline must be an object",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline"
        )

    if "steps" not in pipeline:
        raise ValueError(
            "ETL_ERROR: pipeline must have a 'steps' field",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ValueError(
            "ETL_ERROR: steps must be an array",
            "SCHEMA_VALIDATION_FAILED",
            "pipeline.steps"
        )

    normalized_steps = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(
                f"ETL_ERROR: step {i} must be an object",
                "SCHEMA_VALIDATION_FAILED",
                f"pipeline.steps[{i}]"
            )
        normalized_step = validate_and_normalize_step(step, i)
        normalized_steps.append(normalized_step)

    return {"steps": normalized_steps}


def validate_dataset(dataset: Any) -> None:
    """
    Validate the dataset field.

    Args:
        dataset: The dataset value to validate

    Raises:
        ValueError: With message, error_code, and path attributes for errors
    """
    if not isinstance(dataset, list):
        raise ValueError(
            "ETL_ERROR: dataset must be an array",
            "SCHEMA_VALIDATION_FAILED",
            "dataset"
        )

    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            raise ValueError(
                f"ETL_ERROR: dataset[{i}] must be an object",
                "SCHEMA_VALIDATION_FAILED",
                f"dataset[{i}]"
            )


def execute_select_step(data: list[dict], step: dict[str, Any]) -> list[dict]:
    """Execute a select step, returning only specified columns."""
    columns = step["columns"]
    # Check all columns exist before processing
    for col in columns:
        # Check if any row is missing this column
        for row in data:
            if col not in row:
                raise ValueError(
                    f"ETL_ERROR: column '{col}' not found in row",
                    "MISSING_COLUMN",
                    f"pipeline.steps[{step.get('_index', '')}].columns[{columns.index(col)}]"
                )

    result = []
    for row in data:
        new_row = {col: row[col] for col in columns}
        result.append(new_row)
    return result


def execute_filter_step(data: list[dict], step: dict[str, Any]) -> list[dict]:
    """Execute a filter step, returning rows where condition is true."""
    where_expr = step["where"]
    try:
        parser = ExprParser(where_expr)
        ast = parser.parse()
        evaluator = ExprEvaluator(ast)
    except Exception as e:
        raise ValueError(
            f"ETL_ERROR: failed to parse filter expression: {e}",
            "BAD_EXPR",
            f"pipeline.steps[{step.get('_index', '')}].where"
        )

    result = []
    for row in data:
        try:
            val = evaluator.evaluate(row)
            if evaluator._to_bool(val):
                result.append(row)
        except Exception as e:
            raise ValueError(
                f"ETL_ERROR: execution failed: {e}",
                "EXECUTION_FAILED",
                f"pipeline.steps[{step.get('_index', '')}].where"
            )
    return result


def execute_map_step(data: list[dict], step: dict[str, Any]) -> list[dict]:
    """Execute a map step, evaluating expression and adding/updating field."""
    as_field = step["as"]
    expr_str = step["expr"]

    try:
        parser = ExprParser(expr_str)
        ast = parser.parse()
        evaluator = ExprEvaluator(ast)
    except Exception as e:
        raise ValueError(
            f"ETL_ERROR: failed to parse map expression: {e}",
            "BAD_EXPR",
            f"pipeline.steps[{step.get('_index', '')}].expr"
        )

    result = []
    for row in data:
        new_row = dict(row)
        try:
            val = evaluator.evaluate(row)
            new_row[as_field] = val
        except Exception as e:
            raise ValueError(
                f"ETL_ERROR: execution failed: {e}",
                "EXECUTION_FAILED",
                f"pipeline.steps[{step.get('_index', '')}].expr"
            )
        result.append(new_row)
    return result


def execute_rename_step(data: list[dict], step: dict[str, Any]) -> list[dict]:
    """Execute a rename step, renaming columns."""
    mapping = step["mapping"]

    # Check all source columns exist before processing
    for src in mapping:
        for row in data:
            if src not in row:
                raise ValueError(
                    f"ETL_ERROR: column '{src}' not found in row",
                    "MISSING_COLUMN",
                    f"pipeline.steps[{step.get('_index', '')}].mapping.{src}"
                )

    result = []
    for row in data:
        new_row = dict(row)
        # Apply mappings in iteration order (later overwrites earlier if conflict)
        for src, dst in mapping.items():
            if src in new_row:
                val = new_row.pop(src)
                new_row[dst] = val
        result.append(new_row)
    return result


def execute_limit_step(data: list[dict], step: dict[str, Any]) -> list[dict]:
    """Execute a limit step, returning at most n rows."""
    n = step["n"]
    return data[:n]


def execute_pipeline(dataset: list[dict], pipeline: dict[str, Any]) -> tuple[list[dict], dict]:
    """
    Execute the pipeline on the dataset and return (data, metrics).
    """
    rows_in = len(dataset)
    data = list(dataset)  # Start with a copy

    for i, step in enumerate(pipeline["steps"]):
        # Store step index for error messages
        step["_index"] = i
        op = step["op"]

        if op == "select":
            data = execute_select_step(data, step)
        elif op == "filter":
            data = execute_filter_step(data, step)
        elif op == "map":
            data = execute_map_step(data, step)
        elif op == "rename":
            data = execute_rename_step(data, step)
        elif op == "limit":
            data = execute_limit_step(data, step)
        else:
            raise ValueError(
                f"ETL_ERROR: unsupported op '{op}'",
                "UNKNOWN_OP",
                f"pipeline.steps[{i}].op"
            )

    metrics = {
        "rows_in": rows_in,
        "rows_out": len(data)
    }

    return data, metrics


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="ETL Pipeline Executor"
    )
    parser.add_argument(
        "--execute",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Execute the pipeline on the dataset (default: false, only validate/normalize)"
    )
    args = parser.parse_args()

    try:
        # Read JSON from STDIN
        input_data = json.load(sys.stdin)

        # Validate top-level structure
        if not isinstance(input_data, dict):
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": "ETL_ERROR: root must be an object",
                "path": ""
            }), flush=True)
            sys.exit(1)

        # Check required fields
        if "pipeline" not in input_data:
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": "ETL_ERROR: missing required field 'pipeline'",
                "path": "pipeline"
            }), flush=True)
            sys.exit(1)

        if "dataset" not in input_data:
            print(json.dumps({
                "status": "error",
                "error_code": "SCHEMA_VALIDATION_FAILED",
                "message": "ETL_ERROR: missing required field 'dataset'",
                "path": "dataset"
            }), flush=True)
            sys.exit(1)

        # Validate and normalize pipeline
        normalized_pipeline = validate_and_normalize_pipeline(input_data["pipeline"])

        # Validate dataset
        validate_dataset(input_data["dataset"])

        if not args.execute:
            # Default behavior: return normalized pipeline
            result = {
                "status": "ok",
                "normalized": normalized_pipeline
            }
            print(json.dumps(result), flush=True)
            sys.exit(0)

        # Execute mode: run the pipeline on the dataset
        data, metrics = execute_pipeline(input_data["dataset"], normalized_pipeline)
        result = {
            "status": "ok",
            "data": data,
            "metrics": metrics
        }
        print(json.dumps(result), flush=True)
        sys.exit(0)

    except ValueError as e:
        # Extract error details
        args_e = e.args
        if len(args_e) >= 3:
            message, error_code, path = args_e[0], args_e[1], args_e[2]
        else:
            message = str(args_e[0])
            error_code = "SCHEMA_VALIDATION_FAILED"
            path = ""

        error_response = {
            "status": "error",
            "error_code": error_code,
            "message": message,
            "path": path
        }
        print(json.dumps(error_response), flush=True)
        sys.exit(1)

    except json.JSONDecodeError as e:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: invalid JSON: {e}",
            "path": ""
        }
        print(json.dumps(error_response), flush=True)
        sys.exit(1)

    except Exception as e:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: unexpected error: {e}",
            "path": ""
        }
        print(json.dumps(error_response), flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
