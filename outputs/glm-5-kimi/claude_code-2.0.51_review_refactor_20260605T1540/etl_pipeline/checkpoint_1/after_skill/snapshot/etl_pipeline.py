#!/usr/bin/env python3
"""ETL Pipeline Parser - Validates and normalizes ETL pipeline specifications."""

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


def validate_expression(expr: str) -> bool:
    """
    Basic validation of expression syntax.
    Rejects obvious syntax errors like consecutive operators.
    """
    if not expr or expr.isspace():
        return False

    # Check for consecutive operators (e.g., ^^, **, ++, etc.)
    # Allow >=, <=, ==, != as valid two-character operators
    tokens = re.split(r'\s+', expr)

    # Check for invalid consecutive symbols
    invalid_patterns = [
        r'[+\-*/]{2,}',  # Consecutive arithmetic operators
        r'[><]{2,}',     # Consecutive comparison operators (but >= <= are valid)
        r'(?<![<>=!])=[^=]',  # Single = (should be ==)
    ]

    for pattern in invalid_patterns:
        if re.search(pattern, expr):
            # Special case: >=, <=, ==, != are valid
            if pattern == r'[><]{2,}':
                # Check if it's >= or <= which are valid
                if not re.search(r'(?<![<>])[<>]{2,}|[<>](?![>=])', expr.replace('>=', '  ').replace('<=', '  ')):
                    continue
            return False

    # Check for consecutive logical operators
    if re.search(r'\b(and|or|not)\s+(and|or|not)\b', expr, re.IGNORECASE):
        return False

    # Check for invalid operator combinations like ^^
    if re.search(r'\^{2,}', expr):
        return False

    # Check for consecutive comparison operators
    if re.search(r'(>=|<=|==|!=|>|<)\s*(>=|<=|==|!=|>|<)', expr):
        return False

    return True


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

        if not validate_expression(where):
            raise ETLError(
                "BAD_EXPR",
                f"invalid expression syntax",
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

        if not validate_expression(expr):
            raise ETLError(
                "BAD_EXPR",
                f"invalid expression syntax",
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


def main():
    """Main entry point for the ETL pipeline parser."""
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

        # Output success response
        output = {
            "status": "ok",
            "normalized": normalized
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
