#!/usr/bin/env python3

import argparse
import json
import re
import sys
from collections import OrderedDict
from pathlib import Path

import yaml

ERROR_MISSING_FILE = "MISSING_FILE"
ERROR_PARSE = "PARSE_ERROR"
ERROR_INVALID_STRUCTURE = "INVALID_STRUCTURE"
ERROR_MISSING_KEY = "MISSING_KEY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_INPUT = "INVALID_INPUT"
ERROR_UNKNOWN_FRAGMENT = "UNKNOWN_FRAGMENT"
ERROR_FRAGMENT_CYCLE = "FRAGMENT_CYCLE"
ERROR_INVALID_TEMPLATE = "INVALID_TEMPLATE"
ERROR_UNKNOWN_ENV_VAR = "UNKNOWN_ENV_VAR"

LAYER_BASE = "base.yaml"
LAYER_DEFAULTS = "defaults.yaml"
LAYER_RUN = "run.yaml"
LAYER_FLAGS = "flags.json"


def error_exit(code: str, message: str):
    print(json.dumps({"error": {"code": code, "message": message}}), file=sys.stderr)
    sys.exit(1)


def parse_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


def parse_yaml_file(path: Path) -> tuple:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return None, None, None
        data = yaml.load(content, yaml.SafeLoader)
        return data, None, None
    except yaml.YAMLError as e:
        return None, ERROR_PARSE, f"Failed to parse {path}: {e}"
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read {path}: {e}"


def load_fragments(fragments_dir: Path) -> tuple:
    fragments = {}
    if not fragments_dir.exists():
        return fragments, None, None
    for frag_path in fragments_dir.glob("*.yaml"):
        if frag_path.name == "meta.yaml":
            continue
        frag_name = frag_path.stem
        if not re.match(r'^[a-z0-9_]+$', frag_name):
            return None, ERROR_INVALID_TEMPLATE, f"Invalid fragment name: {frag_name}"
        data, err_code, err_msg = parse_yaml_file(frag_path)
        if err_code:
            return None, err_code, err_msg
        fragments[frag_name] = data if data is not None else {}
    return fragments, None, None


def load_fragment_meta(fragments_dir: Path) -> dict:
    meta_path = fragments_dir / "meta.yaml"
    if not meta_path.exists():
        return {}
    data, err_code, err_msg = parse_yaml_file(meta_path)
    if err_code:
        error_exit(err_code, err_msg)
    return data if data is not None else {}


# Fragment placeholder pattern
FRAGMENT_PATTERN = re.compile(r'^\{\{fragment:([a-z0-9_]+)\}\}$')


def is_fragment_placeholder(value) -> bool:
    """Check if a value is a fragment placeholder."""
    if not isinstance(value, str):
        return False
    return bool(FRAGMENT_PATTERN.match(value))


def extract_fragment_name(value: str) -> str:
    """Extract fragment name from placeholder. Assumes is_fragment_placeholder(value) is True."""
    # Since is_fragment_placeholder was called first, regex must match
    return FRAGMENT_PATTERN.match(value).group(1)


