#!/usr/bin/env python3
"""
ETL Pipeline Validator and Normalizer

Reads a JSON pipeline specification from STDIN, validates and normalizes it,
then outputs a structured JSON response or error.
"""

import json
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
                f"ETL_ERROR: invalid expression syntax in expr",
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


def is_valid_expression(expr: str) -> bool:
    """
    Check if an expression has valid syntax.

    Supports:
    - Arithmetic: +, -, *, /
    - Comparison: >, >=, <, <=, ==, !=
    - Boolean: and, or, not
    - Parentheses

    Returns True if the expression appears syntactically valid.
    """
    if not expr:
        return False

    # Remove whitespace for easier parsing
    expr_clean = expr.replace(' ', '')

    # Check balanced parentheses
    stack = 0
    for char in expr_clean:
        if char == '(':
            stack += 1
        elif char == ')':
            stack -= 1
            if stack < 0:
                return False
    if stack != 0:
        return False

    # Check that expression only contains allowed characters
    # Allowed: letters, digits, _ (for identifiers), + - * / < > = ! ( ), digits, . (for decimals)
    # Also allow whitespace for readability
    allowed_chars = set('+-*/<>=!()_.abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ')
    if any(c not in allowed_chars for c in expr):
        return False

    # Check for consecutive single operators like ++, --, **, //
    # Valid multi-char operators are: ==, !=, <=, >=
    # Remove those first, then check for consecutive ops
    temp = expr_clean.replace('==', ' ').replace('!=', ' ').replace('<=', ' ').replace('>=', ' ')
    if re.search(r'[+\-*/]{2,}', temp):
        return False

    # Check for invalid consecutive op patterns
    # e.g., >=<, ==>, !=<, etc. (operators together but not valid multi-op)
    invalid_op_combos = re.search(r'[><=!]=[><=!]', expr_clean)
    if invalid_op_combos:
        return False

    # Check for consecutive comparison operators like >==, <==, etc.
    if re.search(r'[><]={2,}', expr_clean):
        return False

    # Check that boolean keywords are properly bounded
    # Check in the original (with whitespace), not the cleaned version
    # 'and', 'or' must be surrounded by whitespace or at boundaries
    if re.search(r'\band\b', expr):
        if not re.search(r'(\s+and\s+|^and\s+|\s+and$)', expr):
            return False
    if re.search(r'\bor\b', expr):
        if not re.search(r'(\s+or\s+|^or\s+|\s+or$)', expr):
            return False
    if re.search(r'\bnot\b', expr):
        # 'not' can be at start or after space
        if not re.search(r'^not\s+|\s+not\s+', expr):
            return False

    # Check that the expression has at least one valid token
    # Token pattern: identifier (letters, numbers, underscores), numbers, operators, parentheses
    if not re.search(r'[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+[+\-*/()<>=]', expr_clean):
        return False

    return True


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


def main() -> None:
    """Main entry point."""
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

        # Output success
        result = {
            "status": "ok",
            "normalized": normalized_pipeline
        }
        print(json.dumps(result), flush=True)
        sys.exit(0)

    except ValueError as e:
        # Extract error details
        args = e.args
        if len(args) >= 3:
            message, error_code, path = args[0], args[1], args[2]
        else:
            message = str(args[0])
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
