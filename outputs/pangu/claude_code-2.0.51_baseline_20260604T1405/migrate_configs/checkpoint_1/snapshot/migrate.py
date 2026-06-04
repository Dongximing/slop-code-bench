#!/usr/bin/env python3
"""
CLI tool to migrate configuration files across different environments.
Applies transformation rules from a rules.json file to all config files in a target directory.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from fnmatch import fnmatch
from typing import Any, Optional
from copy import deepcopy

import yaml
import toml


def load_rules(rules_path: str) -> dict:
    """Load and parse the rules JSON file."""
    try:
        with open(rules_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: rules file not found: {rules_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: rules file not found: {rules_path}", file=sys.stderr)
        sys.exit(1)


def set_nested_value(data: dict, key_path: str, value: Any) -> bool:
    """Set a value at a nested key path using dot notation. Returns True if successful."""
    keys = key_path.split('.')
    current = data

    # Navigate to the parent of the target key
    for key in keys[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]

    # Set the value at the target key
    if isinstance(current, dict):
        current[keys[-1]] = value
        return True
    return False


def get_nested_value(data: dict, key_path: str) -> tuple[Optional[dict], Optional[str], Optional[Any]]:
    """Get a value at a nested key path. Returns (parent_dict, key, value) or (None, None, None) if not found."""
    keys = key_path.split('.')
    current = data

    for key in keys[:-1]:
        if not isinstance(current, dict) or key not in current:
            return None, None, None
        current = current[key]

    last_key = keys[-1]
    if isinstance(current, dict) and last_key in current:
        return current, last_key, current[last_key]
    return None, None, None


def deep_merge_dicts(base: dict, merge: dict) -> dict:
    """Deep merge two dictionaries. Values from merge take precedence."""
    result = deepcopy(base)
    for key, value in merge.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def matches_glob(file_path: str, glob_pattern: str, target_dir: str) -> bool:
    """Check if a file path matches a glob pattern relative to target directory."""
    # Get relative path from target directory
    try:
        rel_path = os.path.relpath(file_path, target_dir)
    except ValueError:
        return False
    # Normalize path separators for cross-platform compatibility
    rel_path = rel_path.replace(os.sep, '/')

    # Convert glob pattern to regex for proper ** support
    import re
    pattern = glob_pattern
    # Escape literal dots
    pattern = pattern.replace('.', r'\.')
    # Handle ** - matches zero or more directory levels
    pattern = pattern.replace(r'\.\*\*', '.*')
    # Handle **/* - matches any file at any depth
    pattern = pattern.replace(r'\.\*/\*', '.*')
    # Handle * within a single path component
    # Replace escaped asterisks back
    pattern = pattern.replace(r'\.\*', '.*')
    # Convert ? to .
    pattern = pattern.replace('?', '.')
    # Convert single-level * to .*
    pattern = pattern.replace('*', '.*')
    # Add anchors
    pattern = '^' + pattern + '$'

    try:
        return bool(re.match(pattern, rel_path))
    except re.error:
        # Fall back to fnmatch if regex conversion fails
        return fnmatch(rel_path, glob_pattern)


def parse_config(file_path: str) -> tuple[Any, str]:
    """Parse a config file and return (data, format). Supported formats: json, yaml, yml, toml, ini"""
    ext = Path(file_path).suffix.lower()

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if ext in ('.json',):
        try:
            return json.loads(content), 'json'
        except json.JSONDecodeError as e:
            print(f"Error: failed to parse {file_path}", file=sys.stderr)
            sys.exit(1)

    elif ext in ('.yaml', '.yml'):
        try:
            return yaml.safe_load(content) or {}, 'yaml'
        except yaml.YAMLError as e:
            print(f"Error: failed to parse {file_path}", file=sys.stderr)
            sys.exit(1)

    elif ext in ('.toml',):
        try:
            return toml.loads(content), 'toml'
        except toml.TomlDecodeError as e:
            print(f"Error: failed to parse {file_path}", file=sys.stderr)
            sys.exit(1)

    elif ext in ('.ini',):
        # Parse INI format
        config = {}
        current_section = None
        for line in content.splitlines():
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith('#') or line.startswith(';'):
                continue
            # Check for section header
            if line.startswith('[') and line.endswith(']'):
                current_section = line[1:-1].strip()
                config[current_section] = {}
            elif '=' in line and current_section:
                # Parse key = value pair
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # Try to convert value to appropriate type
                if value.isdigit():
                    value = int(value)
                elif value.lower() == 'true':
                    value = True
                elif value.lower() == 'false':
                    value = False
                elif value.lower() == 'null':
                    value = None
                config[current_section][key] = value
        return config, 'ini'

    else:
        return None, None


def write_config(file_path: str, data: Any, fmt: str) -> None:
    """Write config data to file in the specified format."""
    if fmt == 'json':
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
    elif fmt in ('yaml', 'yml'):
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    elif fmt == 'toml':
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(toml.dumps(data))
    elif fmt == 'ini':
        with open(file_path, 'w', encoding='utf-8') as f:
            for section, values in data.items():
                f.write(f'[{section}]\n')
                for key, value in values.items():
                    # Convert value to string
                    if value is True:
                        str_value = 'true'
                    elif value is False:
                        str_value = 'false'
                    elif value is None:
                        str_value = 'null'
                    else:
                        str_value = str(value)
                    f.write(f'{key} = {str_value}\n')
                f.write('\n')


def apply_replace_value(data: dict, rule: dict) -> bool:
    """Apply a replace_value rule. Returns True if the value was changed."""
    key = rule.get('key')
    value = rule.get('value')
    if key is None or value is None:
        return False

    # Check if the key path exists
    parent, key_name, old_value = get_nested_value(data, key)
    if parent is None:
        return False

    # Only set if value is different
    if old_value != value:
        parent[key_name] = value
        return True
    return False


def apply_rename_key(data: dict, rule: dict) -> bool:
    """Apply a rename_key rule. Returns True if the key was renamed."""
    old_key = rule.get('old_key')
    new_key = rule.get('new_key')
    if old_key is None or new_key is None:
        return False

    parent, key_name, value = get_nested_value(data, old_key)
    if parent is None:
        return False

    # Remove old key and set new key
    del parent[key_name]
    # Use set_nested_value to handle nested new_key path
    return set_nested_value(data, new_key, value)


def apply_merge_data(data: dict, rule: dict, file_path: str, target_dir: str) -> bool:
    """Apply a merge_data rule if the file matches the glob pattern. Returns True if merged."""
    glob_pattern = rule.get('glob')
    merge_data = rule.get('data', {})

    if glob_pattern is None or not merge_data:
        return False

    # Check if file matches glob
    if not matches_glob(file_path, glob_pattern, target_dir):
        return False

    # Deep merge the data
    merged = deep_merge_dicts(data, merge_data)
    # Check if anything changed
    changed = merged != data
    if changed:
        # Update the data in place
        data.clear()
        data.update(merged)
    return changed


def process_file(file_path: str, rules: dict, target_dir: str) -> Optional[list]:
    """Process a single config file with all rules. Returns list of applied rule names or None if no changes."""
    # Parse the config file
    data, fmt = parse_config(file_path)
    if data is None:
        return None

    if not isinstance(data, dict):
        # Non-dict configs (like TOML arrays at root) can't be processed
        return None

    applied_rules = []
    original_data = deepcopy(data)

    # Apply rules in lexicographic order by rule name
    for rule_name in sorted(rules.keys()):
        rule = rules[rule_name]
        rule_type = rule.get('type')

        if rule_type == 'replace_value':
            if apply_replace_value(data, rule):
                applied_rules.append(rule_name)

        elif rule_type == 'rename_key':
            if apply_rename_key(data, rule):
                applied_rules.append(rule_name)

        elif rule_type == 'merge_data':
            if apply_merge_data(data, rule, file_path, target_dir):
                applied_rules.append(rule_name)

    # Only return applied rules if file was changed
    if applied_rules:
        # Write the modified data back to file
        write_config(file_path, data, fmt)
        return applied_rules

    return None


def find_config_files(target_dir: str) -> list:
    """Find all config files in the target directory recursively."""
    config_extensions = {'.json', '.yaml', '.yml', '.toml', '.ini'}
    config_files = []

    for root, dirs, files in os.walk(target_dir):
        for file in files:
            ext = Path(file).suffix.lower()
            if ext in config_extensions:
                config_files.append(os.path.join(root, file))

    return config_files


def main():
    parser = argparse.ArgumentParser(
        description='Migrate configuration files by applying transformation rules.'
    )
    parser.add_argument('rules_file', help='Path to the rules JSON file')
    parser.add_argument('target_dir', help='Path to the target directory containing config files')

    args = parser.parse_args()

    # Check if target directory exists
    if not os.path.isdir(args.target_dir):
        print(f"Error: target directory not found: {args.target_dir}", file=sys.stderr)
        sys.exit(1)

    # Load rules
    rules = load_rules(args.rules_file)

    # Find all config files
    config_files = find_config_files(args.target_dir)

    # Process each config file
    for file_path in config_files:
        applied_rules = process_file(file_path, rules, args.target_dir)
        if applied_rules:
            # Output JSONL event
            rel_path = os.path.relpath(file_path, args.target_dir)
            event = {
                "event": "file_updated",
                "file": rel_path,
                "rules_applied": applied_rules
            }
            print(json.dumps(event))


if __name__ == '__main__':
    main()
