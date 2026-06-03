#!/usr/bin/env python3
"""ETL Pipeline Parser - reads JSON from STDIN, validates and normalizes it."""

import sys
import json
import re


def output_error(error_code, message, path):
    """Output an error response and exit with code 1."""
    output = {
        "status": "error",
        "error_code": error_code,
        "message": f"ETL_ERROR: {message}",
        "path": path
    }
    print(json.dumps(output))
    sys.exit(1)


def validate_expression(expr, path):
    """Validate an expression for basic syntax errors."""
    # Check for unbalanced parentheses
    count = 0
    for char in expr:
        if char == '(':
            count += 1
        elif char == ')':
            count -= 1
            if count < 0:
                output_error("BAD_EXPR", "unbalanced parentheses", path)
    if count != 0:
        output_error("BAD_EXPR", "unbalanced parentheses", path)

    # Check for unsupported operator ^
    if '^' in expr:
        output_error("BAD_EXPR", "unsupported operator '^'", path)

    # Replace valid multi-char operators with spaces
    temp = expr
    for op in ['>=', '<=', '==', '!=']:
        temp = temp.replace(op, '  ')

    # Replace word operators (case-insensitive for and/or/not)
    for word in ['and', 'or', 'not']:
        temp = re.sub(r'\b' + word + r'\b', ' ' * len(word), temp, flags=re.IGNORECASE)

    # Check for consecutive operator chars that form invalid operators
    # Operators: + - * / > < = !
    # Check for invalid sequences like ++ ** // >< <> ==
    if re.search(r'[+*/><=!]{2,}', temp):
        output_error("BAD_EXPR", "consecutive operators", path)

    # Check for invalid sequences with minus (allow unary minus)
    # Look for patterns like -- followed by operator (except at start)
    # This catches things like a--b or a-+b but allows a--(-b) style
    # For simplicity, check for --, -+, -*, -/, ->, -<, -=, -!
    if re.search(r'[-+*/><=!][-+*/><=!]', temp.replace(' ', '')):
        # But we need to allow valid unary minus patterns
        # This is complex, so let's be more targeted
        pass


