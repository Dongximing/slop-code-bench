#!/usr/bin/env python3
"""
ETL Pipeline Validator and Normalizer

Reads a JSON ETL pipeline specification from STDIN, validates and normalizes it,
then outputs a structured JSON response or error.
"""

import json
import re
import sys
from typing import Any


def parse_input() -> tuple[dict, list]:
    """Parse JSON from STDIN, return (pipeline, dataset)."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"Invalid JSON: {e}",
                       "root")

    pipeline = data.get("pipeline", {})
    dataset = data.get("dataset", [])

    if not isinstance(dataset, list):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "dataset must be an array",
                       "dataset")

    return pipeline, dataset


def error_response(code: str, message: str, path: str) -> None:
    """Output error JSON and exit with code 1."""
    result = {
        "status": "error",
        "error_code": code,
        "message": f"ETL_ERROR: {message}",
        "path": path,
    }
    print(json.dumps(result))
    sys.exit(1)


def normalize_step(step: dict, index: int) -> dict:
    """
    Normalize a single step object.
    Returns the normalized step dict.
    Raises specific errors via error_response.
    """
    if not isinstance(step, dict):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Each step must be an object",
                       f"pipeline.steps[{index}]")

    if "op" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Step is missing required 'op' field",
                       f"pipeline.steps[{index}]")

    op = step["op"]
    if not isinstance(op, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'op' must be a string",
                       f"pipeline.steps[{index}].op")

    # Normalize operation name: trim and lowercase
    op_normalized = op.strip().lower()
    if not op_normalized:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'op' field is empty after trimming",
                       f"pipeline.steps[{index}].op")

    # Validate supported operations
    supported_ops = {"select", "filter", "map", "rename", "limit"}
    if op_normalized not in supported_ops:
        error_response("UNKNOWN_OP",
                       f"unsupported op '{op_normalized}'",
                       f"pipeline.steps[{index}].op")

    # Dispatch to operation-specific normalization
    if op_normalized == "select":
        return normalize_select(step, index)
    elif op_normalized == "filter":
        return normalize_filter(step, index)
    elif op_normalized == "map":
        return normalize_map(step, index)
    elif op_normalized == "rename":
        return normalize_rename(step, index)
    elif op_normalized == "limit":
        return normalize_limit(step, index)

    # Unreachable, but keep for completeness
    return {}


def normalize_select(step: dict, index: int) -> dict:
    """Normalize select operation."""
    if "columns" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "select step is missing required 'columns' field",
                       f"pipeline.steps[{index}]")

    columns = step["columns"]
    if not isinstance(columns, list):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'columns' must be an array",
                       f"pipeline.steps[{index}].columns")

    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error_response("SCHEMA_VALIDATION_FAILED",
                           f"columns[{i}] must be a string",
                           f"pipeline.steps[{index}].columns[{i}]")
        # Column names are preserved exactly (spaces are significant)
        # No trimming, no modification
        if col == "":
            error_response("SCHEMA_VALIDATION_FAILED",
                           f"columns[{i}] cannot be empty after trimming",
                           f"pipeline.steps[{index}].columns[{i}]")

    # Known keys: columns. Unknown keys are dropped.
    # Key ordering: op first, then other fields alphabetically.
    normalized = {"op": "select", "columns": columns}
    return normalized


def normalize_filter(step: dict, index: int) -> dict:
    """Normalize filter operation."""
    if "where" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "filter step is missing required 'where' field",
                       f"pipeline.steps[{index}]")

    where = step["where"]
    if not isinstance(where, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'where' must be a string",
                       f"pipeline.steps[{index}].where")

    # Trim the expression
    where_trimmed = where.strip()
    if not where_trimmed:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'where' field is empty after trimming",
                       f"pipeline.steps[{index}].where")

    # Validate expression syntax
    validate_expression(where_trimmed, index, "where")

    normalized = {"op": "filter", "where": where_trimmed}
    return normalized


def normalize_map(step: dict, index: int) -> dict:
    """Normalize map operation."""
    if "as" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "map step is missing required 'as' field",
                       f"pipeline.steps[{index}]")
    if "expr" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "map step is missing required 'expr' field",
                       f"pipeline.steps[{index}]")

    as_field = step["as"]
    if not isinstance(as_field, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'as' must be a string",
                       f"pipeline.steps[{index}].as")

    expr = step["expr"]
    if not isinstance(expr, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'expr' must be a string",
                       f"pipeline.steps[{index}].expr")

    # Trim 'as' field
    as_trimmed = as_field.strip()
    if not as_trimmed:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'as' field is empty after trimming",
                       f"pipeline.steps[{index}].as")

    # Trim expr
    expr_trimmed = expr.strip()
    if not expr_trimmed:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'expr' field is empty after trimming",
                       f"pipeline.steps[{index}].expr")

    # Validate expression syntax
    validate_expression(expr_trimmed, index, "expr")

    normalized = {"op": "map", "as": as_trimmed, "expr": expr_trimmed}
    return normalized


def normalize_rename(step: dict, index: int) -> dict:
    """Normalize rename operation."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step

    if not (has_from_to or has_mapping):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step requires either ('from' and 'to') or 'mapping'",
                       f"pipeline.steps[{index}]")

    if has_from_to and has_mapping:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step cannot have both ('from' and 'to') and 'mapping'",
                       f"pipeline.steps[{index}]")

    if has_from_to:
        from_field = step["from"]
        to_field = step["to"]

        if not isinstance(from_field, str):
            error_response("SCHEMA_VALIDATION_FAILED",
                           "'from' must be a string",
                           f"pipeline.steps[{index}].from")
        if not isinstance(to_field, str):
            error_response("SCHEMA_VALIDATION_FAILED",
                           "'to' must be a string",
                           f"pipeline.steps[{index}].to")

        from_trimmed = from_field.strip()
        to_trimmed = to_field.strip()

        if not from_trimmed:
            error_response("SCHEMA_VALIDATION_FAILED",
                           "'from' field is empty after trimming",
                           f"pipeline.steps[{index}].from")
        if not to_trimmed:
            error_response("SCHEMA_VALIDATION_FAILED",
                           "'to' field is empty after trimming",
                           f"pipeline.steps[{index}].to")

        mapping = {from_trimmed: to_trimmed}
    else:
        mapping_obj = step["mapping"]
        if not isinstance(mapping_obj, dict):
            error_response("SCHEMA_VALIDATION_FAILED",
                           "'mapping' must be an object",
                           f"pipeline.steps[{index}].mapping")

        mapping = {}
        for key, value in mapping_obj.items():
            if not isinstance(key, str):
                error_response("SCHEMA_VALIDATION_FAILED",
                               "mapping keys must be strings",
                               f"pipeline.steps[{index}].mapping")
            if not isinstance(value, str):
                error_response("SCHEMA_VALIDATION_FAILED",
                               "mapping values must be strings",
                               f"pipeline.steps[{index}].mapping[{json.dumps(key)}]")
            # Mapping keys/values preserved exactly
            mapping[key] = value

    normalized = {"op": "rename", "mapping": mapping}
    return normalized