def find_fragment_placeholders(obj, path: str = "") -> list:
    """Recursively find all fragment placeholder paths in an object.
    Returns list of (json_pointer, value, path_for_merge).
    """
    placeholders = []
    if isinstance(obj, dict):
        for key, val in obj.items():
            new_path = f"{path}/{key}" if path else f"/{key}"
            if is_fragment_placeholder(val):
                placeholders.append((new_path, val, path))
            elif isinstance(val, (dict, list)):
                placeholders.extend(find_fragment_placeholders(val, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            new_path = f"{path}/{i}" if path else f"/{i}"
            if is_fragment_placeholder(item):
                placeholders.append((new_path, item, path))
            elif isinstance(item, (dict, list)):
                placeholders.extend(find_fragment_placeholders(item, new_path))
    return placeholders


def resolve_fragments(doc: dict, fragments: dict, warnings: list, meta: dict,
                      resolution_stack: list = None) -> tuple:
    """
    Recursively resolve {{fragment:name}} placeholders in the document.
    Returns (resolved_doc, err_code, err_msg).
    Handles nested fragment references and cycle detection.
    """
    if resolution_stack is None:
        resolution_stack = []

    deprecated_warned = set()

    def _resolve(val) -> tuple:
        if not isinstance(val, str) or not is_fragment_placeholder(val):
            if isinstance(val, dict):
                result = {}
                for k, v in val.items():
                    resolved, err_code, err_msg = _resolve(v)
                    if err_code:
                        return None, err_code, err_msg
                    result[k] = resolved
                return result, None, None

            if isinstance(val, list):
                result = []
                for item in val:
                    resolved, err_code, err_msg = _resolve(item)
                    if err_code:
                        return None, err_code, err_msg
                    result.append(resolved)
                return result, None, None
            return val, None, None

        # Handle fragment placeholders
        frag_name = extract_fragment_name(val)

        if frag_name in resolution_stack:
            cycle = " -> ".join(resolution_stack[resolution_stack.index(frag_name):] + [frag_name])
            return None, ERROR_FRAGMENT_CYCLE, f"Fragment cycle detected: {cycle}"

        if frag_name not in fragments:
            return None, ERROR_UNKNOWN_FRAGMENT, f"Fragment '{frag_name}' not found"

        frag_content = fragments[frag_name]

        if frag_name in meta and "deprecated_reason" in meta[frag_name]:
            if frag_name not in deprecated_warned:
                warnings.append(f"FRAGMENT_DEPRECATED {frag_name} {meta[frag_name]['deprecated_reason']}")
                deprecated_warned.add(frag_name)

        if frag_content is None:
            frag_content = {}

        if isinstance(frag_content, dict) and "_meta" in frag_content:
            frag_content = {k: v for k, v in frag_content.items() if k != "_meta"}

        resolved, err_code, err_msg = _resolve(frag_content)
        if err_code:
            return None, err_code, err_msg
        return resolved, None, None

    resolved, err_code, err_msg = _resolve(doc)
    return resolved, err_code, err_msg


def parse_json_file(path: Path) -> tuple:
    """Parse a JSON file, returning (data, error_code, error_message)."""
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None, None, None  # missing or empty is treated as absent
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, ERROR_PARSE, f"{path} must contain a JSON object, got {type(data).__name__}"
        return data, None, None
    except json.JSONDecodeError as e:
        return None, ERROR_PARSE, f"Failed to parse {path}: {e}"
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read {path}: {e}"


# Environment variable pattern: ${VAR_NAME}
ENV_PATTERN = re.compile(r'\$\{([a-zA-Z_][a-zA-Z0-9_]*)\}')


def interpolate_env_vars(obj: dict, env: dict, path: str = "") -> tuple:
    if isinstance(obj, dict):
        result = {}
        for key, val in obj.items():
            new_path = f"{path}/{key}" if path else f"/{key}"
            resolved, err_code, err_msg = interpolate_env_vars(val, env, new_path)
            if err_code:
                return None, err_code, err_msg
            result[key] = resolved
        return result, None, None
    if isinstance(obj, list):
        result = []
        for i, item in enumerate(obj):
            new_path = f"{path}/{i}" if path else f"/{i}"
            resolved, err_code, err_msg = interpolate_env_vars(item, env, new_path)
            if err_code:
                return None, err_code, err_msg
            result.append(resolved)
        return result, None, None
    if isinstance(obj, str):
        matches = list(ENV_PATTERN.finditer(obj))
        if not matches:
            return obj, None, None
        result = obj
        for match in matches:
            var_name = match.group(1)
            if var_name not in env:
                return None, ERROR_UNKNOWN_ENV_VAR, f"{var_name} is not defined"
            result = result[:match.start()] + env[var_name] + result[match.end():]
        return result, None, None
    return obj, None, None


def _type_name(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


def _merge_layers_with_sources(layers: list, warnings: list, path: str = "") -> tuple:
    """Deep merge layers tracking source of each value."""
    result = {}
    sources = {}
    for layer_name, layer_dict in layers:
        if not layer_dict:
            continue
        for key, val in layer_dict.items():
            current_path = f"{path}/{key}" if path else f"/{key}"
            if key not in result:
                result[key] = val
                sources[key] = layer_name
                continue

            base = result[key]
            if _type_name(base) != _type_name(val):
                warnings.append(f"CONFLICT {current_path} {sources[key]} -> {layer_name}")

            if isinstance(base, dict) and isinstance(val, dict):
                remaining = [(lname, ldict.get(key, {})) for lname, ldict in layers]
                result[key], _ = _merge_layers_with_sources(remaining, warnings, current_path)
            else:
                result[key] = val
            sources[key] = layer_name
    return result, sources


def merge_layers(layers: list, warnings: list, path: str = "") -> dict:
    """Deep merge layers. Layers is [(name, dict), ...] lowest to highest precedence."""
    result, _ = _merge_layers_with_sources(layers, warnings, path)
    return result


def validate_document(doc: dict) -> tuple:
    if not isinstance(doc, dict):
        return False, ERROR_INVALID_STRUCTURE, "Merged document is not an object"

    for key, typ, msg in [
        ("experiment_name", str, "experiment_name must be a non-empty string with visible characters"),
        ("training", dict, "training must be an object"),
        ("resources", dict, "resources must be an object"),
    ]:
        if key not in doc:
            return False, ERROR_MISSING_KEY, f"{key} is required"
        if not isinstance(doc[key], typ):
            return False, ERROR_INVALID_VALUE, msg

    exp_name = doc["experiment_name"]
    if not exp_name or not exp_name.strip():
        return False, ERROR_MISSING_KEY, "experiment_name must be a non-empty string with visible characters"

    resources = doc["resources"]

    # Check world_size if present
    if "world_size" in resources:
        ws = resources["world_size"]
        if not isinstance(ws, int) or ws <= 0:
            return False, ERROR_INVALID_VALUE, "resources.world_size must be a positive integer"

    # Check accelerators if present
    if "accelerators" in resources:
        acc = resources["accelerators"]
        if not isinstance(acc, list):
            return False, ERROR_INVALID_VALUE, "resources.accelerators must be an array"
        for i, item in enumerate(acc):
            if not isinstance(item, dict):
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}] must be an object"
            if "type" not in item or not isinstance(item["type"], str):
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}].type must be a string"
            if "count" not in item or not isinstance(item["count"], int) or item["count"] <= 0:
                return False, ERROR_INVALID_VALUE, f"resources.accelerators[{i}].count must be a positive integer"

    # Check that either accelerators or profile exists
    if not ("accelerators" in resources or "profile" in resources):
        return False, ERROR_MISSING_KEY, "resources must include either accelerators or profile"

    return True, None, None


