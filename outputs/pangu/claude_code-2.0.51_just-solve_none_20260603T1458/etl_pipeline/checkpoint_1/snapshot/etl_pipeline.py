#!/usr/bin/env python3
"""ETL Pipeline CLI program that reads JSON from STDIN, validates and normalizes it."""

import json
import re
import sys


class ETLValidationError(Exception):
    """Exception raised for ETL pipeline validation errors."""
    def __init__(self, error_code, message, path):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(self.message)


def parse_request():
    """Parse JSON from STDIN and validate top-level structure."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            f"ETL_ERROR: Invalid JSON: {e}",
            ""
        )

    if "pipeline" not in data:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Missing 'pipeline' key",
            ""
        )

    if "dataset" not in data:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Missing 'dataset' key",
            ""
        )

    if not isinstance(data["dataset"], list):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'dataset' must be an array",
            "dataset"
        )

    for i, item in enumerate(data["dataset"]):
        if not isinstance(item, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: dataset[{i}] must be an object",
                f"dataset[{i}]"
            )

    return data


def normalize_step(step, index):
    """Normalize a single step object.

    Args:
        step: The step object to normalize
        index: The index of this step in the steps array

    Returns:
        Normalized step object

    Raises:
        ETLValidationError: If the step is invalid
    """
    if not isinstance(step, dict):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Step must be an object",
            f"pipeline.steps[{index}]"
        )

    if "op" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Step missing 'op' field",
            f"pipeline.steps[{index}]"
        )

    # Normalize and validate operation name
    op = step["op"].strip().lower()
    if not op:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Operation name cannot be empty or whitespace",
            f"pipeline.steps[{index}].op"
        )

    # Supported operations
    valid_ops = {
        "select": _validate_select,
        "filter": _validate_filter,
        "map": _validate_map,
        "rename": _validate_rename,
        "limit": _validate_limit,
    }

    if op not in valid_ops:
        raise ETLValidationError(
            "UNKNOWN_OP",
            f"ETL_ERROR: unsupported op '{op}'",
            f"pipeline.steps[{index}].op"
        )

    # Validate and normalize the operation
    return valid_ops[op](step, index, op)


def _validate_select(step, index, op):
    """Validate and normalize select operation."""
    if "columns" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'select' operation requires 'columns' field",
            f"pipeline.steps[{index}]"
        )

    columns = step["columns"]
    if not isinstance(columns, list):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'columns' must be an array",
            f"pipeline.steps[{index}].columns"
        )

    new_columns = []
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: column[{i}] must be a string",
                f"pipeline.steps[{index}].columns[{i}]"
            )
        # Trim to check for emptiness
        if not col.strip():
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: column[{i}] cannot be empty or whitespace",
                f"pipeline.steps[{index}].columns[{i}]"
            )
        # Preserve exactly as provided (spaces are significant)
        new_columns.append(col)

    result = {"op": op, "columns": new_columns}
    return result


def _validate_filter(step, index, op):
    """Validate and normalize filter operation."""
    if "where" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'filter' operation requires 'where' field",
            f"pipeline.steps[{index}]"
        )

    where_expr = step["where"].strip()
    if not where_expr:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'where' expression cannot be empty or whitespace",
            f"pipeline.steps[{index}].where"
        )

    # Validate expression syntax
    validate_expression(where_expr, f"pipeline.steps[{index}].where")

    result = {"op": op, "where": where_expr}
    return result


def _validate_map(step, index, op):
    """Validate and normalize map operation."""
    if "as" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'map' operation requires 'as' field",
            f"pipeline.steps[{index}]"
        )

    as_field = step["as"].strip()
    if not as_field:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'as' field cannot be empty or whitespace",
            f"pipeline.steps[{index}].as"
        )

    if "expr" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'map' operation requires 'expr' field",
            f"pipeline.steps[{index}]"
        )

    expr = step["expr"].strip()
    if not expr:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'expr' field cannot be empty or whitespace",
            f"pipeline.steps[{index}].expr"
        )

    # Validate expression syntax
    validate_expression(expr, f"pipeline.steps[{index}].expr")

    result = {"op": op, "as": as_field, "expr": expr}
    return result


def _validate_rename(step, index, op):
    """Validate and normalize rename operation."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step

    if not (has_from_to or has_mapping):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'rename' operation requires either ('from' and 'to') or 'mapping' field",
            f"pipeline.steps[{index}]"
        )

    if has_from_to and has_mapping:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'rename' operation cannot have both 'from'/'to' and 'mapping' fields",
            f"pipeline.steps[{index}]"
        )

    if has_from_to:
        from_field = step["from"]
        to_field = step["to"]

        if not isinstance(from_field, str) or not isinstance(to_field, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'from' and 'to' must be strings",
                f"pipeline.steps[{index}].from"
            )

        # Trim the from/to fields
        from_field = from_field.strip()
        to_field = to_field.strip()

        if not from_field:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'from' field cannot be empty or whitespace",
                f"pipeline.steps[{index}].from"
            )

        if not to_field:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'to' field cannot be empty or whitespace",
                f"pipeline.steps[{index}].to"
            )

        mapping = {from_field: to_field}
        result = {"op": op, "mapping": mapping}
    else:
        mapping = step["mapping"]
        if not isinstance(mapping, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'mapping' must be an object",
                f"pipeline.steps[{index}].mapping"
            )

        # Preserve keys/values exactly (spaces are significant for column names)
        new_mapping = {}
        for k, v in mapping.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: mapping keys and values must be strings",
                    f"pipeline.steps[{index}].mapping"
                )
            new_mapping[k] = v

        result = {"op": op, "mapping": new_mapping}

    return result