def normalize_limit(step: dict, index: int) -> dict:
    """Normalize limit operation."""
    if "n" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "limit step is missing required 'n' field",
                       f"pipeline.steps[{index}]")

    n = step["n"]
    if not isinstance(n, int):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'n' must be an integer",
                       f"pipeline.steps[{index}].n")

    if n < 0:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'n' must be >= 0",
                       f"pipeline.steps[{index}].n")

    normalized = {"op": "limit", "n": n}
    return normalized


def validate_expression(expr: str, index: int, field: str) -> None:
    """
    Validate expression syntax for filter/where fields.
    Reject obvious syntax errors like consecutive operators (^^).
    """
    # Check for consecutive operators (e.g., ^^, **, //, ++, ,, etc.)
    # This pattern looks for two operator-like characters in a row
    # Operators: + - * / > < = ! & | ^ %
    # Allow: <=, >=, ==, !=
    # Reject: ++, --, **, //, <<, >>, ^^, &&, ||
    consecutive_ops = re.search(
        r'(\+\+|--|\*\*|/\/|<<|>>|&&|||\^\%|[<>]\\s*[<>])',
        expr
    )

    if consecutive_ops:
        error_response("BAD_EXPR",
                       f"invalid syntax: consecutive operators '{consecutive_ops.group()}'",
                       f"pipeline.steps[{index}].{field}")

    # Additional basic validation: expression should not be empty after checks
    if not expr.strip():
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"{field} expression is empty",
                       f"pipeline.steps[{index}].{field}")


def main() -> None:
    """Main entry point."""
    pipeline, dataset = parse_input()

    # Validate pipeline structure
    if not isinstance(pipeline, dict):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "pipeline must be an object",
                       "pipeline")

    steps = pipeline.get("steps")
    if steps is None:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "pipeline is missing required 'steps' field",
                       "pipeline")

    if not isinstance(steps, list):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "'steps' must be an array",
                       "pipeline.steps")

    # Normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step = normalize_step(step, i)
        normalized_steps.append(normalized_step)

    # Build result
    result = {
        "status": "ok",
        "normalized": {
            "steps": normalized_steps
        }
    }

    print(json.dumps(result))


if __name__ == "__main__":
    main()
