#!/usr/bin/env python3
"""ETL Pipeline Parser - Reads JSON from STDIN, validates and normalizes pipeline specification."""

import json
import sys
import re
from typing import Any


def is_valid_expr(expr: str) -> bool:
    """Check for obvious syntax errors in expressions.

    Rejects consecutive operators (e.g., '^^', '+-', '*/', etc.).
    """
    # Common operators that should not appear consecutively (except allowed pairs)
    operators = set('+-*/%<>=!&|^?')
    # Allowed consecutive pairs
    allowed_pairs = {
        ('>', '='), ('<', '='), ('!', '='),  # >=, <=, !=
        ('*', '/'), ('/', '*'),  # */, /* (division/multiplication adjacent)
        ('<', '<'), ('>', '>'),  # <<, >>
        ('|', '|'), ('&', '&'),  # ||, &&
        ('=', '='),  # ==
    }

    i = 0
    while i < len(expr) - 1:
        curr = expr[i]
        next_char = expr[i + 1]

        # Check if current and next are both operators
        if curr in operators and next_char in operators:
            # Special case: unary minus at start or after operator/open paren/brace/bracket/comma
            if curr == '-' and i == 0:
                i += 1
                continue
            if curr == '-' and expr[i-1] in '+-*/%<>=!&|^?({[,' and next_char not in operators:
                i += 1
                continue

            # Check if this is an allowed pair
            if {curr, next_char} in allowed_pairs:
                i += 1
                continue

            # Check for specific forbidden consecutive operators
            # e.g., ^^, ##, @@, ~~, $$(, $$, etc.
            if (curr == '^' and next_char == '^'):
                return False  # ^^
            if (curr == '#' and next_char == '#'):
                return False  # ##
            if (curr == '@' and next_char == '@'):
                return False  # @@
            if (curr == '~' and next_char == '~'):
                return False  # ~~
            if (curr == '?' and next_char == '?'):
                return False  # ??

            # General case: if both are arithmetic/bitwise operators and not a known comparison
            if curr in '+-*/%&|^?' and next_char in '+-*/%&|^?':
                return False

        i += 1

    return True


