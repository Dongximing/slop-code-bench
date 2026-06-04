#!/usr/bin/env python3
"""ETL Pipeline CLI program that reads JSON from STDIN, validates and executes normalized ETL pipeline."""

import argparse
import json
import re
import sys
from typing import Any


class ETLValidationError(Exception):
    """Exception raised for ETL pipeline validation errors."""
    def __init__(self, error_code, message, path):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(self.message)


class ETLEvaluationError(Exception):
    """Exception raised for ETL pipeline execution errors."""
    def __init__(self, error_code, message, path):
        self.error_code = error_code
        self.message = message
        self.path = path
        super().__init__(self.message)


def validate_def_name(name, path):
    """Validate a definition name matches the required pattern."""
    if not isinstance(name, str):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: definition name must be a string",
            path
        )
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', name):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: definition name must match ^[A-Za-z_][A-Za-z0-9_]*$",
            path
        )


def validate_library_structure(library, path):
    """Validate the library structure with namespaced definitions."""
    if not isinstance(library, dict):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'library' must be an object",
            path
        )

    for ns, ns_value in library.items():
        if not isinstance(ns, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: namespace must be a string",
                path
            )

        if not ns.strip():
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: namespace cannot be empty or whitespace",
                path
            )

        ns_path = f"{path}.{ns}" if path else f"library.{ns}"

        if not isinstance(ns_value, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: namespace value must be an object",
                ns_path
            )

        if "defs" not in ns_value:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: namespace missing 'defs' field",
                ns_path
            )

        defs = ns_value["defs"]
        if not isinstance(defs, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'defs' must be an object",
                f"{ns_path}.defs"
            )

        for def_name, def_value in defs.items():
            validate_def_name(def_name, f"{ns_path}.defs[{def_name}]")
            if not isinstance(def_value, dict):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: definition must be an object",
                    f"{ns_path}.defs[{def_name}]"
                )
            if "steps" not in def_value:
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: definition missing 'steps' field",
                    f"{ns_path}.defs[{def_name}]"
                )
            if not isinstance(def_value["steps"], list):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: definition 'steps' must be an array",
                    f"{ns_path}.defs[{def_name}].steps"
                )


def validate_compose_item(item, index, library):
    """Validate a single compose item (ref or steps)."""
    if not isinstance(item, dict):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: compose item must be an object",
            f"compose[{index}]"
        )

    has_ref = "ref" in item
    has_steps = "steps" in item

    if not (has_ref or has_steps):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: compose item must have either 'ref' or 'steps' field",
            f"compose[{index}]"
        )

    if has_ref and has_steps:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: compose item cannot have both 'ref' and 'steps' fields",
            f"compose[{index}]"
        )

    if has_ref:
        ref = item["ref"]
        if not isinstance(ref, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'ref' must be a string",
                f"compose[{index}].ref"
            )

        # Validate ref format: "ns:name"
        if ":" not in ref:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'ref' must be in format 'ns:name'",
                f"compose[{index}].ref"
            )

        parts = ref.split(":", 1)
        if len(parts) != 2:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'ref' must be in format 'ns:name'",
                f"compose[{index}].ref"
            )

        ns, name = parts
        if not ns or not name:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: namespace and name cannot be empty",
                f"compose[{index}].ref"
            )

        # Validate params if present
        if "params" in item:
            params = item["params"]
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'params' must be an object",
                    f"compose[{index}].params"
                )

        # Check if namespace and reference exist
        if library is not None:
            if ns not in library:
                raise ETLValidationError(
                    "UNKNOWN_NAMESPACE",
                    f"ETL_ERROR: namespace '{ns}' not found in library",
                    f"compose[{index}].ref"
                )
            if name not in library[ns]["defs"]:
                raise ETLValidationError(
                    "UNKNOWN_LIB_REF",
                    f"ETL_ERROR: definition '{name}' not found in namespace '{ns}'",
                    f"compose[{index}].ref"
                )
    else:
        # Validate inline steps
        steps = item["steps"]
        if not isinstance(steps, list):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'steps' must be an array",
                f"compose[{index}].steps"
            )
        for j, step in enumerate(steps):
            try:
                normalize_step(step, j)
            except ETLValidationError as e:
                # Re-raise with updated path for nested steps
                if e.path:
                    new_path = f"compose[{index}].steps[{j}]"
                    if not e.path.startswith("pipeline.steps["):
                        new_path = f"{new_path}.{e.path}"
                    else:
                        # Extract the step index and append
                        match = re.match(r'pipeline\.steps\[(\d+)\]\.(.*)', e.path)
                        if match:
                            new_path = f"{new_path}.{match.group(2)}"
                        else:
                            new_path = f"{new_path}.{e.path}"
                    raise ETLValidationError(e.error_code, e.message, new_path)
                raise


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

    # Validate library if present
    library = None
    if "library" in data:
        library = data["library"]
        validate_library_structure(library, "library")

    # Validate compose if present
    if "compose" in data:
        compose = data["compose"]
        if not isinstance(compose, list):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'compose' must be an array",
                "compose"
            )

        for i, item in enumerate(compose):
            validate_compose_item(item, i, library)

    # Check mutual exclusion of compose and pipeline.steps
    has_compose = "compose" in data
    has_pipeline_steps = False
    if "pipeline" in data:
        pipeline = data["pipeline"]
        if isinstance(pipeline, dict) and "steps" in pipeline:
            has_pipeline_steps = True

    if has_compose and has_pipeline_steps:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'compose' and 'pipeline.steps' are mutually exclusive",
            "pipeline"
        )

    return data, library


