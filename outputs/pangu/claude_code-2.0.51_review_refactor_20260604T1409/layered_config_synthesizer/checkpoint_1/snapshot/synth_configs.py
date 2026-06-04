#!/usr/bin/env python3
"""Synth Configs: Turn layered YAML/JSON inputs into a canonical experiment spec."""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml


def parse_cli_args(argv: list[str]) -> dict[str, str]:
    """Parse CLI args manually to support arbitrary flag order and duplicate handling."""
    result: dict[str, str] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--"):
            flag = arg
            # Check if this is a known flag
            if flag not in ("--base", "--overrides", "--flags", "--env", "--out"):
                sys.stderr.write(json.dumps({
                    "error": {"code": "INVALID_INPUT", "message": f"Unknown flag: {flag}"}
                }) + "\n")
                sys.exit(1)
            # Next arg should be the value
            if i + 1 >= len(argv):
                sys.stderr.write(json.dumps({
                    "error": {"code": "INVALID_INPUT", "message": f"Missing value for {flag}"}
                }) + "\n")
                sys.exit(1)
            result[flag] = argv[i + 1]
            i += 2
        else:
            sys.stderr.write(json.dumps({
                "error": {"code": "INVALID_INPUT", "message": f"Unexpected argument: {arg}"}
            }) + "\n")
            sys.exit(1)

    # Validate all required flags are present
    required = ["--base", "--overrides", "--flags", "--env", "--out"]
    for req in required:
        if req not in result:
            sys.stderr.write(json.dumps({
                "error": {"code": "INVALID_INPUT", "message": f"Missing required flag: {req}"}
            }) + "\n")
            sys.exit(1)

    return result


