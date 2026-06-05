#!/usr/bin/env python3
"""
ETL Pipeline Parser and Normalizer

A CLI program that reads an ETL pipeline specification from STDIN,
validates and normalizes it, then outputs a structured JSON response or error.
"""

import json
import sys
import re
from typing import Any, Dict, List, Optional, Tuple


class ETLError(Exception):
    """Custom exception for ETL pipeline errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


def error_response(error_code: str, message: str, path: str) -> Dict[str, str]:
    """Create a standardized error response."""
    return {
        "status": "error",
        "error_code": error_code,
        "message": f"ETL_ERROR: {message}",
        "path": path
    }


def success_response(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Create a standardized success response."""
    return {
        "status": "ok",
        "normalized": normalized
    }


def validate_expression(expr: str) -> bool:
    """
    Validate an expression string for obvious syntax errors.
    Returns True if valid, False otherwise.
    """
    if not expr or not expr.strip():
        return False

    expr = expr.strip()

    # Check for consecutive operators (obvious syntax errors)
    # Operators: +, -, *, /, >, >=, <, <=, ==, !=, and, or, not
    operators = [r'\+\+', r'\-\-', r'\*\*', r'//', r'\|\|', r'&&', r'\^\^']

    for op_pattern in operators:
        if re.search(op_pattern, expr):
            return False

    # Check for consecutive comparison operators
    if re.search(r'(>=|<=|==|!=|>|<)\s*(>=|<=|==|!=|>|<)', expr):
        return False

    # Check for invalid character sequences
    if re.search(r'[^a-zA-Z0-9_\s\+\-\*/><=!().]', expr):
        # Allow only valid characters for identifiers, operators, numbers, and parens
        pass  # We'll be permissive here since the spec doesn't define exact character set

    # Basic tokenization check - must have valid structure
    # This is a simplified check - we ensure no obvious malformed sequences
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+(?:\.[0-9]+)?|[+\-*/><=!]+|[().]', expr)

    # Reconstruct and check if we can parse it reasonably
    # Check for things like "==" not being "===" or malformed operators
    for i, token in enumerate(tokens):
        if token in ['==', '!=', '>=', '<=']:
            continue
        if set(token) <= set('><=!') and len(token) > 2:
            return False
        if token in ['===', '!==', '<<', '>>']:
            return False

    return True


def trim_value(value: str) -> str:
    """Trim whitespace from a string value."""
    return value.strip() if isinstance(value, str) else value


def is_empty_after_trim(value: Any) -> bool:
    """Check if a value is empty or whitespace-only after trimming."""
    if not isinstance(value, str):
        return False
    return len(value.strip()) == 0


