#!/usr/bin/env python3
"""
ETL Pipeline Parser - Parses, validates, and normalizes ETL pipeline specifications.
"""

import json
import re
import sys
from typing import Any, Dict, List, Optional, Tuple


class ETLError(Exception):
    """Custom exception for ETL pipeline errors."""
    def __init__(self, error_code: str, message: str, path: str):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(message)


def create_error_response(error_code: str, message: str, path: str) -> Dict[str, Any]:
    """Create a standardized error response."""
    return {
        "status": "error",
        "error_code": error_code,
        "message": f"ETL_ERROR: {message}",
        "path": path
    }


def create_success_response(normalized: Dict[str, Any]) -> Dict[str, Any]:
    """Create a standardized success response."""
    return {
        "status": "ok",
        "normalized": normalized
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

    # Check for consecutive operators (e.g., ^^, ***, ++++)
    # Operators: +, -, *, /, >, >=, <, <=, ==, !=, and, or, not
    # Need to check for truly consecutive operators that aren't valid combinations

    # Remove string literals first (simple approach)
    # Then check for invalid consecutive operators

    # List of operators to check
    ops_pattern = r'(\+{2,}|\-{2,}|\*{2,}|/{2,}|>{2,}|<{2,}|={2,}|!{2,}|\^{2,})'

    # Check for obvious consecutive operator errors
    if re.search(ops_pattern, expr):
        # Allow some valid combinations like >=, <=, ==, !=, but not ^^, ***, etc.
        # Filter out valid multi-char operators
        temp_expr = expr
        # Replace valid multi-char operators with placeholder
        temp_expr = re.sub(r'>=', ' OP ', temp_expr)
        temp_expr = re.sub(r'<=', ' OP ', temp_expr)
        temp_expr = re.sub(r'==', ' OP ', temp_expr)
        temp_expr = re.sub(r'!=', ' OP ', temp_expr)
        temp_expr = re.sub(r'\band\b', ' OP ', temp_expr, flags=re.IGNORECASE)
        temp_expr = re.sub(r'\bor\b', ' OP ', temp_expr, flags=re.IGNORECASE)
        temp_expr = re.sub(r'\bnot\b', ' OP ', temp_expr, flags=re.IGNORECASE)

        # Now check for consecutive operators
        # Look for patterns like ++, --, **, //, ^^, etc.
        if re.search(r'[\+\-\*/\^\<\>\=]{2,}', temp_expr.replace(' OP ', '')):
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

        # Validate expression
        if not validate_expression(where):
            raise ETLError(
                "BAD_EXPR",
                "invalid expression syntax",
                f"pipeline.steps[{index}].where"
            )

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

        # Validate expression
        if not validate_expression(expr):
            raise ETLError(
                "BAD_EXPR",
                "invalid expression syntax",
                f"pipeline.steps[{index}].expr"
            )

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


def process_request(input_json: str) -> Dict[str, Any]:
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

    return create_success_response({"steps": normalized_steps})


def main():
    """Main entry point."""
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

    response = process_request(input_data)
    print(json.dumps(response))

    if response["status"] == "error":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    main()
