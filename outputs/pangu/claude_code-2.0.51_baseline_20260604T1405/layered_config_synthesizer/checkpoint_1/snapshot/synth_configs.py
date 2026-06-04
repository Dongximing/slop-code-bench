#!/usr/bin/env python3
"""
synth_configs - Layered YAML/JSON config merger for experiment specifications.

Turns layered YAML/JSON inputs into a canonical experiment spec with validation.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


# Error codes
ERROR_CODES = {
    "MISSING_FILE": "MISSING_FILE",
    "PARSE_ERROR": "PARSE_ERROR",
    "INVALID_STRUCTURE": "INVALID_STRUCTURE",
    "MISSING_KEY": "MISSING_KEY",
    "INVALID_VALUE": "INVALID_VALUE",
    "INVALID_INPUT": "INVALID_INPUT",
}

# Source file mapping for warnings
SOURCE_NAMES = {
    "base": "base.yaml",
    "defaults": "defaults.yaml",
    "run": "run.yaml",
    "flags": "flags.json",
}


class MergeError(Exception):
    """Exception raised for merge-related errors."""
    pass


def error_exit(code: str, message: str) -> None:
    """Write error to stderr as JSON and exit with code 1."""
    error_obj = {"error": {"code": code, "message": message}}
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


def parse_args(argv):
    """Parse CLI args allowing any order but rejecting unknown flags."""
    # We'll manually parse to allow flexible order and catch unknown flags
    valid_flags = {"--base", "--overrides", "--flags", "--env", "--out"}
    args = {
        "base": None,
        "overrides": None,
        "flags": None,
        "env": None,
        "out": None,
    }
    encountered = set()

    i = 0
    while i < len(argv):
        flag = argv[i]
        if flag not in valid_flags:
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Unknown flag: {flag}"
            )
        if flag in encountered:
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Duplicate flag: {flag}"
            )
        i += 1
        if i >= len(argv):
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Missing value for flag: {flag}"
            )
        args[flag[2:]] = argv[i]  # strip -- prefix
        encountered.add(flag)
        i += 1

    # Validate all required flags are present
    for key in args:
        if args[key] is None:
            error_exit(
                ERROR_CODES["INVALID_INPUT"],
                f"Missing required flag: --{key}"
            )

    return args


def load_yaml(path: str, source_name: str) -> Any:
    """Load a YAML file, returning None for empty files."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        if source_name == "defaults.yaml" or source_name == "base.yaml":
            error_exit(ERROR_CODES["MISSING_FILE"], f"Required file missing: {path}")
        else:
            return None

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = yaml.safe_load(content)
        return data
    except yaml.YAMLError as e:
        error_exit(ERROR_CODES["PARSE_ERROR"], f"YAML parse error in {path}: {e}")


def load_json(path: str) -> Any:
    """Load a JSON file, returning None if missing or empty."""
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        data = json.loads(content)
        return data
    except json.JSONDecodeError as e:
        error_exit(ERROR_CODES["PARSE_ERROR"], f"JSON parse error in {path}: {e}")


def load_env(path: str) -> None:
    """Load env.list file (values ignored for Part 1)."""
    path = Path(path)
    if path.exists() and path.is_file():
        # For Part 1, we just accept and parse (values ignored)
        try:
            content = path.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if line and "=" in line:
                    # Basic parsing - key=value
                    key, _ = line.split("=", 1)
                    # We don't need to store these for Part 1
                    pass
        except Exception:
            pass  # Silently ignore env file issues for Part 1


def deep_merge(base: Any, override: Any, path: str = "", warnings: list = None) -> Any:
    """
    Deep merge override into base.
    Records type conflicts as warnings.

    Rules:
    - Objects merge recursively
    - Arrays replace wholesale (no merge)
    - Scalars are replaced
    - Type mismatches generate warnings
    """
    if warnings is None:
        warnings = []

    # If base is None or not present, return override
    if base is None:
        return override

    # If override is None, return base
    if override is None:
        return base

    # Type mismatch: different types
    if type(base) != type(override):
        # Record warning
        if warnings is not None:
            warnings.append(path)
        # Override wins
        return override

    # Both are dicts
    if isinstance(base, dict) and isinstance(override, dict):
        result = base.copy()
        for key, value in override.items():
            new_path = f"{path}/{key}" if path else f"/{key}"
            if key in result:
                result[key] = deep_merge(
                    result[key], value, new_path, warnings
                )
            else:
                result[key] = value
        return result

    # Both are lists - array replaces wholesale
    if isinstance(base, list) and isinstance(override, list):
        return override

    # Both are scalars of same type - override wins (same type, no warning)
    return override