def sort_json(obj):
    if isinstance(obj, dict):
        return {k: sort_json(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [sort_json(item) for item in obj]
    return obj


def write_outputs(doc: dict, warnings: list, out_dir: Path):
    sorted_doc = sort_json(doc)
    spec_path = out_dir / "final_spec.json"
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump(sorted_doc, f, separators=(",", ":"))
        f.write("\n")

    warnings_path = out_dir / "warnings.txt"
    with open(warnings_path, "w", encoding="utf-8") as f:
        for w in warnings:
            f.write(w + "\n")
        f.write("\n")


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="synth_configs",
        description="Merge layered YAML/JSON configs into canonical experiment spec.",
        allow_abbrev=False
    )
    parser.add_argument("--base", required=True, help="Path to base.yaml")
    parser.add_argument("--overrides", required=True, help="Path to overrides directory")
    parser.add_argument("--fragments", required=True, help="Path to fragments directory")
    parser.add_argument("--flags", required=True, help="Path to flags.json")
    parser.add_argument("--env", required=True, help="Path to env.list")
    parser.add_argument("--out", required=True, help="Path to output directory")

    try:
        return parser.parse_args(argv)
    except SystemExit:
        error_exit(ERROR_INVALID_INPUT, "Invalid CLI invocation")


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    args = parse_args(argv)

    base_path = Path(args.base)
    overrides_dir = Path(args.overrides)
    fragments_dir = Path(args.fragments)
    flags_path = Path(args.flags)
    env_path = Path(args.env)
    out_dir = Path(args.out)

    # Validate output directory exists
    if not out_dir.exists() or not out_dir.is_dir():
        error_exit(ERROR_INVALID_INPUT, f"Output directory does not exist: {out_dir}")

    # 1. Parse env file
    env = parse_env_file(env_path)

    # 2. Load base.yaml (required)
    if not base_path.exists():
        error_exit(ERROR_MISSING_FILE, f"Base file not found: {base_path}")
    base_data, err_code, err_msg = parse_yaml_file(base_path)
    if err_code:
        error_exit(err_code, err_msg)
    if base_data is None:
        base_data = {}
    if not isinstance(base_data, dict):
        error_exit(ERROR_PARSE, f"base.yaml must be a YAML object, got {type(base_data).__name__}")

    # 3. Load overrides/defaults.yaml (required)
    defaults_path = overrides_dir / "defaults.yaml"
    if not defaults_path.exists():
        error_exit(ERROR_MISSING_FILE, f"Defaults file not found: {defaults_path}")
    defaults_data, err_code, err_msg = parse_yaml_file(defaults_path)
    if err_code:
        error_exit(err_code, err_msg)
    if defaults_data is None:
        defaults_data = {}
    if not isinstance(defaults_data, dict):
        error_exit(ERROR_PARSE, f"defaults.yaml must be a YAML object, got {type(defaults_data).__name__}")

    # 4. Load overrides/run.yaml (optional)
    run_path = overrides_dir / "run.yaml"
    run_data = None
    if run_path.exists():
        run_data, err_code, err_msg = parse_yaml_file(run_path)
        if err_code:
            error_exit(err_code, err_msg)
        if not isinstance(run_data, dict):
            error_exit(ERROR_PARSE, f"run.yaml must be a YAML object, got {type(run_data).__name__}")

    # 5. Load flags.json (optional, may be missing or empty)
    flags_data, err_code, err_msg = parse_json_file(flags_path)
    if err_code:
        error_exit(err_code, err_msg)
    if flags_data is None:
        flags_data = {}

    # 6. Load fragments
    fragments, err_code, err_msg = load_fragments(fragments_dir)
    if err_code:
        error_exit(err_code, err_msg)

    # 7. Load fragment metadata (deprecation info)
    fragment_meta = load_fragment_meta(fragments_dir)

    # 8. Merge layers in order: base -> defaults -> run -> flags
    warnings = []

    # Build layered list: lowest to highest precedence
    layers = [(LAYER_BASE, base_data if base_data else {})]
    layers.append((LAYER_DEFAULTS, defaults_data if defaults_data else {}))
    if run_data is not None:
        layers.append((LAYER_RUN, run_data if run_data else {}))
    layers.append((LAYER_FLAGS, flags_data if flags_data else {}))

    # Merge all layers
    merged = merge_layers(layers, warnings)

    # 9. Inject defaults for missing required sections from defaults.yaml
    #    Only fill gaps, don't override existing keys
    for section in REQUIRED_SECTIONS:
        if section not in merged or merged[section] is None:
            if section in defaults_data and defaults_data[section] is not None:
                merged[section] = defaults_data[section]

    # 10. Resolve fragment placeholders
    resolved, err_code, err_msg = resolve_fragments(merged, fragments, warnings, fragment_meta)
    if err_code:
        error_exit(err_code, err_msg)
    merged = resolved

    # 11. Interpolate environment variables
    interpolated, err_code, err_msg = interpolate_env_vars(merged, env)
    if err_code:
        error_exit(err_code, err_msg)
    merged = interpolated

    # 12. Validate (after interpolation)
    is_valid, err_code, err_msg = validate_document(merged)
    if not is_valid:
        error_exit(err_code, err_msg)

    # 13. Write outputs
    write_outputs(merged, warnings, out_dir)

    # Exit 0 on success
    sys.exit(0)


if __name__ == "__main__":
    main()