def normalize_step(step: dict[str, Any], index: int) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Normalize a single step. Returns (normalized_step, error_code, path) or (None, error_code, path) on error."""
    if not isinstance(step, dict):
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

    # Get and validate op field
    if "op" not in step:
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

    op_raw = step["op"]
    if not isinstance(op_raw, str):
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].op"

    # Normalize op: trim and lowercase
    op_normalized = op_raw.strip().lower()

    if not op_normalized:
        return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].op"

    # Supported operations
    supported_ops = {"select", "filter", "map", "rename", "limit"}

    if op_normalized not in supported_ops:
        return None, "UNKNOWN_OP", f"pipeline.steps[{index}].op"

    # Build normalized step with op first, then other fields alphabetically
    normalized: dict[str, Any] = {"op": op_normalized}

    # Validate and normalize based on operation
    if op_normalized == "select":
        if "columns" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        columns = step["columns"]
        if not isinstance(columns, list):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].columns"
        # Preserve column names exactly as-is (spaces are significant)
        for col in columns:
            if not isinstance(col, str):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].columns"
        normalized["columns"] = columns

    elif op_normalized == "filter":
        if "where" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        where = step["where"]
        if not isinstance(where, str):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].where"
        # Trim expression
        where_trimmed = where.strip()
        if not where_trimmed:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].where"
        # Validate expression for obvious syntax errors
        if not is_valid_expr(where_trimmed):
            return None, "BAD_EXPR", f"pipeline.steps[{index}].where"
        normalized["where"] = where_trimmed

    elif op_normalized == "map":
        if "as" not in step or "expr" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        as_field = step["as"]
        expr = step["expr"]
        if not isinstance(as_field, str) or not isinstance(expr, str):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        # Trim both fields
        as_trimmed = as_field.strip()
        expr_trimmed = expr.strip()
        if not as_trimmed or not expr_trimmed:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        # Validate expression
        if not is_valid_expr(expr_trimmed):
            return None, "BAD_EXPR", f"pipeline.steps[{index}].expr"
        normalized["as"] = as_trimmed
        normalized["expr"] = expr_trimmed

    elif op_normalized == "rename":
        # Check for from/to or mapping
        has_from_to = "from" in step and "to" in step
        has_mapping = "mapping" in step

        if not has_from_to and not has_mapping:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"

        if has_from_to:
            from_val = step["from"]
            to_val = step["to"]
            if not isinstance(from_val, str) or not isinstance(to_val, str):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
            from_trimmed = from_val.strip()
            to_trimmed = to_val.strip()
            if not from_trimmed or not to_trimmed:
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
            # Convert to mapping format
            normalized["mapping"] = {from_trimmed: to_trimmed}

        if has_mapping:
            mapping = step["mapping"]
            if not isinstance(mapping, dict):
                return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].mapping"
            # Preserve keys and values exactly
            normalized_mapping = {}
            for k, v in mapping.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].mapping"
                normalized_mapping[k] = v
            normalized["mapping"] = normalized_mapping

    elif op_normalized == "limit":
        if "n" not in step:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}]"
        n_val = step["n"]
        if not isinstance(n_val, int):
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].n"
        if n_val < 0:
            return None, "SCHEMA_VALIDATION_FAILED", f"pipeline.steps[{index}].n"
        normalized["n"] = n_val

    # Drop unknown keys (already handled by only adding known fields to normalized)

    return normalized, None, None


def validate_and_normalize_pipeline(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """Validate and normalize the entire pipeline.

    Returns (result_dict, error_code, path) or (None, error_code, path) on error.
    """
    # Check top-level structure
    if "pipeline" not in data:
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline"

    if "dataset" not in data:
        return None, "SCHEMA_VALIDATION_FAILED", "dataset"

    pipeline = data["pipeline"]
    dataset = data["dataset"]

    if not isinstance(pipeline, dict):
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline"

    if not isinstance(dataset, list):
        return None, "SCHEMA_VALIDATION_FAILED", "dataset"

    # Validate each element in dataset must be a JSON object
    for i, row in enumerate(dataset):
        if not isinstance(row, dict):
            return None, "SCHEMA_VALIDATION_FAILED", f"dataset[{i}]"

    # Check steps
    if "steps" not in pipeline:
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline.steps"

    steps = pipeline["steps"]
    if not isinstance(steps, list):
        return None, "SCHEMA_VALIDATION_FAILED", "pipeline.steps"

    # Validate and normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step, error_code, path = normalize_step(step, i)
        if error_code:
            return None, error_code, path
        normalized_steps.append(normalized_step)

    result = {
        "status": "ok",
        "normalized": {
            "steps": normalized_steps
        }
    }
    return result, None, None


def main() -> None:
    """Main entry point."""
    try:
        # Read JSON from STDIN
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error_response = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: Invalid JSON - {e}",
            "path": ""
        }
        print(json.dumps(error_response))
        sys.exit(1)

    # Validate and normalize
    result, error_code, path = validate_and_normalize_pipeline(input_data)

    if error_code:
        error_response = {
            "status": "error",
            "error_code": error_code,
            "message": f"ETL_ERROR: {path}",
            "path": path
        }
        # For error messages, we need to provide more details
        # Based on examples, we should provide descriptive messages
        if error_code == "UNKNOWN_OP":
            # Extract the op value for the message
            try:
                step_idx = int(path.split('[')[1].split(']')[0])
                op_value = input_data["pipeline"]["steps"][step_idx]["op"]
                error_response["message"] = f"ETL_ERROR: unsupported op '{op_value.lower()}'"
            except (IndexError, KeyError, ValueError):
                pass
        elif error_code == "SCHEMA_VALIDATION_FAILED":
            error_response["message"] = f"ETL_ERROR: schema validation failed at {path}"
        elif error_code == "BAD_EXPR":
            error_response["message"] = f"ETL_ERROR: invalid expression at {path}"
        elif error_code == "MISSING_COLUMN":
            error_response["message"] = f"ETL_ERROR: missing column at {path}"

        print(json.dumps(error_response))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