def normalize_step(step: Dict[str, Any], index: int) -> Dict[str, Any]:
    """
    Normalize a single step according to the rules.
    Returns the normalized step dict.
    Raises ETLError on validation errors.
    """
    if not isinstance(step, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "step must be an object",
            f"pipeline.steps[{index}]"
        )

    # Check for 'op' field
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
    known_ops = {"select", "filter", "map", "rename", "limit"}

    if op_normalized not in known_ops:
        raise ETLError(
            "UNKNOWN_OP",
            f"unsupported op '{op_normalized}'",
            f"pipeline.steps[{index}].op"
        )

    # Build normalized step with only valid fields
    normalized = {"op": op_normalized}

    # Process each operation type
    if op_normalized == "select":
        # Requires "columns" (array of strings)
        if "columns" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'columns' for select operation",
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
        for col_idx, col in enumerate(columns):
            if not isinstance(col, str):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "column names must be strings",
                    f"pipeline.steps[{index}].columns[{col_idx}]"
                )
            if is_empty_after_trim(col):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "column name cannot be empty",
                    f"pipeline.steps[{index}].columns[{col_idx}]"
                )

        # Preserve column names exactly (including spaces)
        normalized["columns"] = columns

    elif op_normalized == "filter":
        # Requires "where" (string expression)
        if "where" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'where' for filter operation",
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
                "where expression cannot be empty",
                f"pipeline.steps[{index}].where"
            )

        where_trimmed = where.strip()

        # Validate expression syntax
        if not validate_expression(where_trimmed):
            raise ETLError(
                "BAD_EXPR",
                f"invalid expression '{where_trimmed}'",
                f"pipeline.steps[{index}].where"
            )

        normalized["where"] = where_trimmed

    elif op_normalized == "map":
        # Requires "as" (string) and "expr" (string expression)
        if "as" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'as' for map operation",
                f"pipeline.steps[{index}]"
            )

        if "expr" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'expr' for map operation",
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

        expr_value = step["expr"]
        if not isinstance(expr_value, str):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr must be a string",
                f"pipeline.steps[{index}].expr"
            )

        if is_empty_after_trim(expr_value):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "expr cannot be empty",
                f"pipeline.steps[{index}].expr"
            )

        expr_trimmed = expr_value.strip()

        # Validate expression syntax
        if not validate_expression(expr_trimmed):
            raise ETLError(
                "BAD_EXPR",
                f"invalid expression '{expr_trimmed}'",
                f"pipeline.steps[{index}].expr"
            )

        # Key ordering: as comes before expr alphabetically
        normalized["as"] = as_value.strip()
        normalized["expr"] = expr_trimmed

    elif op_normalized == "rename":
        # Requires either from/to OR mapping

        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if not has_from_to and not has_mapping:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "rename requires either 'from' and 'to' fields, or 'mapping' field",
                f"pipeline.steps[{index}]"
            )

        if has_from_to and has_mapping:
            # Prefer mapping if both present, but we'll use mapping
            pass

        if has_mapping:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                raise ETLError(
                    "SCHEMA_VALIDATION_FAILED",
                    "mapping must be an object",
                    f"pipeline.steps[{index}].mapping"
                )

            # Validate all keys and values are strings
            for key, value in mapping.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping keys and values must be strings",
                        f"pipeline.steps[{index}].mapping"
                    )
                if is_empty_after_trim(key):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping key cannot be empty",
                        f"pipeline.steps[{index}].mapping"
                    )
                if is_empty_after_trim(value):
                    raise ETLError(
                        "SCHEMA_VALIDATION_FAILED",
                        "mapping value cannot be empty",
                        f"pipeline.steps[{index}].mapping"
                    )

            # Preserve mapping keys/values exactly
            normalized["mapping"] = mapping

        else:
            # Convert from/to to mapping
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

            # Trim before conversion to mapping
            from_trimmed = from_value.strip()
            to_trimmed = to_value.strip()

            # Preserve exact values
            normalized["mapping"] = {from_trimmed: to_trimmed}

    elif op_normalized == "limit":
        # Requires "n" (integer >= 0)
        if "n" not in step:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "missing required field 'n' for limit operation",
                f"pipeline.steps[{index}]"
            )

        n_value = step["n"]
        if not isinstance(n_value, int) or isinstance(n_value, bool):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be an integer",
                f"pipeline.steps[{index}].n"
            )

        if n_value < 0:
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "n must be >= 0",
                f"pipeline.steps[{index}].n"
            )

        normalized["n"] = n_value

    return normalized


def validate_and_normalize(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and normalize the input ETL pipeline specification.
    Returns the normalized pipeline or raises ETLError.
    """
    # Check for required top-level structure
    if not isinstance(input_data, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "input must be an object",
            ""
        )

    # Dataset is required
    if "dataset" not in input_data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'dataset'",
            "dataset"
        )

    dataset = input_data["dataset"]
    if not isinstance(dataset, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "dataset must be an array",
            "dataset"
        )

    # Validate each dataset element is a JSON object
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            raise ETLError(
                "SCHEMA_VALIDATION_FAILED",
                "each dataset element must be an object",
                f"dataset[{i}]"
            )

    # Pipeline is required
    if "pipeline" not in input_data:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'pipeline'",
            "pipeline"
        )

    pipeline = input_data["pipeline"]
    if not isinstance(pipeline, dict):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "pipeline must be an object",
            "pipeline"
        )

    # Steps is required in pipeline
    if "steps" not in pipeline:
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "missing required field 'steps' in pipeline",
            "pipeline.steps"
        )

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        raise ETLError(
            "SCHEMA_VALIDATION_FAILED",
            "steps must be an array",
            "pipeline.steps"
        )

    # Normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step = normalize_step(step, i)
        normalized_steps.append(normalized_step)

    return {"steps": normalized_steps}


def main():
    """Main entry point for the ETL pipeline CLI."""
    try:
        # Read JSON from STDIN
        input_text = sys.stdin.read()

        try:
            input_data = json.loads(input_text)
        except json.JSONDecodeError as e:
            response = error_response(
                "SCHEMA_VALIDATION_FAILED",
                f"invalid JSON: {str(e)}",
                ""
            )
            print(json.dumps(response, indent=2))
            sys.exit(1)

        # Validate and normalize
        try:
            normalized = validate_and_normalize(input_data)
            response = success_response(normalized)
            print(json.dumps(response, indent=2))
            sys.exit(0)
        except ETLError as e:
            response = error_response(e.error_code, e.message, e.path)
            print(json.dumps(response, indent=2))
            sys.exit(1)

    except Exception as e:
        response = error_response(
            "SCHEMA_VALIDATION_FAILED",
            f"unexpected error: {str(e)}",
            ""
        )
        print(json.dumps(response, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
