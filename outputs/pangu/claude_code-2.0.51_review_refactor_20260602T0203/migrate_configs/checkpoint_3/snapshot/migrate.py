#!/usr/bin/env python3
"""
CLI tool to migrate configuration files across different environments.
Applies transformation rules from a rules.json file to config files in various formats.
"""

import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import toml
import yaml
from iniparse import INIConfig


class ConfigParser:

    SUPPORTED_EXTENSIONS = {'.json', '.yaml', '.yml', '.toml', '.ini'}

    @classmethod
    def is_supported(cls, filepath: str) -> bool:
        """Check if file has a supported extension."""
        return Path(filepath).suffix.lower() in cls.SUPPORTED_EXTENSIONS

    @classmethod
    def load(cls, filepath: str) -> tuple[str, dict]:
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
        if fmt == 'json':
            return json.dumps(data, indent=2, ensure_ascii=False)
        elif fmt in ('yaml', '.yml'):
            return yaml.dump(data, Dumper=yaml.SafeDumper, default_flow_style=False, allow_unicode=True)
        elif fmt == 'toml':
            return toml.dumps(data)
        elif fmt == 'ini':
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


def match_pattern(key_pattern: str, path: list[str]) -> dict | None:
    """
    Match a key pattern against a path.
    Pattern syntax: $variable captures a component, * matches any component without capturing.
    Returns dict of captured variables if match, None otherwise.
    """
    pattern_parts = key_pattern.split('.')

    # Pattern and path must have the same number of parts
    if len(pattern_parts) != len(path):
        return None

    captures = {}

    for pattern_part, path_part in zip(pattern_parts, path):
        if pattern_part.startswith('$'):
            # Variable capture
            var_name = pattern_part[1:]  # Remove $ prefix
            captures[var_name] = path_part
        elif pattern_part == '*':
            # Wildcard - matches any component without capturing
            continue
        elif pattern_part != path_part:
            # Literal match failed
            return None

    return captures


def find_matching_keys(data: dict, key_pattern: str) -> list[tuple[list[str], dict]]:
    """
    Find all keys in nested dict that match the pattern.
    Returns list of (path_parts, captures) tuples.
    """
    matches = []

    def _search(current: Any, path: list[str]):
        if isinstance(current, dict):
            for key, value in current.items():
                new_path = path + [str(key)]
                captures = match_pattern(key_pattern, new_path)
                if captures is not None:
                    matches.append((new_path, captures))
                _search(value, new_path)
        elif isinstance(current, list):
            for i, item in enumerate(current):
                _search(item, path + [str(i)])

    _search(data, [])
    return matches


def apply_template(template: str, variables: dict) -> str:
    """Apply template with ${var} substitutions using variables dict."""
    result = template
    for var_name, var_value in variables.items():
        result = result.replace('${' + var_name + '}', str(var_value))
    return result


def validate_rules(rules: dict) -> list[str]:
    """Validate all rules. Returns list of error messages (empty if valid)."""
    errors = []

    for rule_name, rule in rules.items():
        rule_type = rule.get('type')

        if rule_type == 'pattern_replace':
            if 'key_pattern' not in rule:
                errors.append(f"Rule '{rule_name}': pattern_replace must have a 'key_pattern' field")

        elif rule_type == 'template_string':
            if 'template' not in rule:
                errors.append(f"Rule '{rule_name}': template_string must have a 'template' field")
            elif not isinstance(rule.get('template'), str):
                errors.append(f"Rule '{rule_name}': template must be a string")
            if 'variables' not in rule:
                errors.append(f"Rule '{rule_name}': template_string must have a 'variables' field")
            else:
                variables = rule.get('variables')
                if not isinstance(variables, dict):
                    errors.append(f"Rule '{rule_name}': variables must be a dict")
                else:
                    for var_name, var_path in variables.items():
                        if not isinstance(var_path, str):
                            errors.append(f"Rule '{rule_name}': variable '{var_name}' path must be a string")

        elif rule_type == 'conditional_replace':
            if 'condition' not in rule:
                errors.append(f"Rule '{rule_name}': conditional_replace must have a 'condition' block")
            else:
                condition = rule.get('condition')
                if not isinstance(condition, dict):
                    errors.append(f"Rule '{rule_name}': condition must be a dict")
                elif not condition:
                    errors.append(f"Rule '{rule_name}': condition block must have at least one condition type")
                else:
                    valid_condition_keys = {'current_value_equals', 'current_value_not_equals', 'current_value_matches'}
                    condition_keys = set(condition.keys())
                    if not condition_keys & valid_condition_keys:
                        errors.append(f"Rule '{rule_name}': condition must have at least one valid condition type")

                    # Validate regex patterns
                    if 'current_value_matches' in condition:
                        pattern = condition.get('current_value_matches')
                        if not isinstance(pattern, str):
                            errors.append(f"Rule '{rule_name}': current_value_matches must be a string")
                        else:
                            try:
                                re.compile(pattern)
                            except re.error as e:
                                errors.append(f"Rule '{rule_name}': invalid regex pattern in current_value_matches: {e}")

    return errors


