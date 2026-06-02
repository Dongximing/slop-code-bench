#!/usr/bin/env python3
"""ETL Pipeline Validator and Normalizer.

Reads a JSON ETL pipeline specification from STDIN, validates and normalizes it,
then outputs a structured JSON response or error.
"""

import json
import re
import sys


def _required_field(step: dict, name: str, index: int):
    """Get required field or error."""
    if name not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"Step is missing required '{name}' field",
                       f"pipeline.steps[{index}]")
    return step[name]


def _non_empty_string(value, field_path: str) -> str:
    """Validate and return non-empty trimmed string."""
    if not isinstance(value, str):
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"'{field_path.split('.')[-1]}' must be a string",
                       field_path)
    trimmed = value.strip()
    if not trimmed:
        error_response("SCHEMA_VALIDATION_FAILED",
                       f"'{field_path.split('.')[-1]}' field is empty after trimming",
                       field_path)
    return trimmed


def parse_input() -> tuple[dict, list]:
    """Parse JSON from STDIN, return (pipeline, dataset)."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error_response("SCHEMA_VALIDATION_FAILED", f"Invalid JSON: {e}", "root")

    pipeline = data.get("pipeline", {})
    dataset = data.get("dataset", [])

    if not isinstance(dataset, list):
        error_response("SCHEMA_VALIDATION_FAILED", "dataset must be an array", "dataset")

    return pipeline, dataset


def error_response(code: str, message: str, path: str) -> None:
    """Output error JSON and exit with code 1."""
    print(json.dumps({
        "status": "error",
        "error_code": code,
        "message": f"ETL_ERROR: {message}",
        "path": path,
    }))
    sys.exit(1)


def normalize_step(step: dict, index: int) -> dict:
    """Normalize a single step object."""
    if not isinstance(step, dict):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Each step must be an object", f"pipeline.steps[{index}]")
    if "op" not in step:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "Step is missing required 'op' field", f"pipeline.steps[{index}]")

    op = step["op"]
    if not isinstance(op, str):
        error_response("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    op_normalized = op.strip().lower()
    if not op_normalized:
        error_response("SCHEMA_VALIDATION_FAILED", "'op' field is empty after trimming", f"pipeline.steps[{index}].op")

    dispatch = {
        "select": normalize_select,
        "filter": normalize_filter,
        "map": normalize_map,
        "rename": normalize_rename,
        "limit": normalize_limit,
    }
    handler = dispatch.get(op_normalized)
    if handler is None:
        error_response("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")
    return handler(step, index)


def normalize_select(step: dict, index: int) -> dict:
    """Normalize select operation."""
    columns = _required_field(step, "columns", index)
    if not isinstance(columns, list):
        error_response("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error_response("SCHEMA_VALIDATION_FAILED", f"columns[{i}] must be a string", f"pipeline.steps[{index}].columns[{i}]")
        if col == "":
            error_response("SCHEMA_VALIDATION_FAILED", f"columns[{i}] cannot be empty", f"pipeline.steps[{index}].columns[{i}]")
    return {"op": "select", "columns": columns}


def normalize_filter(step: dict, index: int) -> dict:
    """Normalize filter operation."""
    where = _non_empty_string(_required_field(step, "where", index), f"pipeline.steps[{index}].where")
    validate_expression(where, index, "where")
    return {"op": "filter", "where": where}


def normalize_map(step: dict, index: int) -> dict:
    """Normalize map operation."""
    as_field = _non_empty_string(_required_field(step, "as", index), f"pipeline.steps[{index}].as")
    expr = _non_empty_string(_required_field(step, "expr", index), f"pipeline.steps[{index}].expr")
    validate_expression(expr, index, "expr")
    return {"op": "map", "as": as_field, "expr": expr}


def normalize_rename(step: dict, index: int) -> dict:
    """Normalize rename operation."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step

    if not (has_from_to or has_mapping):
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step requires either ('from' and 'to') or 'mapping'", f"pipeline.steps[{index}]")
    if has_from_to and has_mapping:
        error_response("SCHEMA_VALIDATION_FAILED",
                       "rename step cannot have both ('from' and 'to') and 'mapping'", f"pipeline.steps[{index}]")

    if has_from_to:
        from_field = _non_empty_string(step["from"], f"pipeline.steps[{index}].from")
        to_field = _non_empty_string(step["to"], f"pipeline.steps[{index}].to")
        mapping = {from_field: to_field}
    else:
        mapping_obj = _required_field(step, "mapping", index)
        if not isinstance(mapping_obj, dict):
            error_response("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")
        mapping = {}
        for key, value in mapping_obj.items():
            if not isinstance(key, str):
                error_response("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(value, str):
                error_response("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping[{json.dumps(key)}]")
            mapping[key] = value
    return {"op": "rename", "mapping": mapping}


def normalize_limit(step: dict, index: int) -> dict:
    """Normalize limit operation."""
    n = _required_field(step, "n", index)
    if not isinstance(n, int):
        error_response("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")
    if n < 0:
        error_response("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")
    return {"op": "limit", "n": n}


def validate_expression(expr: str, index: int, field: str) -> None:
    """Validate expression syntax for filter/where fields."""
    consecutive_ops = re.search(
        r'(\+\+|--|\*\*|/\/|<<|>>|\|\||&&|\^%|[<>]\s*[<>])',
        expr
    )
    if consecutive_ops:
        error_response("BAD_EXPR", f"invalid syntax: consecutive operators '{consecutive_ops.group()}'", f"pipeline.steps[{index}].{field}")
    if not expr.strip():
        error_response("SCHEMA_VALIDATION_FAILED", f"{field} expression is empty", f"pipeline.steps[{index}].{field}")


def main() -> None:
    """Main entry point."""
    pipeline, dataset = parse_input()

    if not isinstance(pipeline, dict):
        error_response("SCHEMA_VALIDATION_FAILED", "pipeline must be an object", "pipeline")

    steps = pipeline.get("steps")
    if steps is None:
        error_response("SCHEMA_VALIDATION_FAILED", "pipeline is missing required 'steps' field", "pipeline")
    if not isinstance(steps, list):
        error_response("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", "pipeline.steps")

    normalized_steps = [normalize_step(step, i) for i, step in enumerate(steps)]

    print(json.dumps({"status": "ok", "normalized": {"steps": normalized_steps}}))


if __name__ == "__main__":
    main()
