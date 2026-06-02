#!/usr/bin/env python3
"""Pipeline parsing and normalization."""

import re
import json
from errors import error

IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def validate_expression(expr, index, field):
    """Validate expression doesn't contain unsafe operators."""
    if re.search(r'(\+\+|--|\*\*|/\/<\|\||&&|\^%|<>\s*<>)', expr):
        error("BAD_EXPR", "unsupported operator", f"pipeline.steps[{index}].{field}")
    if not expr.strip():
        error("SCHEMA_VALIDATION_FAILED", f"{field} expression is empty", f"pipeline.steps[{index}].{field}")


def normalize_defs(defs):
    """Normalize library definitions."""
    normalized = {}
    for name, def_obj in defs.items():
        validate_definition_name(name, f"defs[{name}]")
        if not isinstance(def_obj, dict):
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}' must be an object", f"defs[{name}]")
        if "steps" not in def_obj:
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}' is missing required 'steps' field", f"defs[{name}]")
        steps = def_obj["steps"]
        if not isinstance(steps, list):
            error("SCHEMA_VALIDATION_FAILED", f"definition '{name}'.steps must be an array", f"defs[{name}].steps")
        normalized[name] = {"steps": [normalize_step(step, i) for i, step in enumerate(steps)]}
    return normalized


def resolve_library_ref(ref, library, index, field_path):
    """Resolve library reference 'ns:name' to steps."""
    if not isinstance(ref, str):
        error("SCHEMA_VALIDATION_FAILED", "ref must be a string", f"{field_path}")

    if ":" not in ref:
        error("SCHEMA_VALIDATION_FAILED", "ref must be in format 'ns:name'", f"{field_path}")

    parts = ref.split(":", 1)
    if len(parts) != 2:
        error("SCHEMA_VALIDATION_FAILED", "ref must be in format 'ns:name'", f"{field_path}")

    ns, name = parts
    if not ns:
        error("SCHEMA_VALIDATION_FAILED", "namespace cannot be empty", f"{field_path}")
    if not name:
        error("SCHEMA_VALIDATION_FAILED", "definition name cannot be empty", f"{field_path}")

    if ns not in library:
        error("UNKNOWN_NAMESPACE", f"namespace '{ns}' not found in library", f"{field_path}")

    if name not in library[ns]:
        error("UNKNOWN_LIB_REF", f"definition '{name}' not found in namespace '{ns}'", f"{field_path}")

    return library[ns][name]["steps"]


def expand_compose(compose, library, index, field_path, is_execute=True):
    """Expand compose array into flat list of normalized steps."""
    expanded_steps = []
    for i, item in enumerate(compose):
        if not isinstance(item, dict):
            error("SCHEMA_VALIDATION_FAILED", "compose item must be an object", f"compose[{i}]")

        has_ref = "ref" in item
        has_steps = "steps" in item

        if has_ref and has_steps:
            error("SCHEMA_VALIDATION_FAILED", "compose item cannot have both 'ref' and 'steps'", f"compose[{i}]")
        if not has_ref and not has_steps:
            error("SCHEMA_VALIDATION_FAILED", "compose item must have either 'ref' or 'steps'", f"compose[{i}]")

        if has_ref:
            ref = item["ref"]
            params = item.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                error("SCHEMA_VALIDATION_FAILED", "'params' must be an object", f"compose[{i}].params")

            step_list = resolve_library_ref(ref, library, i, f"compose[{i}].ref")
            for j, step in enumerate(step_list):
                step_copy = step.copy()
                if step_copy.get("op") == "call":
                    existing_params = step_copy.get("params", {})
                    merged_params = {**params, **existing_params}
                    step_copy["params"] = merged_params
                else:
                    step_copy["_library_params"] = params
                expanded_steps.append(step_copy)
        else:
            for j, step in enumerate(item["steps"]):
                expanded_steps.append(normalize_step(step, len(expanded_steps)))

    return expanded_steps


# Step Normalizers
def normalize_select(step, index):
    """Normalize select step."""
    from errors import error
    columns = _required(step, "columns", index)
    if not isinstance(columns, list):
        error("SCHEMA_VALIDATION_FAILED", "'columns' must be an array", f"pipeline.steps[{index}].columns")
    for i, col in enumerate(columns):
        if not isinstance(col, str):
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] must be a string", f"pipeline.steps[{index}].columns[{i}]")
        if col == "":
            error("SCHEMA_VALIDATION_FAILED", f"columns[{i}] cannot be empty", f"pipeline.steps[{index}].columns[{i}]")
    return {"op": "select", "columns": columns}


def normalize_filter(step, index):
    """Normalize filter step."""
    where = _non_empty_string(_required(step, "where", index), f"pipeline.steps[{index}].where")
    validate_expression(where, index, "where")
    return {"op": "filter", "where": where}