def resolve_compose_to_steps(compose, library):
    """
    Convert compose items to a flat list of normalized steps.

    Handles:
    - Library references (ref) with params substitution
    - Inline steps

    Library steps use definitions from previous checkpoints, so params references
    remain as expressions in the normalized output (per checkpoint 2 rules).
    """
    normalized_steps = []

    for compose_item in compose:
        if "ref" in compose_item:
            # Resolve library reference
            ref = compose_item["ref"]
            params = compose_item.get("params", {}) or {}

            ns, name = ref.split(":", 1)
            lib_def = library[ns]["defs"][name]

            # Add all steps from the library definition
            for step in lib_def["steps"]:
                normalized_steps.append(step)
        else:
            # Inline steps - add them directly
            for step in compose_item["steps"]:
                normalized_steps.append(step)

    return normalized_steps


def normalize_step(step, index):
    """Normalize a single step object."""
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

    op = step["op"].strip().lower()
    if not op:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: Operation name cannot be empty or whitespace",
            f"pipeline.steps[{index}].op"
        )

    valid_ops = {
        "select": _validate_select,
        "filter": _validate_filter,
        "map": _validate_map,
        "rename": _validate_rename,
        "limit": _validate_limit,
        "branch": _validate_branch,
        "call": _validate_call,
    }

    if op not in valid_ops:
        raise ETLValidationError(
            "UNKNOWN_OP",
            f"ETL_ERROR: unsupported op '{op}'",
            f"pipeline.steps[{index}].op"
        )

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
        if not col.strip():
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                f"ETL_ERROR: column[{i}] cannot be empty or whitespace",
                f"pipeline.steps[{index}].columns[{i}]"
            )
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


def _validate_call(step, index, op):
    """Validate and normalize call operation."""
    if "name" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'call' operation requires 'name' field",
            f"pipeline.steps[{index}]"
        )

    name = step["name"]
    if not isinstance(name, str):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'call' operation 'name' must be a string",
            f"pipeline.steps[{index}].name"
        )

    # Validate the name pattern
    validate_def_name(name, f"pipeline.steps[{index}].name")

    # Validate params if present
    params = step.get("params", {})
    if params is None:
        params = {}

    if not isinstance(params, dict):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'call' operation 'params' must be an object",
            f"pipeline.steps[{index}].params"
        )

    # Validate param values are scalars or arrays (no nested objects)
    for param_key, param_value in params.items():
        if not isinstance(param_key, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'call' operation param keys must be strings",
                f"pipeline.steps[{index}].params"
            )
        # Check if value is a scalar or array of scalars
        if isinstance(param_value, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'call' operation params cannot have nested objects",
                f"pipeline.steps[{index}].params[{param_key}]"
            )

    result = {"op": op, "name": name, "params": params}
    return result


