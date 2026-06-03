#!/usr/bin/env python3
"""
synth_configs - CLI that turns layered YAML/JSON inputs into a canonical experiment spec.
"""

import argparse
import json
import os
import sys
from pathlib import Path
import re

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
ERROR_UNKNOWN_FRAGMENT = "UNKNOWN_FRAGMENT"
ERROR_FRAGMENT_CYCLE = "FRAGMENT_CYCLE"
ERROR_INVALID_TEMPLATE = "INVALID_TEMPLATE"
ERROR_UNKNOWN_ENV_VAR = "UNKNOWN_ENV_VAR"
ERROR_INVALID_MANIFEST = "INVALID_MANIFEST"
ERROR_RUN_FAILURE = "RUN_FAILURE"

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
        'fragments': None,
        'flags': None,
        'env': None,
        'out': None,
        'manifest': None
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
        elif arg == '--fragments':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --fragments")
            parsed['fragments'] = args[i + 1]
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
        elif arg == '--manifest':
            if i + 1 >= len(args):
                raise SynthConfigsError(ERROR_INVALID_INPUT, "Missing value for --manifest")
            parsed['manifest'] = args[i + 1]
            i += 2
        else:
            raise SynthConfigsError(ERROR_INVALID_INPUT, f"Unknown argument: {arg}")

    # Validate all required flags are present (including --manifest for Part 3)
    for key in ['base', 'overrides', 'fragments', 'flags', 'env', 'out', 'manifest']:
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
    """Read and parse an env file (KEY=VALUE per line). For Part 2, store full values."""
    path = Path(filepath)

    if not path.exists():
        return {}

    if not path.is_file():
        raise SynthConfigsError(ERROR_MISSING_FILE, f"env file is not a file: {filepath}")

    try:
        content = path.read_text(encoding='utf-8')
    except OSError:
        raise SynthConfigsError(ERROR_MISSING_FILE, "Cannot read env file")

    # Parse with actual values for Part 2
    env_data = {}
    for line in content.splitlines():
        line = line.strip()
        if line and '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip()
            if key:
                env_data[key] = value

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


# === Fragment Resolution ===

FRAGMENT_PLACEHOLDER_PREFIX = "{{fragment:"
FRAGMENT_PLACEHOLDER_SUFFIX = "}}"
FRAGMENT_NAME_PATTERN = r'^[a-z][a-z0-9_]*$'


def is_fragment_placeholder(value):
    """Check if a value is a fragment placeholder string."""
    return isinstance(value, str) and value.startswith(FRAGMENT_PLACEHOLDER_PREFIX) and value.endswith(FRAGMENT_PLACEHOLDER_SUFFIX)


def extract_fragment_name(placeholder):
    """Extract the fragment name from a placeholder string."""
    return placeholder[len(FRAGMENT_PLACEHOLDER_PREFIX):-len(FRAGMENT_PLACEHOLDER_SUFFIX)]


def validate_fragment_name(name):
    """Validate that a fragment name contains only lowercase letters, numbers, and underscores."""
    return bool(re.match(FRAGMENT_NAME_PATTERN, name))


def read_fragments(fragments_dir):
    """Read all fragment YAML files from the fragments directory.

    Returns a dict mapping fragment_name -> parsed_content.
    """

    fragments = {}
    fragments_path = Path(fragments_dir)

    if not fragments_path.exists() or not fragments_path.is_dir():
        return fragments

    for fragment_file in fragments_path.iterdir():
        if fragment_file.suffix != '.yaml' or fragment_file.name.startswith('.'):
            continue

        fragment_name = fragment_file.stem

        # Validate fragment name
        if not validate_fragment_name(fragment_name):
            raise SynthConfigsError(ERROR_INVALID_TEMPLATE, f"Invalid fragment name: {fragment_name}")

        # Read and parse fragment
        try:
            content = fragment_file.read_text(encoding='utf-8')
            if not content.strip():
                # Empty document counts as null, ignore it unless referenced
                fragment_data = None
            else:
                fragment_data = yaml.safe_load(content)
            fragments[fragment_name] = fragment_data
        except yaml.YAMLError:
            raise SynthConfigsError(ERROR_PARSE, f"Failed to parse fragment: {fragment_name}")
        except OSError:
            raise SynthConfigsError(ERROR_MISSING_FILE, f"Cannot read fragment: {fragment_name}")

    return fragments