def normalize_map(step, index):
    """Normalize map step."""
    as_field = _non_empty_string(_required(step, "as", index), f"pipeline.steps[{index}].as")
    expr = _non_empty_string(_required(step, "expr", index), f"pipeline.steps[{index}].expr")
    validate_expression(expr, index, "expr")
    return {"op": "map", "as": as_field, "expr": expr}


def normalize_rename(step, index):
    """Normalize rename step."""
    has_from_to = "from" in step and "to" in step
    has_mapping = "mapping" in step
    if not (has_from_to or has_mapping):
        error("SCHEMA_VALIDATION_FAILED", "rename step requires either ('from' and 'to') or 'mapping'", f"pipeline.steps[{index}]")
    if has_from_to and has_mapping:
        error("SCHEMA_VALIDATION_FAILED", "rename step cannot have both ('from' and 'to') and 'mapping'", f"pipeline.steps[{index}]")

    if has_from_to:
        mapping = {_non_empty_string(step["from"], f"pipeline.steps[{index}].from"): _non_empty_string(step["to"], f"pipeline.steps[{index}].to")}
    else:
        mapping_obj = _required(step, "mapping", index)
        if not isinstance(mapping_obj, dict):
            error("SCHEMA_VALIDATION_FAILED", "'mapping' must be an object", f"pipeline.steps[{index}].mapping")
        mapping = {}
        for key, value in mapping_obj.items():
            if not isinstance(key, str):
                error("SCHEMA_VALIDATION_FAILED", "mapping keys must be strings", f"pipeline.steps[{index}].mapping")
            if not isinstance(value, str):
                error("SCHEMA_VALIDATION_FAILED", "mapping values must be strings", f"pipeline.steps[{index}].mapping[{json.dumps(key)}]")
            mapping[key] = value
    return {"op": "rename", "mapping": mapping}


def normalize_limit(step, index):
    """Normalize limit step."""
    n = _required(step, "n", index)
    if not isinstance(n, int):
        error("SCHEMA_VALIDATION_FAILED", "'n' must be an integer", f"pipeline.steps[{index}].n")
    if n < 0:
        error("SCHEMA_VALIDATION_FAILED", "'n' must be >= 0", f"pipeline.steps[{index}].n")
    return {"op": "limit", "n": n}


def normalize_branch(step, index):
    """Normalize branch step."""
    branches = _required(step, "branches", index)
    if not isinstance(branches, list):
        error("SCHEMA_VALIDATION_FAILED", "'branches' must be an array", f"pipeline.steps[{index}].branches")
    if len(branches) == 0:
        error("SCHEMA_VALIDATION_FAILED", "'branches' must not be empty", f"pipeline.steps[{index}].branches")

    normalized_branches = []
    otherwise_seen = False

    for i, branch in enumerate(branches):
        if not isinstance(branch, dict):
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] must be an object", f"pipeline.steps[{index}].branches[{i}]")

        when = branch.get("when")
        if when is None:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}] is missing required 'when' field", f"pipeline.steps[{index}].branches[{i}].when")

        if isinstance(when, str):
            when_stripped = when.strip()
            if when_stripped.lower() == "otherwise":
                if otherwise_seen:
                    error("MALFORMED_STEP", "only one 'otherwise' branch allowed", f"pipeline.steps[{index}].branches")
                if i < len(branches) - 1:
                    error("MALFORMED_STEP", "'otherwise' branch must be last", f"pipeline.steps[{index}].branches")
                otherwise_seen = True
                when = "otherwise"
            else:
                validate_expression(when_stripped, index, f"branches[{i}].when")
                when = when_stripped
        else:
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].when must be a string", f"pipeline.steps[{index}].branches[{i}].when")

        branch_steps = branch.get("steps", [])
        if not isinstance(branch_steps, list):
            error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].steps must be an array", f"pipeline.steps[{index}].branches[{i}].steps")

        normalized_branch = {"when": when, "steps": branch_steps}
        if "id" in branch:
            id_val = branch["id"]
            if not isinstance(id_val, str):
                error("SCHEMA_VALIDATION_FAILED", f"branch[{i}].id must be a string", f"pipeline.steps[{index}].branches[{i}].id")
            normalized_branch["id"] = id_val.strip()

        normalized_branches.append(normalized_branch)

    merge = step.get("merge", {})
    if not isinstance(merge, dict):
        error("SCHEMA_VALIDATION_FAILED", "'merge' must be an object", f"pipeline.steps[{index}].merge")

    strategy = merge.get("strategy", "concat")
    if not isinstance(strategy, str):
        error("SCHEMA_VALIDATION_FAILED", "'merge.strategy' must be a string", f"pipeline.steps[{index}].merge.strategy")

    strategy_stripped = strategy.strip().lower()
    if strategy_stripped != "concat":
        error("SCHEMA_VALIDATION_FAILED", "'merge.strategy' must be 'concat'", f"pipeline.steps[{index}].merge.strategy")

    return {"op": "branch", "branches": normalized_branches, "merge": {"strategy": strategy_stripped}}