def get_source_name(source_key: str) -> str:
    """Get the source name for a given source key."""
    return SOURCE_NAMES.get(source_key, source_key)


def detect_type_conflicts(base: dict, merged: dict, source_names: dict) -> list:
    """
    Detect and report type conflicts where a higher precedence value
    overwrote a different type.

    Returns list of (json_pointer, loser_source, winner_source) tuples.
    """
    conflicts = []

    def _compare(orig, new, path="", orig_source="base", new_source="override"):
        if type(orig) != type(new):
            # Type changed - this is a conflict to report
            conflicts.append((path, orig_source, new_source))
            return

        if isinstance(orig, dict) and isinstance(new, dict):
            all_keys = set(orig.keys()) | set(new.keys())
            for key in all_keys:
                new_path = f"{path}/{key}" if path else f"/{key}"
                if key in orig and key in new:
                    _compare(
                        orig[key], new[key], new_path,
                        orig_source, new_source
                    )
                elif key in new:
                    # New key - not a conflict
                    pass
                # key only in orig - removed, not a type conflict

        # For lists and scalars of same type, no conflict

    _compare(base, merged)
    return conflicts


def inject_defaults(merged: dict, defaults_data: dict, warnings: list) -> dict:
    """
    Inject missing keys from defaults.yaml without overriding existing keys.
    """
    if not isinstance(merged, dict) or not isinstance(defaults_data, dict):
        return merged

    result = merged.copy()

    for key, default_value in defaults_data.items():
        if key not in result:
            result[key] = default_value
        elif isinstance(default_value, dict) and isinstance(result[key], dict):
            # Recursively merge nested objects
            result[key] = inject_defaults(result[key], default_value, warnings)

    return result