def _validate_branch(step, index, op):
    """Validate and normalize branch operation."""
    if "branches" not in step:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'branch' operation requires 'branches' field",
            f"pipeline.steps[{index}]"
        )

    branches = step["branches"]
    if not isinstance(branches, list):
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'branches' must be an array",
            f"pipeline.steps[{index}].branches"
        )

    if len(branches) == 0:
        raise ETLValidationError(
            "SCHEMA_VALIDATION_FAILED",
            "ETL_ERROR: 'branches' must not be empty",
            f"pipeline.steps[{index}].branches"
        )

    has_otherwise = False
    normalized_branches = []

    for j, branch in enumerate(branches):
        if not isinstance(branch, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: branch must be an object",
                f"pipeline.steps[{index}].branches[{j}]"
            )

        # Validate id (optional string)
        if "id" in branch:
            branch_id = branch["id"]
            if not isinstance(branch_id, str):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: branch 'id' must be a string",
                    f"pipeline.steps[{index}].branches[{j}].id"
                )
            if not branch_id.strip():
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: branch 'id' cannot be empty or whitespace",
                    f"pipeline.steps[{index}].branches[{j}].id"
                )

        # Validate when
        if "when" not in branch:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: branch requires 'when' field",
                f"pipeline.steps[{index}].branches[{j}]"
            )

        when_expr = branch["when"]
        if not isinstance(when_expr, str):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: branch 'when' must be a string",
                f"pipeline.steps[{index}].branches[{j}].when"
            )

        is_otherwise = when_expr.strip().lower() == "otherwise"

        # Check for otherwise rules
        if is_otherwise:
            if has_otherwise:
                raise ETLValidationError(
                    "MALFORMED_STEP",
                    "ETL_ERROR: multiple 'otherwise' branches not allowed",
                    f"pipeline.steps[{index}].branches"
                )
            has_otherwise = True

        # Otherwise must be last
        if has_otherwise and j < len(branches) - 1:
            raise ETLValidationError(
                "MALFORMED_STEP",
                "ETL_ERROR: 'otherwise' branch must be last",
                f"pipeline.steps[{index}].branches"
            )

        # Validate steps if present
        if "steps" not in branch:
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: branch requires 'steps' field",
                f"pipeline.steps[{index}].branches[{j}]"
            )

        branch_steps = branch["steps"]
        if not isinstance(branch_steps, list):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: branch 'steps' must be an array",
                f"pipeline.steps[{index}].branches[{j}].steps"
            )

        # Validate nested steps with correct path
        normalized_branch_steps = []
        for k, nested_step in enumerate(branch_steps):
            try:
                normalized_step = normalize_step(nested_step, k)
                normalized_branch_steps.append(normalized_step)
            except ETLValidationError as e:
                # Re-raise with updated path for nested steps
                if e.path:
                    # Prepend the branch path
                    new_path = f"pipeline.steps[{index}].branches[{j}].steps[{k}]"
                    if not e.path.startswith("pipeline.steps["):
                        new_path = f"{new_path}.{e.path}"
                    else:
                        new_path = f"{new_path}.{e.path.split('pipeline.steps[', 1)[-1]}"
                    raise ETLValidationError(e.error_code, e.message, new_path)
                raise

        normalized_branch = {
            "id": branch.get("id", "").strip() if "id" in branch else "",
            "when": when_expr.strip(),
            "steps": normalized_branch_steps
        }
        normalized_branches.append(normalized_branch)

    # Validate merge strategy
    merge_strategy = "concat"  # default
    if "merge" in step:
        merge = step["merge"]
        if not isinstance(merge, dict):
            raise ETLValidationError(
                "SCHEMA_VALIDATION_FAILED",
                "ETL_ERROR: 'merge' must be an object",
                f"pipeline.steps[{index}].merge"
            )

        if "strategy" not in merge:
            # Default to "concat" if missing
            merge_strategy = "concat"
        else:
            strategy = merge["strategy"]
            if not isinstance(strategy, str):
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'merge.strategy' must be a string",
                    f"pipeline.steps[{index}].merge.strategy"
                )
            if strategy != "concat":
                raise ETLValidationError(
                    "SCHEMA_VALIDATION_FAILED",
                    "ETL_ERROR: 'merge.strategy' must be 'concat'",
                    f"pipeline.steps[{index}].merge.strategy"
                )
            merge_strategy = strategy

    result = {
        "op": op,
        "branches": normalized_branches,
        "merge": {"strategy": merge_strategy}
    }
    return result