def read_fragment_meta(fragments_dir):
    """Read fragments/meta.yaml for deprecation metadata.

    Returns a dict mapping fragment_name -> deprecation_info.
    """
    from pathlib import Path

    meta_path = Path(fragments_dir) / 'meta.yaml'
    if not meta_path.exists() or not meta_path.is_file():
        return {}

    try:
        content = meta_path.read_text(encoding='utf-8')
        if not content.strip():
            return {}
        meta_data = yaml.safe_load(content)
        return meta_data if isinstance(meta_data, dict) else {}
    except yaml.YAMLError:
        raise SynthConfigsError(ERROR_PARSE, "Failed to parse fragments/meta.yaml")
    except OSError:
        return {}


def find_referenced_fragments(data, visited=None, current_chain=None):
    """Find all fragment references in data and return them as a set.
    Also detects cycles.
    """
    if visited is None:
        visited = set()
    if current_chain is None:
        current_chain = []

    if isinstance(data, dict):
        for value in data.values():
            find_referenced_fragments(value, visited, current_chain)
    elif isinstance(data, list):
        for item in data:
            find_referenced_fragments(item, visited, current_chain)
    elif is_fragment_placeholder(data):
        name = extract_fragment_name(data)
        if name in current_chain:
            # Cycle detected
            cycle_chain = " -> ".join(current_chain[current_chain.index(name):] + [name])
            raise SynthConfigsError(ERROR_FRAGMENT_CYCLE, f"Fragment cycle detected: {cycle_chain}")
        if name not in visited:
            visited.add(name)
            current_chain.append(name)

    return visited


def strip_fragment_meta(data):
    """Remove _meta field from fragment content before insertion."""
    if isinstance(data, dict):
        return {k: strip_fragment_meta(v) for k, v in data.items() if k != '_meta'}
    elif isinstance(data, list):
        return [strip_fragment_meta(item) for item in data]
    else:
        return data


def resolve_fragment(data, fragments, visited_names=None, current_chain=None):
    """Recursively resolve all {{fragment:...}} placeholders in data.

    Args:
        data: The data structure to resolve
        fragments: Dict of fragment_name -> content
        visited_names: Set of already-visited fragment names (for cycle detection)
        current_chain: List tracking current resolution chain

    Returns:
        Resolved data with all fragments substituted
    """
    if visited_names is None:
        visited_names = set()
    if current_chain is None:
        current_chain = []

    if isinstance(data, dict):
        return {k: resolve_fragment(v, fragments, visited_names, current_chain) for k, v in data.items()}
    elif isinstance(data, list):
        return [resolve_fragment(item, fragments, visited_names, current_chain) for item in data]
    elif is_fragment_placeholder(data):
        name = extract_fragment_name(data)

        if not validate_fragment_name(name):
            raise SynthConfigsError(ERROR_INVALID_TEMPLATE, f"Invalid fragment name: {name}")

        if name in current_chain:
            cycle_chain = " -> ".join(current_chain[current_chain.index(name):] + [name])
            raise SynthConfigsError(ERROR_FRAGMENT_CYCLE, f"Fragment cycle detected: {cycle_chain}")

        if name not in fragments:
            raise SynthConfigsError(ERROR_UNKNOWN_FRAGMENT, f"Fragment not found: {name}")

        fragment_data = fragments[name]

        # If fragment is None (empty document), return None
        if fragment_data is None:
            return None

        # Strip _meta from fragment content before insertion
        fragment_data = strip_fragment_meta(fragment_data)

        # Resolve recursively and mark as visited
        new_chain = current_chain + [name]
        visited_names.add(name)
        return resolve_fragment(fragment_data, fragments, visited_names, new_chain)
    else:
        return data


# === Environment Variable Interpolation ===

import re as _re

VAR_TOKEN_PATTERN = _re.compile(r'\$\{([^}]+)\}')


def interpolate_values(data, env_vars, path=""):
    """
    Recursively interpolate ${VAR} tokens in all string values.

    Args:
        data: The data structure (dict, list, or scalar)
        env_vars: Dict of variable_name -> value
        path: Current JSON pointer path for error messages

    Returns:
        Data with all ${VAR} tokens replaced

    Raises:
        SynthConfigsError: If a referenced variable is missing
    """
    if isinstance(data, dict):
        return {k: interpolate_values(v, env_vars, f"{path}/{k}" if path else f"/{k}") for k, v in data.items()}
    elif isinstance(data, list):
        return [interpolate_values(item, env_vars, f"{path}[{i}]") for i, item in enumerate(data)]
    elif isinstance(data, str):
        result = data
        # Find all ${VAR} tokens
        matches = VAR_TOKEN_PATTERN.findall(data)
        for var_name in matches:
            if var_name not in env_vars:
                raise SynthConfigsError(ERROR_UNKNOWN_ENV_VAR, f"{var_name} is not defined")
            result = VAR_TOKEN_PATTERN.sub(env_vars[var_name], result, count=1)
        return result
    else:
        return data


