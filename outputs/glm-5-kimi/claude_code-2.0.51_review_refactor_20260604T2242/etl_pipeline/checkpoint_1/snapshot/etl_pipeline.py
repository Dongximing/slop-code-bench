#!/usr/bin/env python3
"""ETL Pipeline Parser - Validates and normalizes ETL pipeline specifications."""

import json
import sys
import re
from typing import Any, Dict, List, Optional, Tuple


class ETLError(Exception):
    """Custom exception for ETL errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


def validate_json_structure(data: Any) -> None:
    """Validate the basic JSON structure of the input."""
    if not isinstance(data, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: input must be a JSON object",
            "root"
        )

    # Check required fields
    if "pipeline" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'pipeline' field is required",
            "pipeline"
        )

    if "dataset" not in data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' field is required",
            "dataset"
        )

    pipeline = data["pipeline"]
    if not isinstance(pipeline, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'pipeline' must be an object",
            "pipeline"
        )

    if "steps" not in pipeline:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'steps' field is required in pipeline",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'steps' must be an array",
            "pipeline.steps"
        )

    dataset = data["dataset"]
    if not isinstance(dataset, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' must be an array",
            "dataset"
        )

    # Validate dataset elements are objects
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: dataset[{i}] must be an object",
                f"dataset[{i}]"
            )


def is_empty_after_trim(value: str) -> bool:
    """Check if a string is empty or whitespace-only after trimming."""
    return not value or not value.strip()


def validate_expression(expr: str, path: str) -> None:
    """Validate expression syntax for filter and map operations."""
    if is_empty_after_trim(expr):
        raise ETLError(
            "BAD_EXPR",
            "ETL_ERROR: expression cannot be empty",
            path
        )

    expr = expr.strip()

    # Check for obvious syntax errors - consecutive operators
    # Operators: +, -, *, /, >, >=, <, <=, ==, !=, and, or, not
    # We need to check for invalid consecutive operator characters

    # Tokenize to check for consecutive operators
    # Simple approach: check for consecutive operator symbols
    operators_pattern = r'[+\-*/><=!]'

    # Find all operator symbols
    tokens = re.split(r'\s+', expr)

    # Check for invalid consecutive operators like ^^, ***, etc.
    # We need to be careful: ** is not valid in this spec, but we check for obvious errors
    invalid_patterns = [
        r'\^\^',           # ^^
        r'[+\-*/]{2,}',    # consecutive arithmetic operators like ++, --, **, etc.
        r'[><=!]{3,}',     # three or more comparison characters
        r'(?:>>|<<)',      # >> or << (not valid operators)
    ]

    for pattern in invalid_patterns:
        if re.search(pattern, expr):
            raise ETLError(
                "BAD_EXPR",
                f"ETL_ERROR: invalid expression syntax",
                path
            )

    # Check for consecutive comparison/logical operators
    # Valid: >=, <=, ==, !=
    # Invalid: ><, <>, =>, =<, ==!, !==, etc.
    invalid_combo_pattern = r'[><=!]{2}(?:[><=!]|(?![=]))'

    # More specific check: consecutive operators not forming valid compounds
    # We'll check by removing valid compound operators and looking for leftovers

    # Replace valid compound operators with space
    test_expr = expr
    test_expr = re.sub(r'>=', ' ', test_expr)
    test_expr = re.sub(r'<=', ' ', test_expr)
    test_expr = re.sub(r'==', ' ', test_expr)
    test_expr = re.sub(r'!=', ' ', test_expr)

    # Now check if there are two or more operator characters adjacent
    if re.search(r'[+\-*/><=!]{2,}', test_expr):
        raise ETLError(
            "BAD_EXPR",
            "ETL_ERROR: invalid expression syntax",
            path
        )

    # Check for balanced parentheses
    paren_count = 0
    for char in expr:
        if char == '(':
            paren_count += 1
        elif char == ')':
            paren_count -= 1
            if paren_count < 0:
                raise ETLError(
                    "BAD_EXPR",
                    "ETL_ERROR: unbalanced parentheses in expression",
                    path
                )

    if paren_count != 0:
        raise ETLError(
            "BAD_EXPR",
            "ETL_ERROR: unbalanced parentheses in expression",
            path
        )

    # Basic check: expression should have at least one valid token
    # Remove operators and parentheses, check if anything remains
    check_expr = expr
    check_expr = re.sub(r'\(', ' ', check_expr)
    check_expr = re.sub(r'\)', ' ', check_expr)
    check_expr = re.sub(r'>=', ' ', check_expr)
    check_expr = re.sub(r'<=', ' ', check_expr)
    check_expr = re.sub(r'==', ' ', check_expr)
    check_expr = re.sub(r'!=', ' ', check_expr)
    check_expr = re.sub(r'>', ' ', check_expr)
    check_expr = re.sub(r'<', ' ', check_expr)
    check_expr = re.sub(r'\+', ' ', check_expr)
    check_expr = re.sub(r'-', ' ', check_expr)
    check_expr = re.sub(r'\*', ' ', check_expr)
    check_expr = re.sub(r'/', ' ', check_expr)
    check_expr = re.sub(r'=', ' ', check_expr)
    check_expr = re.sub(r'\band\b', ' ', check_expr, flags=re.IGNORECASE)
    check_expr = re.sub(r'\bor\b', ' ', check_expr, flags=re.IGNORECASE)
    check_expr = re.sub(r'\bnot\b', ' ', check_expr, flags=re.IGNORECASE)

    # Check if there are any remaining tokens (identifiers, numbers)
    tokens = check_expr.split()
    if not tokens:
        raise ETLError(
            "BAD_EXPR",
            "ETL_ERROR: expression is empty or invalid",
            path
        )


def validate_step(step: Dict[str, Any], index: int) -> None:
    """Validate a single step in the pipeline."""
    path_prefix = f"pipeline.steps[{index}]"

    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: step must be an object",
            path_prefix
        )

    if "op" not in step:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' field is required",
            f"{path_prefix}.op"
        )

    op_value = step["op"]
    if not isinstance(op_value, str):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' must be a string",
            f"{path_prefix}.op"
        )

    if is_empty_after_trim(op_value):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'op' cannot be empty",
            f"{path_prefix}.op"
        )

    op_normalized = op_value.strip().lower()

    # Check for unknown operations
    valid_ops = {"select", "filter", "map", "rename", "limit"}
    if op_normalized not in valid_ops:
        raise ETLError(
            "UNKNOWN_OP",
            f"ETL_ERROR: unsupported op '{op_normalized}'",
            f"{path_prefix}.op"
        )

    # Validate operation-specific fields
    if op_normalized == "select":
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'columns' field is required for 'select' operation",
                f"{path_prefix}.columns"
            )
        columns = step["columns"]
        if not isinstance(columns, list):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'columns' must be an array",
                f"{path_prefix}.columns"
            )
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    f"ETL_ERROR: column name must be a string",
                    f"{path_prefix}.columns[{i}]"
                )
            if is_empty_after_trim(col):
                raise ETLError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column name cannot be empty",
                    f"{path_prefix}.columns[{i}]"
                )

    elif op_normalized == "filter":
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'where' field is required for 'filter' operation",
                f"{path_prefix}.where"
            )
        where = step["where"]
        if not isinstance(where, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'where' must be a string",
                f"{path_prefix}.where"
            )
        if is_empty_after_trim(where):
            raise ETLError(
                "BAD_EXPR",
                "ETL_ERROR: 'where' expression cannot be empty",
                f"{path_prefix}.where"
            )
        validate_expression(where, f"{path_prefix}.where")

    elif op_normalized == "map":
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' field is required for 'map' operation",
                f"{path_prefix}.as"
            )
        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'expr' field is required for 'map' operation",
                f"{path_prefix}.expr"
            )

        as_value = step["as"]
        if not isinstance(as_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' must be a string",
                f"{path_prefix}.as"
            )
        if is_empty_after_trim(as_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'as' cannot be empty",
                f"{path_prefix}.as"
            )

        expr = step["expr"]
        if not isinstance(expr, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'expr' must be a string",
                f"{path_prefix}.expr"
            )
        if is_empty_after_trim(expr):
            raise ETLError(
                "BAD_EXPR",
                "ETL_ERROR: 'expr' expression cannot be empty",
                f"{path_prefix}.expr"
            )
        validate_expression(expr, f"{path_prefix}.expr")

    elif op_normalized == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if not has_from_to and not has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'rename' requires either 'from'/'to' or 'mapping'",
                path_prefix
            )

        if has_from_to and has_mapping:
            # Both present - we'll process from/to and ignore mapping or vice versa
            # Based on the spec, let's check which one to use
            # The spec says "requires either", so having both is ambiguous
            # Let's prefer from/to conversion as shown in example
            pass

        if has_from_to:
            from_value = step["from"]
            to_value = step["to"]

            if not isinstance(from_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'from' must be a string",
                    f"{path_prefix}.from"
                )
            if not isinstance(to_value, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'to' must be a string",
                    f"{path_prefix}.to"
                )

            if is_empty_after_trim(from_value):
                raise ETLError(
                    "MISSING_COLUMN",
                    "ETL_ERROR: 'from' cannot be empty",
                    f"{path_prefix}.from"
                )
            if is_empty_after_trim(to_value):
                raise ETLError(
                    "MISSING_COLUMN",
                    "ETL_ERROR: 'to' cannot be empty",
                    f"{path_prefix}.to"
                )

        if has_mapping and not has_from_to:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'mapping' must be an object",
                    f"{path_prefix}.mapping"
                )
            for key, value in mapping.items():
                if not isinstance(key, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: mapping key must be a string",
                        f"{path_prefix}.mapping"
                    )
                if not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "ETL_ERROR: mapping value must be a string",
                        f"{path_prefix}.mapping"
                    )

    elif op_normalized == "limit":
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' field is required for 'limit' operation",
                f"{path_prefix}.n"
            )
        n_value = step["n"]
        if not isinstance(n_value, int):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' must be an integer",
                f"{path_prefix}.n"
            )
        if n_value < 0:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'n' must be >= 0",
                f"{path_prefix}.n"
            )


def normalize_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a single step according to the rules."""
    result = {}

    # Normalize op
    op_value = step["op"].strip().lower()
    result["op"] = op_value

    # Process operation-specific fields
    if op_value == "select":
        columns = step.get("columns", [])
        # Column names are preserved exactly (not trimmed)
        result["columns"] = columns

    elif op_value == "filter":
        # Trim expression
        where = step.get("where", "").strip()
        result["where"] = where

    elif op_value == "map":
        # Trim expression and as field
        expr = step.get("expr", "").strip()
        as_value = step.get("as", "").strip()
        result["as"] = as_value
        result["expr"] = expr

    elif op_value == "rename":
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if has_from_to:
            # Convert from/to to mapping
            from_value = step["from"].strip()
            to_value = step["to"].strip()
            result["mapping"] = {from_value: to_value}
        elif has_mapping:
            # Preserve mapping keys/values exactly
            mapping = step.get("mapping", {})
            result["mapping"] = mapping

    elif op_value == "limit":
        n_value = step.get("n", 0)
        result["n"] = n_value

    # Sort keys: op first, then alphabetically
    sorted_result = {"op": result["op"]}
    for key in sorted(k for k in result.keys() if k != "op"):
        sorted_result[key] = result[key]

    return sorted_result


def process_pipeline(input_data: str) -> Tuple[Dict[str, Any], int]:
    """Process the ETL pipeline input and return result with exit code."""
    try:
        data = json.loads(input_data)
    except json.JSONDecodeError as e:
        return {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: invalid JSON: {str(e)}",
            "path": "root"
        }, 1

    try:
        # Validate structure
        validate_json_structure(data)

        steps = data["pipeline"]["steps"]

        # Validate each step
        for i, step in enumerate(steps):
            validate_step(step, i)

        # Normalize steps
        normalized_steps = [normalize_step(step) for step in steps]

        return {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }, 0

    except ETLError as e:
        return {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }, 1


def main():
    """Main entry point."""
    input_data = sys.stdin.read()
    result, exit_code = process_pipeline(input_data)
    print(json.dumps(result))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
