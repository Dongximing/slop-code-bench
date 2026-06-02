#!/usr/bin/env python3
"""
synth_configs - CLI that turns layered YAML/JSON inputs into a canonical experiment spec.
"""

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(json.dumps({
        "error": {
            "code": "PARSE_ERROR",
            "message": "PyYAML is required. Install with: pip install pyyaml"
        }
    }), file=sys.stderr)
    sys.exit(1)


# Error codes
ERROR_MISSING_FILE = "MISSING_FILE"
ERROR_PARSE = "PARSE_ERROR"
ERROR_INVALID_STRUCTURE = "INVALID_STRUCTURE"
ERROR_MISSING_KEY = "MISSING_KEY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_INPUT = "INVALID_INPUT"

# Source names for warnings
SOURCE_BASE = "base.yaml"
SOURCE_DEFAULTS = "defaults.yaml"
SOURCE_RUN = "run.yaml"
SOURCE_FLAGS = "flags.json"


class SynthConfigsError(Exception):
    """Base exception for synth_configs errors."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def parse_cli_args(args):
    """Parse CLI arguments allowing any order, last occurrence wins."""
    parsed = {
        'base': None,
        'overrides': None,
        'flags': None,
        'env': None,
        'out': None
    }

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == '--base':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --base")
            parsed['base'] = args[i + 1]
            i += 2
        elif arg == '--overrides':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --overrides")
            parsed['overrides'] = args[i + 1]
            i += 2
        elif arg == '--flags':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --flags")
            parsed['flags'] = args[i + 1]
            i += 2
        elif arg == '--env':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --env")
            parsed['env'] = args[i + 1]
            i += 2
        elif arg == '--out':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --out")
            parsed['out'] = args[i + 1]
            i += 2
        else:
            raise SynthConfigsError(ERROR_INVALID_INPUT, f"Unknown argument: {arg}")

    # Validate all required flags are present
    for key in ['base', 'overrides', 'flags', 'env', 'out']:
        if parsed[key] is None:
            raise SynthConfigsError(ERROR_INVALID_INPUT, f"Missing required flag: --{key}")

    return parsed


def read_yaml_file(filepath, source_name):
    """Read and parse a YAML file."""
    path = Path(filepath)

    if not path.exists():
        raise SynthConfigsError(ERROR_MISSING_FILE, f"{source_name} not found: {filepath}")

    if not path.is_file():
        raise SynthConfigsError(ERROR_MISSING_FILE, f"{source_name} is not a file: {filepath}")

    try:
        content = path.read_text(encoding='utf-8')
    except OSError:
        raise SynthConfigsError(ERROR_MISSING_FILE, f"Cannot read {source_name}")

    # Empty document counts as null
    if not content.strip():
        return None

    try:
        # Use yaml.safe_load to parse YAML
        data = yaml.safe_load(content)
        return data
    except yaml.YAMLError:
        raise SynthConfigsError(ERROR_PARSE, f"Failed to parse {source_name}")


def read_json_file(filepath, source_name):
    """Read and parse a JSON file. May be missing or empty."""
    path = Path(filepath)

    if not path.exists():
        return None

    if not path.is_file():
        raise SynthConfigsError(ERROR_MISSING_FILE, f"{source_name} is not a file: {filepath}")

    try:
        content = path.read_text(encoding='utf-8')
    except OSError:
        raise SynthConfigsError(ERROR_MISSING_FILE, f"Cannot read {source_name}")

    # Empty file returns None
    if not content.strip():
        return None

    try:
        data = json.loads(content)
        return data
    except json.JSONDecodeError:
        raise SynthConfigsError(ERROR_PARSE, f"Failed to parse {source_name}")


def read_env_file(filepath):
    """Read and parse an env file (KEY=VALUE per line). For Part 1, parse but ignore values."""
    path = Path(filepath)

    if not path.exists():
        return {}

    if not path.is_file():
        raise SynthConfigsError(ERROR_MISSING_FILE, f"env file is not a file: {filepath}")

    try:
        content = path.read_text(encoding='utf-8')
    except OSError:
        raise SynthConfigsError(ERROR_MISSING_FILE, "Cannot read env file")

    # Parse but ignore values for Part 1
    env_data = {}
    for line in content.splitlines():
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            key = line.split('=', 1)[0].strip()
            if key:
                env_data[key] = None  # Store but ignore value

    return env_data


def deep_merge_dicts(base, override, path="", source_names=None, warnings=None):
    """
    Deep merge two dictionaries. Override takes precedence.
    Track type conflicts in warnings list.

    Args:
        base: Lower precedence dict
        override: Higher precedence dict
        path: Current JSON pointer path (for warnings)
        source_names: Tuple of (base_source_name, override_source_name)
        warnings: List to append warnings to

    Returns:
        Merged dictionary
    """
    if source_names is None:
        source_names = (SOURCE_BASE, SOURCE_FLAGS)

    result = base.copy() if base else {}

    for key, override_value in override.items():
        current_path = f"{path}/{key}" if path else f"/{key}"

        if key in result:
            base_value = result[key]

            # Check for type conflict
            if warnings is not None:
                base_type = type(base_value).__name__
                override_type = type(override_value).__name__

                if base_type != override_type:
                    warnings.append({
                        'path': current_path,
                        'loser': source_names[0],
                        'winner': source_names[1]
                    })

            # Both are dicts - deep merge
            if isinstance(base_value, dict) and isinstance(override_value, dict):
                result[key] = deep_merge_dicts(
                    base_value, override_value,
                    current_path, source_names, warnings
                )
            else:
                # Override completely replaces
                result[key] = override_value
        else:
            result[key] = override_value

    return result


def validate_data(data):
    """Validate the merged data structure. Returns list of errors."""
    errors = []

    # Check root is an object
    if not isinstance(data, dict):
        errors.append((ERROR_INVALID_STRUCTURE,
                      "Merged configuration must be a JSON object"))
        return errors

    # Check experiment_name
    if 'experiment_name' not in data:
        errors.append((ERROR_MISSING_KEY, "experiment_name is required"))
    elif not isinstance(data['experiment_name'], str) or not data['experiment_name'].strip():
        errors.append((ERROR_MISSING_KEY, "experiment_name must be a non-empty string"))

    # Check training object
    if 'training' not in data:
        errors.append((ERROR_MISSING_KEY, "training is required"))
    elif not isinstance(data['training'], dict):
        errors.append((ERROR_INVALID_STRUCTURE, "training must be an object"))

    # Check resources object
    if 'resources' not in data:
        errors.append((ERROR_MISSING_KEY, "resources is required"))
    elif not isinstance(data['resources'], dict):
        errors.append((ERROR_INVALID_STRUCTURE, "resources must be an object"))
    else:
        resources = data['resources']

        # Check world_size if present
        if 'world_size' in resources:
            ws = resources['world_size']
            if not isinstance(ws, int) or ws <= 0:
                errors.append((ERROR_INVALID_VALUE,
                             f"resources.world_size must be a positive integer, got {ws}"))

        # Check for accelerators or profile
        has_accelerators = 'accelerators' in resources
        has_profile = 'profile' in resources

        if not has_accelerators and not has_profile:
            errors.append((ERROR_MISSING_KEY,
                         "resources must include either 'accelerators' or 'profile'"))

        # Validate accelerators if present
        if has_accelerators:
            accels = resources['accelerators']
            if not isinstance(accels, list):
                errors.append((ERROR_INVALID_VALUE,
                             "resources.accelerators must be an array"))
            else:
                for i, accel in enumerate(accels):
                    if not isinstance(accel, dict):
                        errors.append((ERROR_INVALID_VALUE,
                                     f"resources.accelerators[{i}] must be an object"))
                    else:
                        if 'type' not in accel or not isinstance(accel['type'], str):
                            errors.append((ERROR_INVALID_VALUE,
                                         f"resources.accelerators[{i}].type must be a string"))
                        if 'count' not in accel or not isinstance(accel['count'], int) or accel['count'] <= 0:
                            errors.append((ERROR_INVALID_VALUE,
                                         f"resources.accelerators[{i}].count must be a positive integer"))

    return errors


def defaults_injection(final_data, defaults_override):
    """
    Inject missing top-level sections from defaults.yaml.
    Only fill gaps, don't override existing keys.
    """
    if not isinstance(defaults_override, dict):
        return final_data

    for key, default_value in defaults_override.items():
        if key not in final_data:
            # Missing key, add the default
            final_data[key] = deep_copy(default_value)
        elif isinstance(default_value, dict) and isinstance(final_data[key], dict):
            # Recursively merge nested objects
            final_data[key] = deep_merge_objects_gaps(
                final_data[key], default_value, key
            )

    return final_data


def deep_merge_objects_gaps(target, source, path=""):
    """
    Merge source into target, only filling gaps (not overwriting existing).
    Returns a new dict.
    """
    result = target.copy()

    for key, source_value in source.items():
        current_path = f"{path}/{key}" if path else key

        if key not in result:
            result[key] = deep_copy(source_value)
        elif isinstance(source_value, dict) and isinstance(result[key], dict):
            result[key] = deep_merge_objects_gaps(result[key], source_value, current_path)
        # If key exists and types differ, keep existing (don't override)

    return result


def deep_copy(obj):
    """Create a deep copy of a JSON-serializable object."""
    if isinstance(obj, dict):
        return {k: deep_copy(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [deep_copy(item) for item in obj]
    else:
        return obj


def canonical_json(obj):
    """
    Convert object to JSON string with keys sorted lexicographically at every level.
    """
    def sort_keys(item):
        if isinstance(item, dict):
            return {k: sort_keys(v) for k, v in sorted(item.items())}
        elif isinstance(item, list):
            return [sort_keys(elem) for elem in item]
        else:
            return item

    sorted_obj = sort_keys(obj)
    return json.dumps(sorted_obj, separators=(',', ':'))


def format_warning(warning):
    """Format a warning dict into the required string format."""
    return f"CONFLICT {warning['path']} {warning['loser']} -> {warning['winner']}"


def print_error_and_exit(code, message):
    """Print error JSON to stderr and exit with code 1."""
    error_obj = {
        "error": {
            "code": code,
            "message": message
        }
    }
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


def main():
    """Main entry point."""
    # Parse CLI args
    try:
        args = parse_cli_args(sys.argv[1:])
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    base_path = args['base']
    overrides_dir = args['overrides']
    flags_path = args['flags']
    env_path = args['env']
    out_dir = args['out']

    # Validate out directory exists and is a directory
    out_path = Path(out_dir)
    if not out_path.exists():
        print_error_and_exit(ERROR_MISSING_FILE, f"Output directory does not exist: {out_dir}")
    if not out_path.is_dir():
        print_error_and_exit(ERROR_INVALID_INPUT, f"Output path is not a directory: {out_dir}")

    # Track warnings across all merges
    all_warnings = []

    # Step 1: Read base.yaml (required)
    try:
        base_data = read_yaml_file(base_path, SOURCE_BASE)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    if base_data is None:
        base_data = {}

    # Step 2: Read defaults.yaml from overrides directory
    defaults_path = Path(overrides_dir) / 'defaults.yaml'
    try:
        defaults_data = read_yaml_file(str(defaults_path), SOURCE_DEFAULTS)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    if defaults_data is None:
        defaults_data = {}

    # Step 3: Read run.yaml from overrides directory (optional)
    run_path = Path(overrides_dir) / 'run.yaml'
    run_data = None
    if run_path.exists() and run_path.is_file():
        try:
            run_content = run_path.read_text(encoding='utf-8')
            if run_content.strip():
                try:
                    run_data = yaml.safe_load(run_content)
                except yaml.YAMLError:
                    print_error_and_exit(ERROR_PARSE, "Failed to parse run.yaml")
        except OSError:
            print_error_and_exit(ERROR_MISSING_FILE, "Cannot read run.yaml")
    # If file is empty or missing, run_data stays None

    # Step 4: Read flags.json (optional, may be missing or empty)
    try:
        flags_data = read_json_file(flags_path, SOURCE_FLAGS)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    if flags_data is None:
        flags_data = {}

    # Step 5: Read env file (parse but ignore for Part 1)
    try:
        env_data = read_env_file(env_path)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    # Step 6: Merge in order of precedence
    # Start with base.yaml
    merged = deep_copy(base_data)

    # Merge defaults.yaml
    warnings_defaults = []
    merged = deep_merge_dicts(
        merged, defaults_data,
        "", (SOURCE_BASE, SOURCE_DEFAULTS), warnings_defaults
    )
    all_warnings.extend(warnings_defaults)

    # Merge run.yaml if present
    if run_data is not None:
        warnings_run = []
        merged = deep_merge_dicts(
            merged, run_data,
            "", (SOURCE_DEFAULTS if defaults_data else SOURCE_BASE, SOURCE_RUN),
            warnings_run
        )
        all_warnings.extend(warnings_run)

    # Merge flags.json
    warnings_flags = []
    merged = deep_merge_dicts(
        merged, flags_data,
        "", (SOURCE_RUN if run_data else (SOURCE_DEFAULTS if defaults_data else SOURCE_BASE), SOURCE_FLAGS),
        warnings_flags
    )
    all_warnings.extend(warnings_flags)

    # Step 7: Apply defaults injection (fill gaps from defaults.yaml)
    # We need to capture the original defaults.yaml for defaults injection
    if defaults_data:
        merged = defaults_injection(merged, defaults_data)

    # Step 8: Validate
    validation_errors = validate_data(merged)

    if validation_errors:
        # Print first error and exit
        code, message = validation_errors[0]
        print_error_and_exit(code, message)

    # Step 9: Write outputs
    try:
        # Write final_spec.json
        spec_path = out_path / 'final_spec.json'
        json_str = canonical_json(merged)
        spec_path.write_text(json_str + '\n', encoding='utf-8')

        # Write warnings.txt
        warnings_path = out_path / 'warnings.txt'
        if all_warnings:
            warning_lines = [format_warning(w) + '\n' for w in all_warnings]
            warnings_path.write_text(''.join(warning_lines), encoding='utf-8')
        else:
            # Empty file with trailing newline
            warnings_path.write_text('\n', encoding='utf-8')

    except OSError:
        print_error_and_exit(ERROR_MISSING_FILE, "Failed to write output files")

    # Success - exit code 0
    sys.exit(0)


if __name__ == '__main__':
    main()