def _validate_limit(step, index, op):
    """Validate and normalize limit operation."""
    if "n" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'limit' operation requires 'n' field",
            f"pipeline.steps[{index}]"
        )

    n = step["n"]
    if not isinstance(n, int):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'n' must be an integer",
            f"pipeline.steps[{index}].n"
        )

    if n < 0:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'n' must be >= 0",
            f"pipeline.steps[{index}].n"
        )

    result = {"op": op, "n": n}
    return result


# Expression tokenization and validation - order matters: BOOLEAN_OP before OPERATOR and IDENTIFIER!
# The regex engine tries alternatives in order, so more specific patterns must come first.
EXPRESSION_TOKEN_PATTERN = re.compile(
    r'''
        (?P<NUMBER>\d+(?:\.\d+)?)
      | (?P<STRING>"[^"]*"|'[^']*')
      | (?P<BOOLEAN_OP>\band\b|\bor\b|\bnot\b)
      | (?P<OPERATOR>>=|<=|!=|==|\+|>|<|-|\*|/)
      | (?P<IDENTIFIER>[a-zA-Z_][a-zA-Z0-9_]*)
      | (?P<PAREN>[()])
      | (?P<WHITESPACE>\s+)
    ''',
    re.VERBOSE
)

EXPRESSION_OPERATORS = {"+", "-", "*", "/", ">", "<", ">=", "<=", "==", "!=", "and", "or", "not"}


def validate_expression(expr, path):
    """Validate expression syntax.

    Args:
        expr: The expression string to validate
        path: Path for error reporting

    Raises:
        ETLValidationError: If the expression has syntax errors
    """
    if not expr:
        return

    # Check for consecutive operators (except -- which could be unary minus)
    if re.search(r'[+\-*/>=<]{2,}', expr.replace(' -', '-').replace('- ', '-').replace('--', '')):
        pass  # Will catch in tokenization

    tokens = []
    pos = 0
    length = len(expr)

    while pos < length:
        match = EXPRESSION_TOKEN_PATTERN.match(expr, pos)
        if match:
            token_type = match.lastgroup
            value = match.group()
            pos = match.end()

            if token_type == "WHITESPACE":
                continue

            tokens.append((token_type, value))
        else:
            # No match - there's an invalid character or syntax
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Invalid expression syntax",
                path
            )

    # Check for consecutive operators
    for i in range(len(tokens) - 1):
        curr_type, curr_val = tokens[i]
        next_type, next_val = tokens[i + 1]

        if curr_type == "OPERATOR" and next_type == "OPERATOR":
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Consecutive operators in expression",
                path
            )

    # Basic grammar validation: no empty expression after tokenization
    if not tokens:
        raise ETLValidationError(
            "BAD_EXPR",
            "ETL_ERROR: Empty expression",
            path
        )


def main():
    """Main entry point."""
    try:
        data = parse_request()

        pipeline = data["pipeline"]
        if not isinstance(pipeline, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'pipeline' must be an object",
                "pipeline"
            )

        if "steps" not in pipeline:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'pipeline' must have a 'steps' field",
                "pipeline"
            )

        steps = pipeline["steps"]
        if not isinstance(steps, list):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'steps' must be an array",
                "pipeline.steps"
            )

        # Normalize each step
        normalized_steps = []
        for i, step in enumerate(steps):
            normalized_step = normalize_step(step, i)
            normalized_steps.append(normalized_step)

        output = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }

        print(json.dumps(output))
        sys.exit(0)

    except ETLValidationError as e:
        output = {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }
        print(json.dumps(output))
        sys.exit(1)
    except Exception as e:
        output = {
            "status": "error",
            "error_code": "SCHEMA_VALIDATION_FAILED",
            "message": f"ETL_ERROR: {e}",
            "path": ""
        }
        print(json.dumps(output))
        sys.exit(1)


if __name__ == "__main__":
    main()