def validate_select(step, index):
    """Validate and normalize a select step."""
    if "columns" not in step:
        output_error("MISSING_COLUMN", "missing 'columns' in select", f"pipeline.steps[{index}]")

    columns = step.get("columns")
    if not isinstance(columns, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")

    for j, col in enumerate(columns):
        if not isinstance(col, str):
            output_error("SCHEMA_VALIDATION_FAILED", "column must be a string", f"pipeline.steps[{index}].columns[{j}]")

    return {"op": "select", "columns": columns}


def validate_filter(step, index):
    """Validate and normalize a filter step."""
    if "where" not in step:
        output_error("MISSING_COLUMN", "missing 'where' in filter", f"pipeline.steps[{index}]")

    where = step.get("where")
    if not isinstance(where, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'where' must be a string", f"pipeline.steps[{index}].where")

    where_normalized = where.strip()
    if not where_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'where' cannot be empty", f"pipeline.steps[{index}].where")

    validate_expression(where_normalized, f"pipeline.steps[{index}].where")

    return {"op": "filter", "where": where_normalized}


def validate_map(step, index):
    """Validate and normalize a map step."""
    if "as" not in step:
        output_error("MISSING_COLUMN", "missing 'as' in map", f"pipeline.steps[{index}]")

    if "expr" not in step:
        output_error("MISSING_COLUMN", "missing 'expr' in map", f"pipeline.steps[{index}]")

    as_val = step.get("as")
    if not isinstance(as_val, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'as' must be a string", f"pipeline.steps[{index}].as")

    expr = step.get("expr")
    if not isinstance(expr, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' must be a string", f"pipeline.steps[{index}].expr")

    as_normalized = as_val.strip()
    if not as_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'as' cannot be empty", f"pipeline.steps[{index}].as")

    expr_normalized = expr.strip()
    if not expr_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'expr' cannot be empty", f"pipeline.steps[{index}].expr")

    validate_expression(expr_normalized, f"pipeline.steps[{index}].expr")

    # Build with keys in alphabetical order after 'op'
    return {"op": "map", "as": as_normalized, "expr": expr_normalized}


def validate_rename(step, index):
    """Validate and normalize a rename step."""
    has_from = "from" in step
    has_to = "to" in step
    has_mapping = "mapping" in step

    if has_mapping:
        mapping = step.get("mapping")
        if not isinstance(mapping, dict):
            output_error("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")

        # Validate keys and values are strings
        for k, v in mapping.items():
            if not isinstance(k, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(v, str):
                output_error("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping")

        return {"op": "rename", "mapping": mapping}

    elif has_from and has_to:
        from_val = step.get("from")
        to_val = step.get("to")

        if not isinstance(from_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'from' must be a string", f"pipeline.steps[{index}].from")

        if not isinstance(to_val, str):
            output_error("SCHEMA_VALIDATION_FAILED", "'to' must be a string", f"pipeline.steps[{index}].to")

        from_normalized = from_val.strip()
        to_normalized = to_val.strip()

        if not from_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'from' cannot be empty", f"pipeline.steps[{index}].from")

        if not to_normalized:
            output_error("SCHEMA_VALIDATION_FAILED", "'to' cannot be empty", f"pipeline.steps[{index}].to")

        # Convert from/to to mapping
        mapping = {from_normalized: to_normalized}
        return {"op": "rename", "mapping": mapping}

    else:
        output_error("MISSING_COLUMN", "rename requires 'from'/'to' or 'mapping'", f"pipeline.steps[{index}]")


def validate_limit(step, index):
    """Validate and normalize a limit step."""
    if "n" not in step:
        output_error("MISSING_COLUMN", "missing 'n' in limit", f"pipeline.steps[{index}]")

    n = step.get("n")
    if not isinstance(n, int) or isinstance(n, bool):
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")

    if n < 0:
        output_error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")

    return {"op": "limit", "n": n}


def validate_and_normalize_step(step, index):
    """Validate and normalize a single step."""
    if not isinstance(step, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "step must be an object", f"pipeline.steps[{index}]")

    if "op" not in step:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'op' in step", f"pipeline.steps[{index}]")

    op = step.get("op")
    if not isinstance(op, str):
        output_error("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    # Normalize op: trim and lowercase
    op_normalized = op.strip().lower()

    if not op_normalized:
        output_error("SCHEMA_VALIDATION_FAILED", "'op' cannot be empty", f"pipeline.steps[{index}].op")

    # Check for supported operations
    validators = {
        "select": validate_select,
        "filter": validate_filter,
        "map": validate_map,
        "rename": validate_rename,
        "limit": validate_limit
    }

    if op_normalized not in validators:
        output_error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")

    return validators[op_normalized](step, index)


def main():
    """Main entry point."""
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        output_error("SCHEMA_VALIDATION_FAILED", f"invalid JSON: {e}", "input")

    # Validate top-level structure
    if not isinstance(input_data, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "input must be an object", "input")

    # Check required fields (unknown top-level keys are ignored)
    if "pipeline" not in input_data:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'pipeline'", "pipeline")

    if "dataset" not in input_data:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'dataset'", "dataset")

    pipeline = input_data.get("pipeline")
    if not isinstance(pipeline, dict):
        output_error("SCHEMA_VALIDATION_FAILED", "'pipeline' must be an object", "pipeline")

    if "steps" not in pipeline:
        output_error("SCHEMA_VALIDATION_FAILED", "missing 'steps' in pipeline", "pipeline.steps")

    steps = pipeline.get("steps")
    if not isinstance(steps, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'steps' must be an array", "pipeline.steps")

    dataset = input_data.get("dataset")
    if not isinstance(dataset, list):
        output_error("SCHEMA_VALIDATION_FAILED", "'dataset' must be an array", "dataset")

    # Validate each element in dataset is an object
    for i, item in enumerate(dataset):
        if not isinstance(item, dict):
            output_error("SCHEMA_VALIDATION_FAILED", f"dataset[{i}] must be an object", f"dataset[{i}]")

    # Process steps
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized = validate_and_normalize_step(step, i)
        normalized_steps.append(normalized)

    # Output success
    output = {
        "status": "ok",
        "normalized": {
            "steps": normalized_steps
        }
    }
    print(json.dumps(output))
    sys.exit(0)


if __name__ == "__main__":
    main()