def load_yaml_file(path: str) -> Optional[Any]:
    """Load a YAML file. Returns None for empty files."""
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_file():
        return None

    with open(path_obj, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        if not content:
            return None
        try:
            return yaml.safe_load(content)
        except yaml.YAMLError as e:
            sys.stderr.write(json.dumps({
                "error": {"code": "PARSE_ERROR", "message": f"Failed to parse YAML file {path}: {e}"}
            }) + "\n")
            sys.exit(1)


def load_json_file(path: str) -> Optional[Any]:
    """Load a JSON file. Returns None for empty/missing files."""
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_file():
        return None

    with open(path_obj, 'r', encoding='utf-8') as f:
        content = f.read().strip()
        if not content:
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            sys.stderr.write(json.dumps({
                "error": {"code": "PARSE_ERROR", "message": f"Failed to parse JSON file {path}: {e}"}
            }) + "\n")
            sys.exit(1)


def load_env_file(path: str) -> None:
    """Load and parse env.list file. For Part 1, we just validate it exists."""
    path_obj = Path(path)
    if not path_obj.exists() or not path_obj.is_file():
        return
    # Parse but ignore values for Part 1
    with open(path_obj, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and "=" in line:
                pass  # Ignore values for Part 1


def deep_merge(base: Any, override: Any, path: str = "", warnings: list[str] = None,
               base_name: str = "base.yaml", override_name: str = "override") -> Any:
    """
    Deep merge override into base.
    Records type conflicts as warnings.
    """
    if warnings is None:
        warnings = []

    # If base is None or not a dict, just return override
    if not isinstance(base, dict) or override is None:
        # If types differ and both are not None, record warning
        if base is not None and override is not None and type(base) != type(override):
            warnings.append(f"CONFLICT {path} {override_name} replaced {base_name}")
        return override

    # If override is not a dict, it replaces base entirely
    if not isinstance(override, dict):
        warnings.append(f"CONFLICT {path} {override_name} replaced {base_name}")
        return override

    # Both are dicts - deep merge
    result = base.copy()
    for key, value in override.items():
        new_path = f"{path}/{key}" if path else f"/{key}"
        if key in result:
            result[key] = deep_merge(result[key], value, new_path, warnings, base_name, override_name)
        else:
            result[key] = value

    return result


def inject_defaults(merged: dict[str, Any], defaults: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """
    Inject missing top-level sections from defaults.yaml.
    Only fills gaps, does not override existing keys.
    """
    for key, value in defaults.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(value, dict) and isinstance(merged[key], dict):
            # Recursively merge nested dicts from defaults
            merged[key] = deep_merge(value, merged[key], f"/{key}", warnings, "defaults.yaml", "higher layer")
    return merged


def validate_spec(spec: dict[str, Any], warnings: list[str]) -> list[str]:
    """Validate the merged spec. Returns list of error messages."""
    errors = []

    # Check spec is an object
    if not isinstance(spec, dict):
        errors.append("INVALID_STRUCTURE: Final merged document must be an object")
        return errors

    # experiment_name: non-empty string with visible characters
    if "experiment_name" not in spec:
        errors.append("experiment_name is required")
    else:
        exp_name = spec["experiment_name"]
        if not isinstance(exp_name, str):
            errors.append("experiment_name must be a string")
        elif not exp_name.strip():
            errors.append("experiment_name must contain visible characters")

    # training: must exist
    if "training" not in spec:
        errors.append("training is required")
    elif not isinstance(spec["training"], dict):
        errors.append("training must be an object")

    # resources: must exist and have accelerators or profile
    if "resources" not in spec:
        errors.append("resources is required")
    elif not isinstance(spec["resources"], dict):
        errors.append("resources must be an object")
    else:
        resources = spec["resources"]
        if "accelerators" not in resources and "profile" not in resources:
            errors.append("resources must include either accelerators or profile")

        # resources.world_size: must be positive integer if present
        if "world_size" in resources:
            ws = resources["world_size"]
            if not isinstance(ws, int) or ws <= 0:
                errors.append("resources.world_size must be a positive integer")

        # resources.accelerators: if present, must be array of objects with type and count
        if "accelerators" in resources:  # Note: typo in spec (accelerators vs accelerators)
            acc = resources["accelerators"]
            if not isinstance(acc, list):
                errors.append("resources.accelerators must be an array")
            else:
                for i, a in enumerate(acc):
                    if not isinstance(a, dict):
                        errors.append(f"resources.accelerators[{i}] must be an object")
                    else:
                        if "type" not in a or not isinstance(a["type"], str):
                            errors.append(f"resources.accelerators[{i}].type must be a string")
                        if "count" not in a or not isinstance(a["count"], int) or a["count"] <= 0:
                            errors.append(f"resources.accelerators[{i}].count must be a positive integer")

    return errors


def canonicalize(obj: Any) -> Any:
    """
    Canonicalize object: sort dict keys lexicographically at every level.
    """
    if isinstance(obj, dict):
        return {k: canonicalize(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [canonicalize(item) for item in obj]
    else:
        return obj


def get_json_pointer(path: str) -> str:
    """Convert internal path format to JSON pointer format."""
    # path format: "/key1/key2"
    return path


def main():
    # Parse CLI args
    args = parse_cli_args(sys.argv[1:])

    base_path = args["--base"]
    overrides_dir = args["--overrides"]
    flags_path = args["--flags"]
    env_path = args["--env"]
    out_dir = args["--out"]

    warnings: list[str] = []

    # Validate and load base.yaml (REQUIRED)
    if not Path(base_path).exists():
        sys.stderr.write(json.dumps({
            "error": {"code": "MISSING_FILE", "message": f"Base file not found: {base_path}"}
        }) + "\n")
        sys.exit(1)
    base = load_yaml_file(base_path)
    if base is None:
        base = {}

    # Validate and load overrides/defaults.yaml (REQUIRED)
    defaults_path = Path(overrides_dir) / "defaults.yaml"
    if not defaults_path.exists():
        sys.stderr.write(json.dumps({
            "error": {"code": "MISSING_FILE", "message": f"Defaults file not found: {defaults_path}"}
        }) + "\n")
        sys.exit(1)
    defaults = load_yaml_file(str(defaults_path))
    if defaults is None:
        defaults = {}

    # Load overrides/run.yaml (OPTIONAL)
    run_path = Path(overrides_dir) / "run.yaml"
    run = load_yaml_file(str(run_path)) if run_path.exists() else None

    # Load flags.json (OPTIONAL)
    flags = load_json_file(flags_path)
    if flags is None:
        flags = {}

    # Load env.list (parse but ignore values for Part 1)
    load_env_file(env_path)

    # Merge layers in order: base -> defaults -> run -> flags
    # Start with base
    merged = base

    # Merge defaults
    if isinstance(defaults, dict) and isinstance(merged, dict):
        merged = deep_merge(merged, defaults, "", warnings, "base.yaml", "defaults.yaml")
    elif defaults is not None:
        merged = defaults

    # Merge run.yaml if present
    if run is not None:
        if isinstance(run, dict) and isinstance(merged, dict):
            merged = deep_merge(merged, run, "", warnings, "defaults.yaml", "run.yaml")
        else:
            merged = run

    # Merge flags.json
    if isinstance(flags, dict) and isinstance(merged, dict):
        merged = deep_merge(merged, flags, "", warnings, "run.yaml", "flags.json")
    elif flags is not None:
        merged = flags

    # Inject defaults (fill gaps, not override)
    if isinstance(merged, dict) and isinstance(defaults, dict):
        merged = inject_defaults(merged, defaults, warnings)

    # Validate
    if not isinstance(merged, dict):
        sys.stderr.write(json.dumps({
            "error": {"code": "INVALID_STRUCTURE", "message": "Final merged document is not an object"}
        }) + "\n")
        sys.exit(1)

    validation_errors = validate_spec(merged, warnings)
    if validation_errors:
        sys.stderr.write(json.dumps({
            "error": {"code": "MISSING_KEY", "message": validation_errors[0]}
        }) + "\n")
        sys.exit(1)

    # Canonicalize output
    canonicalized = canonicalize(merged)

    # Write final_spec.json
    out_path = Path(out_dir)
    spec_file = out_path / "final_spec.json"
    with open(spec_file, 'w', encoding='utf-8') as f:
        json.dump(canonicalized, f, separators=(',', ':'))
        f.write('\n')

    # Write warnings.txt
    warnings_file = out_path / "warnings.txt"
    with open(warnings_file, 'w', encoding='utf-8') as f:
        for warning in warnings:
            f.write(warning + '\n')
        f.write('\n')

    sys.exit(0)


if __name__ == "__main__":
    main()