def canonicalize(obj: Any) -> Any:
    """
    Convert object to canonical form with sorted keys.
    """
    if isinstance(obj, dict):
        return {k: canonicalize(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        return [canonicalize(item) for item in obj]
    else:
        return obj


def validate_spec(data: dict) -> list:
    """
    Validate the merged specification.
    Returns list of error messages, or empty list if valid.
    """
    errors = []

    # Check top-level is object
    if not isinstance(data, dict):
        errors.append("INVALID_STRUCTURE: Merged document must be an object")
        return errors

    # Check experiment_name
    if "experiment_name" not in data:
        errors.append("experiment_name is required")
    elif not isinstance(data["experiment_name"], str) or not data["experiment_name"].strip():
        errors.append("experiment_name must be a non-empty string")

    # Check training exists
    if "training" not in data:
        errors.append("training is required")
    elif not isinstance(data["training"], dict):
        errors.append("training must be an object")

    # Check resources exists and has required structure
    if "resources" not in data:
        errors.append("resources is required")
    elif not isinstance(data["resources"], dict):
        errors.append("resources must be an object")
    else:
        resources = data["resources"]
        # Must have either accelerators or profile
        has_accelerators = "accelerators" in resources and isinstance(resources["accelerators"], list)
        has_profile = "profile" in resources and isinstance(resources["profile"], str)

        if not has_accelerators and not has_profile:
            errors.append("resources must include either accelerators (array) or profile (string)")

        # Validate world_size if present
        if "world_size" in resources:
            ws = resources["world_size"]
            if not isinstance(ws, int) or ws <= 0:
                errors.append("resources.world_size must be a positive integer")

        # Validate accelerators if present
        if "accelerators" in resources and resources["accelerators"] is not None:
            accels = resources["accelerators"]
            if not isinstance(accels, list):
                errors.append("resources.accelerators must be an array")
            else:
                for i, acc in enumerate(accels):
                    if not isinstance(acc, dict):
                        errors.append(f"resources.accelerators[{i}] must be an object")
                    else:
                        if "type" not in acc or not isinstance(acc["type"], str):
                            errors.append(f"resources.accelerators[{i}].type must be a string")
                        if "count" not in acc or not isinstance(acc["count"], int) or acc["count"] <= 0:
                            errors.append(f"resources.accelerators[{i}].count must be a positive integer")

    return errors


def write_outputs(out_dir: str, final_spec: dict, warnings: list) -> None:
    """Write final_spec.json and warnings.txt to output directory."""
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Write final_spec.json with canonicalized keys
    canonical_spec = canonicalize(final_spec)
    spec_json = json.dumps(canonical_spec, separators=(",", ":")) + "\n"
    (out_path / "final_spec.json").write_text(spec_json, encoding="utf-8")

    # Write warnings.txt
    warning_lines = []
    for warning in warnings:
        # warning is a tuple: (path, loser_source, winner_source)
        # Format: CONFLICT <json-pointer> <loser_source> -> <winner_source>
        json_ptr = warning[0] if warning[0] != "" else "/"
        loser = get_source_name(warning[1]) if len(warning) > 1 else "unknown"
        winner = get_source_name(warning[2]) if len(warning) > 2 else "unknown"
        warning_lines.append(f"CONFLICT {json_ptr} {loser} -> {winner}")

    warnings_content = "\n".join(warning_lines)
    if warnings_content:
        warnings_content += "\n"
    else:
        warnings_content = "\n"  # Empty file with trailing newline

    (out_path / "warnings.txt").write_text(warnings_content, encoding="utf-8")


def main() -> None:
    """Main entry point."""
    args = parse_args(sys.argv[1:])

    # Load env file (values ignored for Part 1)
    load_env(args["env"])

    # Load base.yaml (required)
    base_data = load_yaml(args["base"], "base.yaml")
    if base_data is None:
        error_exit(ERROR_CODES["MISSING_FILE"], f"Required file missing: {args['base']}")

    # Load overrides directory
    overrides_dir = Path(args["overrides"])
    if not overrides_dir.exists() or not overrides_dir.is_dir():
        error_exit(ERROR_CODES["MISSING_FILE"], f"Overrides directory missing: {args['overrides']}")

    defaults_path = overrides_dir / "defaults.yaml"
    if not defaults_path.exists() or not defaults_path.is_file():
        error_exit(ERROR_CODES["MISSING_FILE"], f"Required file missing: {defaults_path}")

    defaults_data = load_yaml(str(defaults_path), "defaults.yaml")

    # Load run.yaml (optional)
    run_data = None
    run_path = overrides_dir / "run.yaml"
    if run_path.exists() and run_path.is_file():
        run_data = load_yaml(str(run_path), "run.yaml")

    # Load flags.json (optional, may be missing or empty)
    flags_data = load_json(args["flags"])

    # Track all type conflicts
    all_warnings = []

    # Perform merge in order: base -> defaults -> run -> flags
    # We'll track type conflicts at each step

    # Track original for conflict detection
    merged = base_data
    previous = base_data
    previous_source = "base"

    # Merge with defaults
    temp_warnings = []
    merged = deep_merge(merged, defaults_data, "", temp_warnings)
    # Add warnings with proper source names
    for wp in temp_warnings:
        all_warnings.append((wp, "base", "defaults"))
    previous = defaults_data
    previous_source = "defaults"

    # Merge with run.yaml if present
    if run_data is not None:
        temp_warnings = []
        merged = deep_merge(merged, run_data, "", temp_warnings)
        for wp in temp_warnings:
            all_warnings.append((wp, "defaults", "run"))
        previous = run_data
        previous_source = "run"

    # Merge with flags.json if present
    if flags_data is not None:
        temp_warnings = []
        merged = deep_merge(merged, flags_data, "", temp_warnings)
        for wp in temp_warnings:
            all_warnings.append((wp, "run", "flags"))

    # Inject defaults for missing keys
    merged = inject_defaults(merged, defaults_data or {}, all_warnings)

    # Validate
    validation_errors = validate_spec(merged)
    if validation_errors:
        error_exit(
            ERROR_CODES["MISSING_KEY"] if "required" in " ".join(validation_errors).lower()
            else ERROR_CODES["INVALID_VALUE"],
            "; ".join(validation_errors)
        )

    # Write outputs
    write_outputs(args["out"], merged, all_warnings)

    # Success - exit 0
    sys.exit(0)


if __name__ == "__main__":
    main()