def normalize_call(step, index):
    """Normalize call step."""
    name = step.get("name")
    if name is None:
        error("SCHEMA_VALIDATION_FAILED", "call step is missing required 'name' field", f"pipeline.steps[{index}]")
    validate_definition_name(name, f"pipeline.steps[{index}].name")

    params = step.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        error("SCHEMA_VALIDATION_FAILED", "'params' must be an object", f"pipeline.steps[{index}].params")

    def validate_param_value(val, path):
        if val is None:
            return
        if isinstance(val, (str, int, float, bool)):
            return
        if isinstance(val, list):
            for i, item in enumerate(val):
                validate_param_value(item, f"{path}[{i}]")
            return
        error("SCHEMA_VALIDATION_FAILED", f"param value must be a scalar or array, got {type(val).__name__}", path)

    for key, value in params.items():
        if not isinstance(key, str):
            error("SCHEMA_VALIDATION_FAILED", "params keys must be strings", f"pipeline.steps[{index}].params")
        validate_param_value(value, f"pipeline.steps[{index}].params.{key}")

    return {"op": "call", "name": name, "params": params}


NORMALIZE_DISPATCH = {
    "select": normalize_select,
    "filter": normalize_filter,
    "map": normalize_map,
    "rename": normalize_rename,
    "limit": normalize_limit,
    "branch": normalize_branch,
    "call": normalize_call,
}


def normalize_step(step, index):
    """Normalize a single step."""
    if not isinstance(step, dict):
        error("SCHEMA_VALIDATION_FAILED", "Each step must be an object", f"pipeline.steps[{index}]")
    if "op" not in step:
        error("SCHEMA_VALIDATION_FAILED", "Step is missing required 'op' field", f"pipeline.steps[{index}]")

    op = step["op"]
    if not isinstance(op, str):
        error("SCHEMA_VALIDATION_FAILED", "'op' must be a string", f"pipeline.steps[{index}].op")

    op_normalized = op.strip().lower()
    if not op_normalized:
        error("SCHEMA_VALIDATION_FAILED", "'op' field is empty after trimming", f"pipeline.steps[{index}].op")

    handler = NORMALIZE_DISPATCH.get(op_normalized)
    if handler is None:
        error("UNKNOWN_OP", f"unsupported op '{op_normalized}'", f"pipeline.steps[{index}].op")
    return handler(step, index)


def validate_definition_name(name, path):
    """Validate definition name format."""
    if not isinstance(name, str):
        error("SCHEMA_VALIDATION_FAILED", "definition name must be a string", path)
    if not IDENTIFIER_PATTERN.match(name):
        error("SCHEMA_VALIDATION_FAILED", f"definition name '{name}' must match ^{IDENTIFIER_PATTERN.pattern}$", path)
    if name.lower() == 'params':
        error("SCHEMA_VALIDATION_FAILED", "definition name 'params' is reserved", path)


def _required(step, name, index):
    """Get required field or error."""
    if name not in step:
        error("SCHEMA_VALIDATION_FAILED", f"Step is missing required '{name}' field", f"pipeline.steps[{index}]")
    return step[name]


def _non_empty_string(value, field_path):
    """Validate and return non-empty string."""
    if not isinstance(value, str):
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' must be a string", field_path)
    trimmed = value.strip()
    if not trimmed:
        error("SCHEMA_VALIDATION_FAILED", f"'{field_path.split('.')[-1]}' field is empty after trimming", field_path)
    return trimmed


def parse_input():
    """Parse and validate input JSON."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        error("SCHEMA_VALIDATION_FAILED", f"Invalid JSON: {e}", "root")

    library = data.get("library", {})
    compose = data.get("compose", [])
    dataset = data.get("dataset", [])

    if not isinstance(library, dict):
        error("SCHEMA_VALIDATION_FAILED", "library must be an object", "library")
    if not isinstance(compose, list):
        error("SCHEMA_VALIDATION_FAILED", "compose must be an array", "compose")
    if not isinstance(dataset, list):
        error("SCHEMA_VALIDATION_FAILED", "dataset must be an array", "dataset")

    normalized_library = {}
    for ns, ns_data in library.items():
        if not isinstance(ns_data, dict):
            error("SCHEMA_VALIDATION_FAILED", f"namespace '{ns}' must be an object", f"library.{ns}")
        defs = ns_data.get("defs", {})
        if not isinstance(defs, dict):
            error("SCHEMA_VALIDATION_FAILED", f"namespace '{ns}' defs must be an object", f"library.{ns}.defs")
        normalized_library[ns] = normalize_defs(defs)

    return normalized_library, compose, dataset