def validate_interpolated_data(data):
    """
    Validate data after interpolation.
    Ensures strings produced via interpolation are still valid.
    """
    errors = []

    def _validate(obj, path=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = f"{path}/{key}" if path else f"/{key}"
                _validate(value, current_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _validate(item, f"{path}[{i}]")
        elif isinstance(obj, str):
            # Check for empty required strings
            # experiment_name is required at root, or nested
            if path == '/experiment_name' or path.endswith('/experiment_name'):
                if not obj.strip():
                    errors.append((ERROR_MISSING_KEY, "experiment_name must be a non-empty string after interpolation"))
        # For other types, validation is handled by validate_data

    _validate(data)
    return errors


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
    fragments_dir = args['fragments']
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

    # Step 5: Read env file (parse with values for Part 2)
    try:
        env_data = read_env_file(env_path)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    # Step 6: Read fragments
    try:
        fragments_data = read_fragments(fragments_dir)
        fragment_meta = read_fragment_meta(fragments_dir)
    except SynthConfigsError as e:
        print_error_and_exit(e.code, e.message)

    # Step 7: Merge in order of precedence
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

    # Step 8: Apply defaults injection (fill gaps from defaults.yaml)
    if defaults_data:
        merged = defaults_injection(merged, defaults_data)

    # Step 9: Fragment Resolution
    # First, collect fragment references and detect cycles
    try:
        referenced_names = find_referenced_fragments(merged)
    except SynthConfigsError as e:
        if e.code == ERROR_FRAGMENT_CYCLE:
            print_error_and_exit(e.code, e.message)
        raise

    # Check for unknown fragments
    for name in referenced_names:
        if name not in fragments_data:
            print_error_and_exit(ERROR_UNKNOWN_FRAGMENT, f"Fragment not found: {name}")

    # Track which fragments we've warned about for deprecations
    warned_deprecations = set()

    # Resolve fragments
    try:
        merged = resolve_fragment(merged, fragments_data)
    except SynthConfigsError as e:
        if e.code in (ERROR_FRAGMENT_CYCLE, ERROR_UNKNOWN_FRAGMENT):
            print_error_and_exit(e.code, e.message)
        raise

    # Step 10: Validate before interpolation
    validation_errors = validate_data(merged)

    if validation_errors:
        # Print first error and exit
        code, message = validation_errors[0]
        print_error_and_exit(code, message)

    # Step 11: Interpolate environment variables
    try:
        merged = interpolate_values(merged, env_data)
    except SynthConfigsError as e:
        if e.code == ERROR_UNKNOWN_ENV_VAR:
            print_error_and_exit(e.code, e.message)
        raise

    # Step 12: Validate after interpolation
    post_interp_errors = validate_interpolated_data(merged)

    if post_interp_errors:
        code, message = post_interp_errors[0]
        print_error_and_exit(code, message)

    # Step 13: Add fragment deprecation warnings
    for name in referenced_names:
        if name in fragment_meta:
            meta_entry = fragment_meta[name]
            if isinstance(meta_entry, dict) and 'deprecated_reason' in meta_entry:
                if name not in warned_deprecations:
                    reason = meta_entry['deprecated_reason']
                    all_warnings.append({
                        'type': 'FRAGMENT_DEPRECATED',
                        'name': name,
                        'reason': reason
                    })
                    warned_deprecations.add(name)

    # Step 14: Write outputs
    try:
        # Write final_spec.json
        spec_path = out_path / 'final_spec.json'
        json_str = canonical_json(merged)
        spec_path.write_text(json_str + '\n', encoding='utf-8')

        # Write warnings.txt
        warnings_path = out_path / 'warnings.txt'
        if all_warnings:
            warning_lines = []
            for w in all_warnings:
                if w.get('type') == 'CONFLICT':
                    warning_lines.append(format_warning(w) + '\n')
                elif w.get('type') == 'FRAGMENT_DEPRECATED':
                    warning_lines.append(f"FRAGMENT_DEPRECATED {w['name']} {w['reason']}\n")
            warnings_path.write_text(''.join(warning_lines), encoding='utf-8')
        else:
            # Empty file with trailing newline
            warnings_path.write_text('', encoding='utf-8')

    except OSError:
        print_error_and_exit(ERROR_MISSING_FILE, "Failed to write output files")

    # Success - exit code 0
    sys.exit(0)


if __name__ == '__main__':
    main()
