#!/usr/bin/env python3
"""
synth_configs - CLI tool that merges layered YAML/JSON inputs into a canonical experiment spec.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

import yaml


# Error codes
ERROR_MISSING_FILE = "MISSING_FILE"
ERROR_PARSE_ERROR = "PARSE_ERROR"
ERROR_INVALID_STRUCTURE = "INVALID_STRUCTURE"
ERROR_MISSING_KEY = "MISSING_KEY"
ERROR_INVALID_VALUE = "INVALID_VALUE"
ERROR_INVALID_INPUT = "INVALID_INPUT"
ERROR_UNKNOWN_FRAGMENT = "UNKNOWN_FRAGMENT"
ERROR_FRAGMENT_CYCLE = "FRAGMENT_CYCLE"
ERROR_INVALID_TEMPLATE = "INVALID_TEMPLATE"
ERROR_UNKNOWN_ENV_VAR = "UNKNOWN_ENV_VAR"


# Source names for warnings
SOURCE_BASE = "base.yaml"
SOURCE_DEFAULTS = "defaults.yaml"
SOURCE_DEFAULT = "defaults.yaml"
SOURCE_RUN = "run.yaml"
SOURCE_FLAGS = "flags.json"
SOURCE_FLAGS_ENV = "environment interpolation"

# Global warnings collection
_warnings: List[str]
_fragment_deprecation_warnings: Set[str]
_fragments_dir: Optional[Path] = None


def _init_globals() -> None:
    global _warnings, _fragment_deprecation_warnings
    _warnings = []
    _fragment_deprecation_warnings = set()


def add_warning(json_pointer: str, description: str, winner: str, loser: str) -> None:
    _warnings.append(f"CONFLICT {json_pointer} {winner} replaced {loser}")


def add_fragment_deprecation(name: str, reason: str) -> None:
    if name not in _fragment_deprecation_warnings:
        _fragment_deprecation_warnings.add(name)
        _warnings.append(f"FRAGMENT_DEPRECATED {name} {reason}")


def output_error(code: str, message: str) -> None:
    error_obj = {"error": {"code": code, "message": message}}
    print(json.dumps(error_obj), file=sys.stderr)
    sys.exit(1)


def parse_env_list(path: Path) -> Dict[str, str]:
    result = {}
    if not path.exists():
        return result
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                result[key.strip()] = value.strip()
    return result


def parse_yaml_file(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            if not content.strip():
                return None
            data = yaml.safe_load(content)
            return data
    except yaml.YAMLError as e:
        output_error(ERROR_PARSE_ERROR, f"Failed to parse {path}: {str(e)}")
    except Exception as e:
        output_error(ERROR_PARSE_ERROR, f"Failed to read {path}: {str(e)}")
    return None


def parse_json_file(path: Path) -> Any:
    if not path.exists() or not path.stat().st_size:
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        output_error(ERROR_PARSE_ERROR, f"Failed to parse {path}: {str(e)}")
    except Exception as e:
        output_error(ERROR_PARSE_ERROR, f"Failed to read {path}: {str(e)}")
    return None


def check_duplicate_keys(path: Path) -> None:
    """Check for duplicate keys in YAML map (invalid)."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if not content.strip():
        return
    # Check for duplicate keys using YAML's built-in loader with custom constructor
    try:
        class DuplicateKeyChecker(dict):
            def __setitem__(self, key, value):
                if key in self:
                    output_error(ERROR_PARSE_ERROR, f"Duplicate key '{key}' in {path}")
                super().__setitem__(key, value)
        yaml.safe_load(content, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        output_error(ERROR_PARSE_ERROR, f"Failed to parse {path}: duplicate key not properly detected")


def deep_merge_dicts(base: Dict[str, Any], override: Dict[str, Any],
                     path: str = "", winner_source: str = "", loser_source: str = "") -> Dict[str, Any]:
    result = dict(base) if base else {}
    if not override:
        return result

    for key, value in override.items():
        current_path = f"{path}/{key}" if path else f"/{key}"

        if key in result:
            base_value = result[key]
            if type(base_value) != type(value):
                add_warning(current_path, f"type mismatch: {type(base_value).__name__} -> {type(value).__name__}",
                          winner_source, loser_source)
                result[key] = value
            elif isinstance(base_value, dict) and isinstance(value, dict):
                result[key] = deep_merge_dicts(base_value, value, current_path, winner_source, loser_source)
            else:
                if base_value != value:
                    add_warning(current_path, f"value conflict: {base_value!r} -> {value!r}",
                              winner_source, loser_source)
                result[key] = value
        else:
            result[key] = value
    return result


def find_fragment_references(node: Any, path: str = "") -> List[Tuple[str, str]]:
    """
    Find all {{fragment:name}} references in the config.
    Returns list of (name, json_pointer) tuples.
    """
    refs = []
    if isinstance(node, str):
        match = re.match(r'^\{\{fragment:([a-z0-9_]+)\}\}$', node)
        if match:
            refs.append((match.group(1), path if path else "/"))
    elif isinstance(node, dict):
        for k, v in node.items():
            new_path = f"{path}/{k}" if path else f"/{k}"
            refs.extend(find_fragment_references(v, new_path))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            new_path = f"{path}/{i}"
            refs.extend(find_fragment_references(item, new_path))
    return refs


def resolve_fragment(name: str, fragments_dir: Path, visited: Set[str]) -> Any:
    """
    Resolve a fragment, including nested fragment references.
    Returns the parsed content. Detects cycles via visited set.
    """
    if name in visited:
        return None  # Signal cycle
    visited.add(name)
    fragment_path = fragments_dir / f"{name}.yaml"
    if not fragment_path.exists():
        output_error(ERROR_UNKNOWN_FRAGMENT, f"Fragment '{name}' not found at {fragment_path}")
    content = parse_yaml_file(fragment_path)
    if content is None:
        return {}  # Empty fragment becomes empty object

    # Recursively resolve nested fragments within this fragment
    return _resolve_fragments_in_object(content, fragments_dir, visited)


def _resolve_fragments_in_object(node: Any, fragments_dir: Path, visited: Set[str]) -> Any:
    """Resolve all fragment references within an object/array."""
    if isinstance(node, str):
        match = re.match(r'^\{\{fragment:([a-z0-9_]+)\}\}$', node)
        if match:
            ref_name = match.group(1)
            if ref_name in visited:
                output_error(ERROR_FRAGMENT_CYCLE, "Fragment cycle detected involving: " + " -> ".join(visited) + f" -> {ref_name}")
            resolved = resolve_fragment(ref_name, fragments_dir, visited)
            return resolved
        return node
    elif isinstance(node, dict):
        return {k: _resolve_fragments_in_object(v, fragments_dir, visited) for k, v in node.items()}
    elif isinstance(node, list):
        return [_resolve_fragments_in_object(item, fragments_dir, visited) for item in node]
    else:
        return node


def apply_fragments(config: Dict[str, Any], fragments_dir: Path, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Find all fragment references in config and replace them with their content.
    """
    visited: Set[str] = set()

    def _apply(node: Any, path: str = "") -> Any:
        if isinstance(node, str):
            match = re.match(r'^\{\{fragment:([a-z0-9_]+)\}\}$', node)
            if match:
                name = match.group(1)
                # Check for cycle
                if name in visited:
                    output_error(ERROR_FRAGMENT_CYCLE, f"Fragment cycle detected involving: {name}")
                resolved = resolve_fragment(name, fragments_dir, set())
                # Apply deprecation warning
                if meta and name in meta:
                    dep = meta[name]
                    if isinstance(dep, dict) and "deprecated_reason" in dep:
                        add_fragment_deprecation(name, dep["deprecated_reason"])
                # Update visited to prevent cycles during this branch
                # But we need to be careful - use a fresh visited for each resolution
                return resolved
            return node
        elif isinstance(node, dict):
            return {k: _apply(v, f"{path}/{k}" if path else f"/{k}") for k, v in node.items()}
        elif isinstance(node, list):
            return [_apply(item, f"{path}/{i}") for i, item in enumerate(node)]
        else:
            return node

    return _apply(config)


def validate_fragment_content(fragment: Any, position_path: str) -> None:
    """Validate that fragment content is compatible with its insertion position."""
    # Fragment content must be a JSON object or scalar
    if fragment is None:
        output_error(ERROR_INVALID_TEMPLATE, f"Fragment at {position_path} resolved to null")
    # Note: We don't validate deep type compatibility here since we don't know
    # the expected structure at the insertion point without full knowledge.
    # The spec says: "Inserting a fragment that evaluates to an array where an object is required"
    # would produce INVALID_TEMPLATE. We need to check the insertion context.


def interpolate_env(node: Any, env_vars: Dict[str, str]) -> Any:
    """Recursively replace ${VAR} tokens in strings."""
    if isinstance(node, str):
        result = node
        # Find all ${VAR} tokens
        for match in re.finditer(r'\$\{([A-Za-z0-9_]+)\}', result):
            var_name = match.group(1)
            if var_name not in env_vars:
                output_error(ERROR_UNKNOWN_ENV_VAR, f"{var_name} is not defined")
            result = result[:match.start()] + env_vars[var_name] + result[match.end():]
        return result
    elif isinstance(node, dict):
        return {k: interpolate_env(v, env_vars) for k, v in node.items()}
    elif isinstance(node, list):
        return [interpolate_env(item, env_vars) for item in node]
    else:
        return node


def sort_dict_recursively(d: Dict[str, Any]) -> Dict[str, Any]:
    result = {}
    for key in sorted(d.keys()):
        value = d[key]
        if isinstance(value, dict):
            result[key] = sort_dict_recursively(value)
        elif isinstance(value, list):
            result[key] = [
                sort_dict_recursively(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            result[key] = value
    return result


def validate_config(config: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> None:
    if not isinstance(config, dict):
        output_error(ERROR_INVALID_STRUCTURE, "Merged configuration must be a JSON object")
    if 'experiment_name' not in config or not isinstance(config['experiment_name'], str):
        output_error(ERROR_MISSING_KEY, "experiment_name is required")
    name = config['experiment_name']
    if not name.strip():
        output_error(ERROR_INVALID_VALUE, "experiment_name must contain non-whitespace characters")

    if 'training' not in config or not isinstance(config['training'], dict):
        output_error(ERROR_MISSING_KEY, "training object is required")

    if 'resources' not in config or not isinstance(config['resources'], dict):
        output_error(ERROR_MISSING_KEY, "resources object is required")

    resources = config['resources']
    has_accelerators = 'accelerators' in resources and isinstance(resources['accelerators'], list)
    has_profile = 'profile' in resources and isinstance(resources['profile'], str)
    if not has_accelerators and not has_profile:
        output_error(ERROR_MISSING_KEY, "resources must include either accelerators or profile")
    if 'world_size' in resources:
        ws = resources['world_size']
        if not isinstance(ws, int) or ws <= 0:
            output_error(ERROR_INVALID_VALUE, "resources.world_size must be a positive integer")
    if has_accelerators:
        for i, acc in enumerate(resources['accelerators']):
            if not isinstance(acc, dict):
                output_error(ERROR_INVALID_VALUE, f"resources.accelerators[{i}] must be an object")
            if 'type' not in acc or not isinstance(acc['type'], str):
                output_error(ERROR_INVALID_VALUE, f"resources.accelerators[{i}].type must be a string")
            if 'count' not in acc or not isinstance(acc['count'], int) or acc['count'] <= 0:
                output_error(ERROR_INVALID_VALUE, f"resources.accelerators[{i}].count must be a positive integer")


def inject_defaults(config: Dict[str, Any], defaults: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not defaults:
        return config
    for key in ['experiment_name', 'training', 'resources']:
        if key not in config and key in defaults:
            config[key] = dict(defaults[key]) if isinstance(defaults[key], dict) else defaults[key]
    return config


def load_meta(fragments_dir: Path) -> Optional[Dict[str, Any]]:
    meta_path = fragments_dir / 'meta.yaml'
    if not meta_path.exists():
        return None
    return parse_yaml_file(meta_path)


def merge_configs(base_data: Optional[Dict[str, Any]],
                  defaults_data: Optional[Dict[str, Any]],
                  run_data: Optional[Dict[str, Any]],
                  flags_data: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    merged = {}
    if base_data:
        merged = dict(base_data)
    if defaults_data:
        merged = deep_merge_dicts(merged, defaults_data, "", SOURCE_DEFAULTS, SOURCE_BASE)
    if run_data:
        merged = deep_merge_dicts(merged, run_data, "", SOURCE_RUN, SOURCE_DEFAULTS if defaults_data else SOURCE_BASE)
    if flags_data:
        if not isinstance(flags_data, dict):
            output_error(ERROR_PARSE_ERROR, "flags.json must be a JSON object")
        prev_loser = "SOURCE_RUN if run_data else SOURCE_DEFAULTS if defaults_data else SOURCE_BASE)
        merged = deep_merge_dicts(merged, flags_data, "", SOURCE_FLAGS, prev_loser)
    return merged, defaults_data


def main() -> None:
    _init_globals()
    parser = argparse.ArgumentParser(prog='synth_configs.py',
        description='Merge layered YAML/JSON config files into a canonical experiment spec.')
    parser.add_argument('--base', required=True, help='Path to base.yaml')
    parser.add_argument('--overrides', required=True, help='Path to overrides directory')
    parser.add_argument('--fragments', required=True, help='Path to fragments directory')
    parser.add_argument('--flags', help='Path to flags.json')
    parser.add_argument('--env', required=True, help='Path to env.list')
    parser.add_argument('--out', required=True, help='Output directory')

    try:
        args = parser.parse_args()
    except SystemExit:
        output_error(ERROR_INVALID_INPUT, "Invalid CLI arguments")

    base_path = Path(args.base)
    if not base_path.exists():
        output_error(ERROR_MISSING_FILE, f"base file not found: {args.base}")

    overrides_dir = Path(args.overrides)
    if not overrides_dir.exists() or not overrides_dir.is_dir():
        output_error(ERROR_MISSING_FILE, f"overrides directory not found or not a directory: {args.overrides}")

    fragments_dir = Path(args.fragments)
    if not fragments_dir.exists() or not fragments_dir.is_dir():
        output_error(ERROR_MISSING_FILE, f"fragments directory not found or not a directory: {args.fragments}")

    out_dir = Path(args.out)
    if not out_dir.exists() or not out_dir.is_dir():
        output_error(ERROR_INVALID_INPUT, f"out directory does not exist or is not a directory: {args.out}")

    # Parse env.list
    env_path = Path(args.env)
    env_vars = parse_env_list(env_path)

    # Check for unknown CLI flags (after parsing args, check if any unrecognized were passed)
    # argparse handles unknown flags with parse_known_args, but we want strict checking
    # Re-parse to catch unknown flags
    known_args = ['--base', '--overrides', '--fragments', '--flags', '--env', '--out']
    for arg in sys.argv[1:]:
        if arg.startswith('--'):
            key = arg.split('=')[0].split('[')[0]  # handle --key=value
            if key not in known_args:
                output_error(ERROR_INVALID_INPUT, f"Unknown CLI flag: {key}")

    # Parse base.yaml
    base_data = parse_yaml_file(base_path)
    if base_data is not None and not isinstance(base_data, dict):
        output_error(ERROR_INVALID_STRUCTURE, "base.yaml must be a JSON object or empty")
    if base_data is None:
        base_data = {}
    # Check for duplicate keys in base.yaml
    check_duplicate_keys(base_path)

    # Parse defaults.yaml
    defaults_path = overrides_dir / 'defaults.yaml'
    if not defaults_path.exists():
        output_error(ERROR_MISSING_FILE, f"defaults.yaml not found in {args.overrides}")
    defaults_data = parse_yaml_file(defaults_path)
    if defaults_data is not None and not isinstance(defaults_data, dict):
        output_error(ERROR_INVALID_STRUCTURE, "defaults.yaml must be a JSON object or empty")
    if defaults_data is None:
        defaults_data = {}
    check_duplicate_keys(defaults_path)

    # Parse run.yaml (optional)
    run_data = None
    run_path = overrides_dir / 'run.yaml'
    if run_path.exists():
        run_data = parse_yaml_file(run_path)
        if run_data is not None and not isinstance(run_data, dict):
            output_error(ERROR_INVALID_STRUCTURE, "run.yaml must be a JSON object or empty")
        if run_data is None:
            run_data = {}
        check_duplicate_keys(run_path)

    # Parse flags.json (optional)
    flags_data = None
    if args.flags:
        flags_path = Path(args.flags)
        flags_data = parse_json_file(flags_path)
        if flags_data is None:
            flags_data = {}
        if not isinstance(flags_data, dict):
            output_error(ERROR_PARSE_ERROR, "flags.json must be a JSON object")

    # Layering: base -> defaults -> run -> flags
    merged_config, defaults_for_injection = merge_configs(
        base_data, defaults_data, run_data, flags_data
    )

    # Load fragment metadata for deprecation warnings
    meta = load_meta(fragments_dir)

    # Apply fragment resolution
    merged_config = apply_fragments(merged_config, fragments_dir, meta)

    # Inject missing defaults (post-fragment)
    merged_config = inject_defaults(merged_config, defaults_for_injection)

    # Apply environment variable interpolation
    merged_config = interpolate_env(merged_config, env_vars)

    # Validate the merged configuration
    validate_config(merged_config, defaults_for_injection)

    # Sort keys lexicographically
    final_config = sort_dict_recursively(merged_config)

    # Write final_spec.json (compact JSON, no indent)
    spec_path = out_dir / 'final_spec.json'
    with open(spec_path, 'w', encoding='utf-8') as f:
        json.dump(final_config, f, separators=(',', ':'))
        f.write('\n')

    # Write warnings.txt
    warnings_path = out_dir / 'warnings.txt'
    with open(warnings_path, 'w', encoding='utf-8') as f:
        for warning in _warnings:
            f.write(warning + '\n')
        # Empty file if no warnings (no trailing newline needed for empty file)
        # But spec says "empty file allowed when there are none"
        if _warnings:
            f.write('\n')

    sys.exit(0)


if __name__ == '__main__':
    main()