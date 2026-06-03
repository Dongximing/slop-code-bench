#!/usr/bin/env python3
"""
CLI tool to migrate configuration files across different environments.
Applies transformation rules from a rules.json file to config files in various formats.
"""

import json
import os
import sys
from pathlib import Path, PurePath
from typing import Any, Optional

import yaml
import toml
from io import StringIO
from iniparse import INIConfig


class ConfigParser:
    """Handles loading and dumping of configuration files in various formats."""

    SUPPORTED_EXTENSIONS = {'.json', '.yaml', '.yml', '.toml', '.ini'}

    @classmethod
    def is_supported(cls, filepath: str) -> bool:
        """Check if file has a supported extension."""
        return Path(filepath).suffix.lower() in cls.SUPPORTED_EXTENSIONS

    @classmethod
    def load(cls, filepath: str) -> tuple[str, dict]:
        """Load a config file and return (format, data)."""
        ext = Path(filepath).suffix.lower()

        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        if ext == '.json':
            return 'json', json.loads(content)
        elif ext in ('.yaml', '.yml'):
            return 'yaml', yaml.safe_load(content) or {}
        elif ext == '.toml':
            return 'toml', toml.loads(content)
        elif ext == '.ini':
            # Parse INI and convert to nested dict
            ini = INIConfig(content)
            data = {}
            for section in ini:
                section_data = {}
                for key, value in ini[section].items():
                    # Handle lists (comma-separated values)
                    if isinstance(value, str) and ',' in value:
                        section_data[key] = [v.strip() for v in value.split(',')]
                    else:
                        section_data[key] = value
                data[section] = section_data
            return 'ini', data
        else:
            raise ValueError(f"Unsupported format: {ext}")

    @classmethod
    def dump(cls, data: dict, fmt: str) -> str:
        """Dump data to a string in the specified format."""
        if fmt == 'json':
            return json.dumps(data, indent=2, ensure_ascii=False)
        elif fmt in ('yaml', '.yml'):
            return yaml.dump(data, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True)
        elif fmt == 'toml':
            return toml.dumps(data)
        elif fmt == 'ini':
            # Convert nested dict back to INI format
            lines = []
            for section, section_data in data.items():
                lines.append(f"[{section}]")
                for key, value in section_data.items():
                    if isinstance(value, list):
                        lines.append(f"{key} = {', '.join(str(v) for v in value)}")
                    else:
                        lines.append(f"{key} = {value}")
                lines.append("")
            return '\n'.join(lines)
        else:
            raise ValueError(f"Unsupported format: {fmt}")


def get_nested(data: dict, path: str) -> Optional[Any]:
    """Get value at dot-notation path, or None if not found."""
    keys = path.split('.')
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_nested(data: dict, path: str, value: Any) -> bool:
    """Set value at dot-notation path. Create intermediate dicts as needed. Returns True if set."""
    keys = path.split('.')
    current = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}
        current = current[key]
    last_key = keys[-1]
    current[last_key] = value
    return True


def delete_nested(data: dict, path: str) -> bool:
    """Delete value at dot-notation path. Returns True if deleted."""
    keys = path.split('.')
    current = data
    for key in keys[:-1]:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    last_key = keys[-1]
    if last_key in current:
        del current[last_key]
        return True
    return False


def deep_merge(base: dict, merge: dict) -> dict:
    """Deep merge two dictionaries. Values in merge take precedence."""
    result = base.copy()
    for key, value in merge.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def matches_glob(filepath: str, glob_pattern: str) -> bool:
    """Check if filepath matches glob pattern."""
    # Normalize separators to forward slashes
    filepath_norm = filepath.replace('\\', '/')
    pattern_norm = glob_pattern.replace('\\', '/')

    # Special case: **/* matches any file at any depth (including root)
    if pattern_norm == '**/*':
        return True

    def _glob_to_regex(pat: str) -> str:
        """Convert Unix shell-style glob pattern to regex."""
        i = 0
        n = len(pat)
        res = ''
        while i < n:
            c = pat[i]
            i += 1
            if c == '*':
                # Check for **
                if i + 1 < n and pat[i] == '*' and pat[i+1] == '/':
                    res += '.*'
                    i += 1  # skip second *
                    i += 1  # skip /
                    continue
                elif i < n and pat[i] == '*':
                    res += '.*'
                    i += 1
                    continue
                else:
                    res += '[^/]*'
            elif c == '?':
                res += '[^/]'
            elif c == '[':
                # Handle character class
                j = i
                if j < n and pat[j] == '!':
                    j += 1
                if j < n and pat[j] == ']':
                    j += 1
                while j < n and pat[j] != ']':
                    j += 1
                if j >= n:
                    res += r'\['
                else:
                    stuff = pat[i:j]
                    if stuff and stuff[0] == '!':
                        stuff = '^' + stuff[1:]
                    elif stuff and stuff[0] == '^':
                        stuff = '\\\\' + stuff
                    res += '[' + stuff + ']'
                    i = j + 1
            else:
                res += re.escape(c)
        return res + r'\Z'

    import re
    regex = _glob_to_regex(pattern_norm)
    return bool(re.match(regex, filepath_norm))


