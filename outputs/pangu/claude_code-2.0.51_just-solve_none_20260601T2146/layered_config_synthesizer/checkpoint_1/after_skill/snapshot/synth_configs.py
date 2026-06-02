#!/usr/bin/env python3
"""synth_configs - Merge layered YAML/JSON configs into canonical experiment spec."""

import argparse
import json
import sys
from pathlib import Path

import yaml

# Error codes
ERROR_MISSING_FILE = "MISSING_FILE"
ERROR_PARSE = "PARSE_ERROR"
ERROR_INVALID_STRUCTURE = "INVALID_STRUCTURE"
ERROR_MISSING_KEY = "MISSING_KEY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_INPUT = "INVALID_INPUT"

REQUIRED_SECTIONS = ["experiment_name", "training", "resources"]
# Layer names for warnings
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
            return None, None, None  # empty file is null
        data = yaml.safe_load(content)
        return data, None, None
    except yaml.YAMLError as e:
        return None, ERROR_PARSE, f"Failed to parse {path}: {e}"
    except OSError as e:
        return None, ERROR_MISSING_FILE, f"Cannot read {path}: {e}"


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


get_json_type = _type_name  # alias


def _merge_layers_with_sources(layers: list, warnings: list, path: str = "") -> tuple:
    """
    Deep merge layers tracking source of each value.
    Returns (dict, source_map) where source_map maps keys to layer they came from.
    """
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
            base_type = _type_name(base)
            override_type = _type_name(val)
            base_source = sources[key]

            if base_type != override_type:
                warnings.append(f"CONFLICT {current_path} {base_source} -> {layer_name}")
                result[key] = val
                sources[key] = layer_name
            elif base_type == "object" and override_type == "object":
                # Build sub-layers from remaining layers at same depth
                remaining = [(lname, ldict.get(key, {})) for lname, ldict in layers]
                sub_result, sub_sources = _merge_layers_with_sources(remaining, warnings, current_path)
                result[key] = sub_result
                sources[key] = layer_name  # Object keys get the layer of the merge result
            else:
                result[key] = val
                sources[key] = layer_name
    return result, sources


def merge_layers(layers: list, warnings: list, path: str = "") -> dict:
    """Deep merge layers. Layers is [(name, dict), ...] lowest to highest precedence."""
    result, _ = _merge_layers_with_sources(layers, warnings, path)
    return result


def validate_document(doc: dict) -> tuple:
    """
    Validate the merged document.
    Returns (is_valid, error_code, error_message).
    """
    if not isinstance(doc, dict):
        return False, ERROR_INVALID_STRUCTURE, "Merged document is not an object"

    # Check experiment_name
    if "experiment_name" not in doc:
        return False, ERROR_MISSING_KEY, "experiment_name is required"
    exp_name = doc["experiment_name"]
    if not isinstance(exp_name, str) or not exp_name.strip():
        return False, ERROR_MISSING_KEY, "experiment_name must be a non-empty string with visible characters"

    # Check training
    if "training" not in doc:
        return False, ERROR_MISSING_KEY, "training is required"
    if not isinstance(doc["training"], dict):
        return False, ERROR_INVALID_VALUE, "training must be an object"

    # Check resources
    if "resources" not in doc:
        return False, ERROR_MISSING_KEY, "resources is required"
    if not isinstance(doc["resources"], dict):
        return False, ERROR_INVALID_VALUE, "resources must be an object"

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
    has_accelerators = "accelerators" in resources
    has_profile = "profile" in resources
    if not has_accelerators and not has_profile:
        return False, ERROR_MISSING_KEY, "resources must include either accelerators or profile"

    return True, None, None


def sort_json(obj):
    """Recursively sort dictionary keys lexicographically."""
    if isinstance(obj, dict):
        return {k: sort_json(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [sort_json(item) for item in obj]
    else:
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
    flags_path = Path(args.flags)
    env_path = Path(args.env)
    out_dir = Path(args.out)

    # Validate output directory exists
    if not out_dir.exists() or not out_dir.is_dir():
        error_exit(ERROR_INVALID_INPUT, f"Output directory does not exist: {out_dir}")

    # 1. Parse env file (accept but ignore for Part 1)
    parse_env_file(env_path)

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
        if run_data is not None and not isinstance(run_data, dict):
            error_exit(ERROR_PARSE, f"run.yaml must be a YAML object, got {type(run_data).__name__}")

    # 5. Load flags.json (optional, may be missing or empty)
    flags_data, err_code, err_msg = parse_json_file(flags_path)
    if err_code:
        error_exit(err_code, err_msg)
    if flags_data is None:
        flags_data = {}

    # 6. Merge layers in order: base -> defaults -> run -> flags
    warnings = []

    # Build layered list: lowest to highest precedence
    layers = [(LAYER_BASE, base_data if base_data else {})]
    layers.append((LAYER_DEFAULTS, defaults_data if defaults_data else {}))
    if run_data is not None:
        layers.append((LAYER_RUN, run_data if run_data else {}))
    layers.append((LAYER_FLAGS, flags_data if flags_data else {}))

    # Merge all layers
    merged = merge_layers(layers, warnings)

    # 7. Inject defaults for missing required sections from defaults.yaml
    #    Only fill gaps, don't override existing keys
    for section in REQUIRED_SECTIONS:
        if section not in merged or merged[section] is None:
            if section in defaults_data and defaults_data[section] is not None:
                merged[section] = defaults_data[section]

    # 8. Validate
    is_valid, err_code, err_msg = validate_document(merged)
    if not is_valid:
        error_exit(err_code, err_msg)

    # 9. Write outputs
    write_outputs(merged, warnings, out_dir)

    # Exit 0 on success
    sys.exit(0)


if __name__ == "__main__":
    main()