# Expression tokenization and validation
EXPRESSION_TOKEN_PATTERN = re.compile(
    r'''
        (?P<NUMBER>\d+(?:\.\d+)?)
      | (?P<STRING>"[^"]*"|'[^']*')
      | (?P<BOOLEAN_OP>\band\b|\bor\b|\bnot\b)
      | (?P<OPERATOR>>=|<=|!=|==|>|<|\+|-|\*|/|\.)
      | (?P<IDENTIFIER>[a-zA-Z_][a-zA-Z0-9_]*)
      | (?P<PAREN>[()])
      | (?P<WHITESPACE>\s+)
    ''',
    re.VERBOSE
)

SUPPORTED_OPERATORS = {"+", "-", "*", "/", ">", "<", ">=", "<=", "==", "!=", "and", "or", "not"}


def validate_expression(expr, path):
    """Validate expression syntax."""
    if not expr:
        return

    # First check for unsupported operators like **, ^^
    if re.search(r'[\*\^]{2,}', expr):
        unsupported = re.search(r'[\*\^]{2,}', expr)
        raise ETLValidationError(
            "BAD_EXPR",
            f"ETL_ERROR: unsupported operator '{unsupported.group()}'",
            path
        )

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
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Invalid expression syntax",
                path
            )

    # Check for unsupported operators (single char like ^)
    remaining = EXPRESSION_TOKEN_PATTERN.sub('', expr)
    if remaining:
        for char in remaining:
            if not char.isspace():
                raise ETLValidationError(
                    "BAD_EXPR",
                    f"ETL_ERROR: unsupported operator '{char}'",
                    path
                )

    # Check for consecutive operators (valid operators like -* would be caught above)
    for i in range(len(tokens) - 1):
        curr_type, curr_val = tokens[i]
        next_type, next_val = tokens[i + 1]

        if curr_type == "OPERATOR" and next_type == "OPERATOR":
            raise ETLValidationError(
                "BAD_EXPR",
                "ETL_ERROR: Consecutive operators in expression",
                path
            )

    if not tokens:
        raise ETLValidationError(
            "BAD_EXPR",
            "ETL_ERROR: Empty expression",
            path
        )