class RuleApplier:
    """Applies transformation rules to configuration data."""

    def __init__(self, rules: dict):
        self.rules = rules
        # Sort rules lexicographically by name
        self.sorted_rules = sorted(rules.items(), key=lambda x: x[0])

    def apply_rules(self, data: dict, filepath: str, relative_path: str) -> list[str]:
        """Apply all applicable rules to data. Returns list of rule names applied."""
        applied = []

        for rule_name, rule in self.sorted_rules:
            rule_type = rule.get('type')

            if rule_type == 'replace_value':
                if self._apply_replace_value(data, rule):
                    applied.append(rule_name)

            elif rule_type == 'rename_key':
                if self._apply_rename_key(data, rule):
                    applied.append(rule_name)

            elif rule_type == 'merge_data':
                glob_pattern = rule.get('glob', '**/*')
                if matches_glob(relative_path, glob_pattern):
                    if self._apply_merge_data(data, rule):
                        applied.append(rule_name)

        return applied

    def _apply_replace_value(self, data: dict, rule: dict) -> bool:
        """Apply replace_value rule. Returns True if data was modified."""
        key = rule.get('key')
        value = rule.get('value')
        if key is None or value is None:
            return False

        if get_nested(data, key) is not None:
            set_nested(data, key, value)
            return True
        return False

    def _apply_rename_key(self, data: dict, rule: dict) -> bool:
        """Apply rename_key rule. Returns True if data was modified."""
        old_key = rule.get('old_key')
        new_key = rule.get('new_key')
        if old_key is None or new_key is None:
            return False

        value = get_nested(data, old_key)
        if value is not None:
            # Delete old key first
            delete_nested(data, old_key)
            # Set new key
            set_nested(data, new_key, value)
            return True
        return False

    def _apply_merge_data(self, data: dict, rule: dict) -> bool:
        """Apply merge_data rule. Returns True if data was modified."""
        merge_data = rule.get('data')
        if merge_data is None:
            return False

        # Create a deep copy to check if changes occur
        import copy
        original = copy.deepcopy(data)

        # Deep merge
        def do_merge(base: dict, merge: dict) -> dict:
            result = base.copy()
            for key, value in merge.items():
                if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                    result[key] = do_merge(result[key], value)
                else:
                    result[key] = value
            return result

        merged = do_merge(data, merge_data)

        # Check if data was modified
        if merged != original:
            # Update data in place by clearing and updating
            data.clear()
            data.update(merged)
            return True
        return False


def find_config_files(target_dir: str) -> list[str]:
    """Find all supported config files recursively in target directory."""
    config_files = []
    target_path = Path(target_dir)

    for filepath in target_path.rglob('*'):
        if filepath.is_file() and ConfigParser.is_supported(str(filepath)):
            config_files.append(str(filepath))

    return config_files


def main():
    """Main entry point."""
    if len(sys.argv) != 3:
        print("Usage: python migrate.py <rules.json> <target_directory>", file=sys.stderr)
        sys.exit(1)

    rules_file = sys.argv[1]
    target_dir = sys.argv[2]

    # Validate rules file
    if not os.path.exists(rules_file):
        print(f"Error: rules file not found: {rules_file}", file=sys.stderr)
        sys.exit(1)

    # Validate target directory
    if not os.path.isdir(target_dir):
        print(f"Error: target directory not found: {target_dir}", file=sys.stderr)
        sys.exit(1)

    # Load rules
    try:
        with open(rules_file, 'r', encoding='utf-8') as f:
            rules = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in rules file: {rules_file}", file=sys.stderr)
        sys.exit(1)

    rule_applier = RuleApplier(rules)

    # Find all config files
    config_files = find_config_files(target_dir)

    # Process each file
    for filepath in config_files:
        try:
            fmt, data = ConfigParser.load(filepath)
        except Exception as e:
            print(f"Error: failed to parse {filepath}", file=sys.stderr)
            sys.exit(1)

        # Get relative path for glob matching
        relative_path = os.path.relpath(filepath, target_dir)

        # Apply rules
        applied = rule_applier.apply_rules(data, filepath, relative_path)

        # If file was modified, write it back and output event
        if applied:
            output = ConfigParser.dump(data, fmt)
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(output)

            event = {
                "event": "file_updated",
                "file": relative_path,
                "rules_applied": applied
            }
            print(json.dumps(event))


if __name__ == '__main__':
    main()