def matches_glob(filepath: str, glob_pattern: str) -> bool:
    """Check if filepath matches glob pattern."""
    # Normalize separators to forward slashes
    filepath_norm = filepath.replace('\\', '/')
    pattern_norm = glob_pattern.replace('\\', '/')

    # Special case: **/* matches any file at any depth (including root)
    if pattern_norm == '**/*':
        return True

    def _glob_to_regex(pat: str) -> str:
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

            elif rule_type == 'pattern_replace':
                if self._apply_pattern_replace(data, rule):
                    applied.append(rule_name)

            elif rule_type == 'template_string':
                if self._apply_template_string(data, rule):
                    applied.append(rule_name)

            elif rule_type == 'conditional_replace':
                if self._apply_conditional_replace(data, rule):
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
        original = copy.deepcopy(data)

        # Deep merge using module-level function
        merged = deep_merge(data, merge_data)

        # Check if data was modified
        if merged != original:
            # Update data in place by clearing and updating
            data.clear()
            data.update(merged)
            return True
        return False

    def _apply_pattern_replace(self, data: dict, rule: dict) -> bool:
        """Apply pattern_replace rule. Returns True if data was modified."""
        key_pattern = rule.get('key_pattern')
        value_template = rule.get('value')

        if key_pattern is None or value_template is None:
            return False

        modified = False
        matches = find_matching_keys(data, key_pattern)

        for path_parts, captures in matches:
            # Apply template to get the new value
            new_value = apply_template(str(value_template), captures)
            path = '.'.join(path_parts)
            current_value = get_nested(data, path)

            # Only update if value actually changes
            if current_value != new_value:
                set_nested(data, path, new_value)
                modified = True

        return modified

    def _apply_template_string(self, data: dict, rule: dict) -> bool:
        """Apply template_string rule. Returns True if data was modified."""
        key = rule.get('key')
        template = rule.get('template')
        variables = rule.get('variables', {})

        if key is None or template is None:
            return False

        # Check if target key exists and contains a string value
        current_value = get_nested(data, key)
        if not isinstance(current_value, str):
            return False

        # Extract values from variable key paths
        var_values = {}
        for var_name, var_path in variables.items():
            if not isinstance(var_path, str):
                return False
            value = get_nested(data, var_path)
            if value is None:
                return False
            var_values[var_name] = value

        # Apply template
        new_value = apply_template(template, var_values)

        # Only update if value changes
        if current_value != new_value:
            set_nested(data, key, new_value)
            return True
        return False

    def _apply_conditional_replace(self, data: dict, rule: dict) -> bool:
        """Apply conditional_replace rule. Returns True if data was modified."""
        key = rule.get('key')
        value = rule.get('value')
        condition = rule.get('condition', {})

        if key is None or value is None:
            return False

        current_value = get_nested(data, key)
        if current_value is None:
            return False

        # Check conditions
        if 'current_value_equals' in condition:
            if str(current_value) != str(condition['current_value_equals']):
                return False

        if 'current_value_not_equals' in condition:
            if str(current_value) == str(condition['current_value_not_equals']):
                return False

        if 'current_value_matches' in condition:
            pattern = condition['current_value_matches']
            try:
                if not re.match(pattern, str(current_value)):
                    return False
            except re.error:
                return False

        # All conditions passed, perform replacement
        if current_value != value:
            set_nested(data, key, value)
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

    # Validate rules
    validation_errors = validate_rules(rules)
    if validation_errors:
        for error in validation_errors:
            print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)

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