class ExpressionEvaluator:
    """Evaluates ETL filter/map expressions."""

    def __init__(self, expr: str):
        self.expr = expr
        self.tokens = []
        self.pos = 0
        self._params = None
        self._tokenize()

    def set_params(self, params: dict):
        """Set the params object for member access."""
        self._params = params if params is not None else {}

    def _tokenize(self):
        """Tokenize the expression."""
        self.tokens = []
        pos = 0
        length = len(self.expr)

        while pos < length:
            match = EXPRESSION_TOKEN_PATTERN.match(self.expr, pos)
            if match:
                token_type = match.lastgroup
                value = match.group()
                pos = match.end()

                if token_type == "WHITESPACE":
                    continue

                # Convert literals to proper types
                if token_type == "NUMBER":
                    if '.' in value:
                        self.tokens.append(("NUMBER", float(value)))
                    else:
                        self.tokens.append(("NUMBER", int(value)))
                elif token_type == "STRING":
                    # Remove quotes
                    self.tokens.append(("STRING", value[1:-1]))
                elif token_type == "BOOLEAN_OP":
                    self.tokens.append(("BOOLEAN_OP", value.lower()))
                elif token_type == "OPERATOR":
                    self.tokens.append(("OPERATOR", value))
                elif token_type == "IDENTIFIER":
                    self.tokens.append(("IDENTIFIER", value))
                elif token_type == "PAREN":
                    self.tokens.append(("PAREN", value))
            else:
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    f"ETL_ERROR: Invalid expression syntax in '{self.expr}'",
                    ""
                )

    def evaluate(self, row: dict) -> Any:
        """Evaluate the expression against a row."""
        self.pos = 0
        result = self._parse_expression()
        if self.pos < len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected tokens after expression",
                ""
            )
        return result

    def _parse_expression(self, precedence=0):
        """Parse expression with given minimum precedence."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        # Parse left operand
        left = self._parse_primary()

        while self.pos < len(self.tokens):
            # Check for operator
            if self.tokens[self.pos][0] == "OPERATOR":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                # Right-associative operators
                if op in ('and', 'or'):
                    right_prec = op_prec
                else:
                    right_prec = op_prec

                self.pos += 1
                right = self._parse_expression(right_prec)
                left = self._apply_operator(left, op, right)
            elif self.tokens[self.pos][0] == "BOOLEAN_OP":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression(op_prec)
                left = self._apply_operator(left, op, right)
            else:
                break

        return left

    def _parse_primary(self):
        """Parse primary expression (literal, identifier, parenthesized)."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        token_type, value = self.tokens[self.pos]

        # Handle unary operators
        if token_type == "OPERATOR" and value == "-":
            self.pos += 1
            operand = self._parse_primary()
            if operand is None:
                return None
            return -operand

        if token_type == "BOOLEAN_OP" and value == "not":
            self.pos += 1
            operand = self._parse_expression(self._get_precedence("not"))
            return not self._to_bool(operand)

        # Parenthesized expression
        if token_type == "PAREN" and value == "(":
            self.pos += 1
            expr = self._parse_expression()
            if self.pos >= len(self.tokens) or self.tokens[self.pos] != ("PAREN", ")"):
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    "ETL_ERROR: Missing closing parenthesis",
                    ""
                )
            self.pos += 1
            return expr

        # Literal or identifier
        self.pos += 1

        if token_type == "NUMBER":
            return value
        elif token_type == "STRING":
            return value
        elif token_type == "IDENTIFIER":
            return value  # Return as identifier name, will be resolved later
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected token '{value}'",
                ""
            )

    def _get_precedence(self, op):
        """Get operator precedence for binary operators."""
        prec = {
            'or': 1,
            'and': 2,
            'not': 3,
            '==': 4, '!=': 4,
            '<': 5, '>': 5, '<=': 5, '>=': 5,
            '+': 6, '-': 6,
            '*': 7, '/': 7,
            # Member access has highest precedence but is handled separately
        }
        return prec.get(op, 0)

    def _parse_primary_with_resolution(self, row: dict):
        """Parse primary and resolve identifiers."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        token_type, value = self.tokens[self.pos]

        # Handle unary minus
        if token_type == "OPERATOR" and value == "-":
            self.pos += 1
            operand = self._parse_primary_with_resolution(row)
            if operand is None:
                return None
            return -operand

        # Handle unary not
        if token_type == "BOOLEAN_OP" and value == "not":
            self.pos += 1
            operand = self._parse_expression_with_resolution(row, self._get_precedence("not"))
            return not self._to_bool(operand)

        # Parenthesized expression
        if token_type == "PAREN" and value == "(":
            self.pos += 1
            expr = self._parse_expression_with_resolution(row)
            if self.pos >= len(self.tokens) or self.tokens[self.pos] != ("PAREN", ")"):
                raise ETLEvaluationError(
                    "BAD_EXPR",
                    "ETL_ERROR: Missing closing parenthesis",
                    ""
                )
            self.pos += 1
            return expr

        # Literal or identifier
        self.pos += 1

        if token_type == "NUMBER":
            return value
        elif token_type == "STRING":
            return value
        elif token_type == "IDENTIFIER":
            base_value = None

            # Check if this is the special 'params' object
            if value == "params" and self._params is not None:
                base_value = self._params
            else:
                # Regular row field access
                base_value = row.get(value)

            # Handle member access (e.g., params.mean, params.a.b)
            while (self.pos < len(self.tokens) and
                   self.tokens[self.pos][0] == "OPERATOR" and
                   self.tokens[self.pos][1] == "."):
                self.pos += 1
                if self.pos >= len(self.tokens):
                    raise ETLEvaluationError(
                        "BAD_EXPR",
                        "ETL_ERROR: Expected member name after '.'",
                        ""
                    )
                member_type, member_name = self.tokens[self.pos]
                if member_type != "IDENTIFIER":
                    raise ETLEvaluationError(
                        "BAD_EXPR",
                        "ETL_ERROR: Expected identifier after '.'",
                        ""
                    )
                self.pos += 1

                # Access the member if base_value is a dict
                if isinstance(base_value, dict):
                    base_value = base_value.get(member_name)
                else:
                    base_value = None

            return base_value
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unexpected token '{value}'",
                ""
            )

    def _parse_and_resolve(self, row: dict):
        """Parse expression and resolve identifiers against row."""
        self.pos = 0
        result = self._parse_expression_with_resolution(row)
        if self.pos < len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected tokens after expression",
                ""
            )
        return result

    def _parse_expression_with_resolution(self, row: dict, precedence=0):
        """Parse expression with resolution against row."""
        if self.pos >= len(self.tokens):
            raise ETLEvaluationError(
                "BAD_EXPR",
                "ETL_ERROR: Unexpected end of expression",
                ""
            )

        left = self._parse_primary_with_resolution(row)

        while self.pos < len(self.tokens):
            if self.tokens[self.pos][0] == "OPERATOR":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression_with_resolution(row, op_prec)
                left = self._apply_operator(left, op, right)
            elif self.tokens[self.pos][0] == "BOOLEAN_OP":
                op = self.tokens[self.pos][1]
                op_prec = self._get_precedence(op)
                if op_prec <= precedence:
                    break

                self.pos += 1
                right = self._parse_expression_with_resolution(row, op_prec)
                left = self._apply_operator(left, op, right)
            else:
                break

        return left

    def _apply_operator(self, left, op, right):
        """Apply an operator to operands."""
        # Handle null in operands
        if left is None or right is None:
            if op in ('==', '!=', '<', '>', '<=', '>='):
                return False
            elif op in ('and', 'or', 'not'):
                return self._to_bool(left or right)
            else:
                return None

        if op == '+':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left + right
            return None
        elif op == '-':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left - right
            return None
        elif op == '*':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left * right
            return None
        elif op == '/':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                if right == 0:
                    return None
                return left / right
            return None
        elif op == '==':
            return left == right
        elif op == '!=':
            return left != right
        elif op == '<':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left < right
            return False
        elif op == '>':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left > right
            return False
        elif op == '<=':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left <= right
            return False
        elif op == '>=':
            if isinstance(left, (int, float)) and isinstance(right, (int, float)):
                return left >= right
            return False
        elif op == 'and':
            return self._to_bool(left) and self._to_bool(right)
        elif op == 'or':
            return self._to_bool(left) or self._to_bool(right)
        else:
            raise ETLEvaluationError(
                "BAD_EXPR",
                f"ETL_ERROR: Unknown operator '{op}'",
                ""
            )

    def _to_bool(self, value):
        """Convert value to boolean (null is falsy)."""
        if value is None:
            return False
        return bool(value)


def execute_select_step(data: list, step: dict) -> list:
    """Execute select operation."""
    columns = step["columns"]
    result = []

    for row in data:
        new_row = {}
        for col in columns:
            if col not in row:
                raise ETLEvaluationError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{col}' not found in row",
                    f"pipeline.steps[{step.get('_index', 0)}].columns[{columns.index(col)}]"
                )
            new_row[col] = row[col]
        result.append(new_row)

    return result


def execute_filter_step(data: list, step: dict, params=None) -> list:
    """Execute filter operation."""
    evaluator = ExpressionEvaluator(step["where"])
    evaluator.set_params(params)
    result = []

    for row in data:
        try:
            value = evaluator._parse_and_resolve(row)
            if evaluator._to_bool(value):
                result.append(row)
        except Exception as e:
            if isinstance(e, ETLEvaluationError):
                raise
            raise ETLEvaluationError(
                "EXECUTION_FAILED",
                f"ETL_ERROR: Failed to evaluate filter: {e}",
                f"pipeline.steps[{step.get('_index', 0)}].where"
            )

    return result


def execute_map_step(data: list, step: dict, params=None) -> list:
    """Execute map operation."""
    evaluator = ExpressionEvaluator(step["expr"])
    evaluator.set_params(params)
    result = []

    for row in data:
        new_row = row.copy()
        try:
            value = evaluator._parse_and_resolve(new_row)
            new_row[step["as"]] = value
        except Exception as e:
            if isinstance(e, ETLEvaluationError):
                raise
            raise ETLEvaluationError(
                "EXECUTION_FAILED",
                f"ETL_ERROR: Failed to evaluate map expression: {e}",
                f"pipeline.steps[{step.get('_index', 0)}].expr"
            )
        result.append(new_row)

    return result


def execute_rename_step(data: list, step: dict) -> list:
    """Execute rename operation."""
    mapping = step["mapping"]
    result = []

    for row in data:
        new_row = {}
        # Copy all fields first
        for k, v in row.items():
            new_row[k] = v

        # Apply renaming in iteration order
        # Later mappings can overwrite earlier ones
        for src, tgt in mapping.items():
            if src not in new_row:
                raise ETLEvaluationError(
                    "MISSING_COLUMN",
                    f"ETL_ERROR: column '{src}' not found in row",
                    f"pipeline.steps[{step.get('_index', 0)}].mapping.{src}"
                )
            new_row[tgt] = new_row[src]
            del new_row[src]

        result.append(new_row)

    return result


def execute_limit_step(data: list, step: dict) -> list:
    """Execute limit operation."""
    n = step["n"]
    return data[:n]


def execute_branch_step(data: list, step: dict, execute_pipeline_func, defs: dict = None, params: dict = None) -> list:
    """Execute branch operation - route rows to sub-pipelines based on conditions."""
    branches = step["branches"]
    outer_step_index = step.get("_index", 0)

    # First-pass: group rows by matching branch (first match wins)
    branch_results = [[] for _ in range(len(branches))]

    for row in data:
        for j, branch in enumerate(branches):
            when_expr = branch["when"]

            # Check if it's "otherwise" branch
            if when_expr == "otherwise":
                # Route to this branch
                branch_results[j].append(row)
                break

            # Evaluate the condition
            try:
                evaluator = ExpressionEvaluator(when_expr)
                value = evaluator._parse_and_resolve(row)
                if evaluator._to_bool(value):
                    # Route to this branch
                    branch_results[j].append(row)
                    break
            except ETLEvaluationError as e:
                raise
            except Exception as e:
                raise ETLEvaluationError(
                    "EXECUTION_FAILED",
                    f"ETL_ERROR: Failed to evaluate branch condition: {e}",
                    f"pipeline.steps[{outer_step_index}].branches[{j}].when"
                )

    # Execute each branch's sub-pipeline and concatenate results
    result = []
    for j, branch in enumerate(branches):
        branch_input = branch_results[j]
        if not branch_input:
            continue

        branch_steps = branch["steps"]
        if not branch_steps:
            # No steps, just add rows as-is
            result.extend(branch_input)
            continue

        try:
            # Execute the sub-pipeline
            branch_output, _ = execute_pipeline_func(branch_input, branch_steps, defs, params)
        except ETLEvaluationError as e:
            # Re-raise with updated path for nested errors - prepend branch path
            if e.path:
                # The path from sub-pipeline will start with pipeline.steps[...
                # We need to transform it to: pipeline.steps[<outer>].branches[<j>].steps[<k>]...
                import re
                match = re.match(r'^pipeline\.steps\[(\d+)\]\.(.*)', e.path)
                if match:
                    k = match.group(1)
                    rest = match.group(2)
                    new_path = f"pipeline.steps[{outer_step_index}].branches[{j}].steps[{k}].{rest}"
                    raise ETLEvaluationError(e.error_code, e.message, new_path)
                else:
                    raise ETLEvaluationError(e.error_code, e.message, e.path)
            raise

    return result


def execute_call_step(data: list, step: dict, defs: dict) -> list:
    """Execute a call to a named sub-pipeline."""
    name = step["name"]
    params = step.get("params", {})

    if name not in defs:
        raise ETLEvaluationError(
            "UNKNOWN_DEF",
            f"ETL_ERROR: definition '{name}' not found",
            f"defs[{name}]"
        )

    # Check for direct recursion
    if "_calling" in defs[name]:
        raise ETLEvaluationError(
            "RECURSION_FORBIDDEN",
            "ETL_ERROR: recursive call detected",
            f"defs[{name}].steps[0].name"
        )

    # Mark this definition as being called (for recursion detection)
    defs[name]["_calling"] = True

    try:
        sub_steps = defs[name]["steps"]
        result, _ = execute_pipeline(data, sub_steps, defs, params)
    finally:
        # Unmark the definition
        del defs[name]["_calling"]

    return result


def execute_pipeline(dataset: list, steps: list, defs: dict = None, params: dict = None) -> tuple:
    """Execute pipeline and return (data, metrics)."""
    if defs is None:
        defs = {}
    if params is None:
        params = {}

    rows_in = len(dataset)
    data = dataset

    for i, step in enumerate(steps):
        step_with_index = dict(step)
        step_with_index["_index"] = i

        op = step["op"]

        if op == "select":
            data = execute_select_step(data, step_with_index)
        elif op == "filter":
            data = execute_filter_step(data, step_with_index, params)
        elif op == "map":
            data = execute_map_step(data, step_with_index, params)
        elif op == "rename":
            data = execute_rename_step(data, step_with_index)
        elif op == "limit":
            data = execute_limit_step(data, step_with_index)
        elif op == "branch":
            data = execute_branch_step(data, step_with_index, execute_pipeline, defs, params)
        elif op == "call":
            data = execute_call_step(data, step_with_index, defs)

    rows_out = len(data)
    metrics = {"rows_in": rows_in, "rows_out": rows_out}

    return data, metrics


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='ETL Pipeline Executor - Process JSON from STDIN'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='Execute the pipeline (default: false, returns normalized form)'
    )

    args = parser.parse_args()

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

        # Get defs if present (default to empty dict)
        defs = data.get("defs", {})
        if not isinstance(defs, dict):
            defs = {}

        # Normalize each step
        normalized_steps = []
        for i, step in enumerate(steps):
            normalized_step = normalize_step(step, i)
            normalized_steps.append(normalized_step)

        if not args.execute:
            # Return normalized form (checkpoint 1 behavior)
            output = {
                "status": "ok",
                "normalized": {
                    "steps": normalized_steps
                }
            }
        else:
            # Execute the pipeline
            dataset = data["dataset"]
            data_result, metrics = execute_pipeline(dataset, normalized_steps, defs)

            output = {
                "status": "ok",
                "data": data_result,
                "metrics": metrics
            }

        print(json.dumps(output))
        sys.exit(0)

    except Exception as e:
        output = {
            "status": "error",
            "error_code": "EXECUTION_FAILED",
            "message": f"ETL_ERROR: {e}",
            "path": ""
        }
        print(json.dumps(output))
        sys.exit(1)


def main_legacy_pipeline(data, args):
    """Handle the legacy pipeline.defs format (for backward compatibility)."""
    pipeline = data["pipeline"]
    steps = pipeline["steps"]
    defs = data.get("defs", {})

    # Normalize each step
    normalized_steps = []
    for i, step in enumerate(steps):
        normalized_step = normalize_step(step, i)
        normalized_steps.append(normalized_step)

    if not args.execute:
        # Return normalized form (checkpoint 1 behavior)
        output = {
            "status": "ok",
            "normalized": {
                "steps": normalized_steps
            }
        }
    else:
        # Execute the pipeline
        dataset = data["dataset"]
        data_result, metrics = execute_pipeline(dataset, normalized_steps, defs)

        output = {
            "status": "ok",
            "data": data_result,
            "metrics": metrics
        }

    print(json.dumps(output))
    sys.exit(0)


def main_compose_pipeline(data, library, args):
    """Handle the new library.compose format."""
    compose = data["compose"]
    dataset = data["dataset"]

    # Convert compose to normalized steps
    normalized_steps = resolve_compose_to_steps(compose, library)

    # Normalize each step (run through validation again to get proper format)
    final_steps = []
    for i, step in enumerate(normalized_steps):
        try:
            normalized_step = normalize_step(step, i)
            final_steps.append(normalized_step)
        except ETLValidationError as e:
            # Update path to reflect compose origin
            if e.path:
                e.path = e.path.replace("pipeline.steps", "compose")
            raise

    if not args.execute:
        # Return normalized form with params as expressions
        output = {
            "status": "ok",
            "normalized": {
                "steps": final_steps
            }
        }
    else:
        # Execute the pipeline - build defs from library for call steps
        defs = {}
        if library:
            for ns, ns_value in library.items():
                for def_name, def_value in ns_value["defs"].items():
                    # Use namespaced key for definitions
                    full_name = f"{ns}:{def_name}"
                    defs[full_name] = {"steps": def_value["steps"]}

        data_result, metrics = execute_pipeline(dataset, final_steps, defs)

        output = {
            "status": "ok",
            "data": data_result,
            "metrics": metrics
        }

    print(json.dumps(output))
    sys.exit(0)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='ETL Pipeline Executor - Process JSON from STDIN'
    )
    parser.add_argument(
        '--execute',
        action='store_true',
        default=False,
        help='Execute the pipeline (default: false, returns normalized form)'
    )

    args = parser.parse_args()

    try:
        data, library = parse_request()

        # Determine which format to use
        if "compose" in data:
            # New format: library + compose
            main_compose_pipeline(data, library, args)
        else:
            # Legacy format: pipeline.defs
            main_legacy_pipeline(data, args)

    except ETLValidationError as e:
        output = {
            "status": "error",
            "error_code": e.error_code,
            "message": e.message,
            "path": e.path
        }
        print(json.dumps(output))
        sys.exit(1)
    except ETLEvaluationError as e:
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
            "error_code": "EXECUTION_FAILED",
            "message": f"ETL_ERROR: {e}",
            "path": ""
        }
        print(json.dumps(output))
        sys.exit(1)


if __name__ == "__main__":
    main()