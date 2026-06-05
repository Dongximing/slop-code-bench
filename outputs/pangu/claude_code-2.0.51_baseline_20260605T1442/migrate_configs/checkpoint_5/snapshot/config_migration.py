#!/usr/bin/env python3
"""Config Migration Tool - Apply transformation rules to configuration files with array patterns and inheritance."""

import argparse
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Dict, List, Tuple, Set

import yaml

# Handle TOML import gracefully
try:
    import tomli
    import tomli_w
    HAS_TOML = True
except ImportError:
    HAS_TOML = False

# Regex for ${variable} substitution in templates
TEMPLATE_VAR_PATTERN = re.compile(r'\$\{(\w+)\}')
# Regex for $variable path matching (checkpoint 2 syntax)
PATH_VAR_PATTERN = re.compile(r'\$(\w+)')


def substitute_template(template: str, variables: Dict[str, Any]) -> str:
    """Substitute ${variable} and $variable patterns in a template string."""
    result = template

    # First handle ${variable} syntax
    for match in TEMPLATE_VAR_PATTERN.finditer(template):
        var_name = match.group(1)
        if var_name in variables:
            result = result.replace(match.group(0), str(variables[var_name]))

    # Then handle $variable syntax (but not ${variable} which was already handled)
    # Only substitute $var if it wasn't already substituted
    remaining = result
    for match in PATH_VAR_PATTERN.finditer(remaining):
        var_name = match.group(1)
        original = '$' + var_name
        # Check if this exact pattern was substituted
        if original in template and '${' + var_name + '}' not in template:
            if var_name in variables:
                result = result.replace(original, str(variables[var_name]))

    return result


class RuleValidationError(Exception):
    """Raised when a rule fails validation."""
    pass


class ConfigLoader:
    """Load and save configuration files in various formats."""

    @staticmethod
    def _get_nested_value(data: Dict[str, Any], key_path: str) -> Any:
        """Get a nested value by key path. Supports array indices."""
        parts = ConfigLoader._split_key_path(key_path)
        value = data
        for part in parts:
            if isinstance(value, dict):
                if part in value:
                    value = value[part]
                else:
                    return None
            elif isinstance(value, list):
                # Try to parse as array index
                if part.isdigit():
                    idx = int(part)
                    if 0 <= idx < len(value):
                        value = value[idx]
                    else:
                        return None
                else:
                    # Filter on array - look for items with this key
                    matched_items = []
                    for item in value:
                        if isinstance(item, dict) and part in item:
                            matched_items.append(item[part])
                    if len(matched_items) == 1:
                        value = matched_items[0]
                    elif len(matched_items) > 1:
                        # If multiple items match, return the first one
                        value = matched_items[0]
                    else:
                        return None
            else:
                return None
        return value

    @staticmethod
    def _get_nested_with_parent(data: Dict[str, Any], key_path: str) -> Tuple[Optional[Dict], Optional[str], Any]:
        """Get a nested value with its parent dict and key name."""
        parts = ConfigLoader._split_key_path(key_path)
        parent = data

        for i, part in enumerate(parts[:-1]):
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                return None, None, None

        last_key = parts[-1]
        if not isinstance(parent, dict) or last_key not in parent:
            return None, None, None

        return parent, last_key, parent[last_key]

    @staticmethod
    def _split_key_path(key_path: str) -> List[str]:
        """Split a key path into components, handling array notation."""
        parts = []
        i = 0
        current = ""

        while i < len(key_path):
            if key_path[i] == '.':
                if current:
                    parts.append(current)
                current = ""
                i += 1
            elif key_path[i] == '[':
                if current:
                    parts.append(current)
                current = ""
                i += 1
                arr_content = ""
                while i < len(key_path) and key_path[i] != ']':
                    arr_content += key_path[i]
                    i += 1
                parts.append(f"[{arr_content}]")
                if i < len(key_path):
                    i += 1  # Skip ']'
            else:
                current += key_path[i]
                i += 1

        if current:
            parts.append(current)

        return parts

    @staticmethod
    def load(file_path: str) -> Tuple[Dict[str, Any], str]:
        """Load a config file and return (data, format)."""
        ext = Path(file_path).suffix.lower()

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if ext in ('.json',):
            data = json.loads(content)
            return data, 'json'
        elif ext in ('.yaml', '.yml'):
            data = yaml.safe_load(content) or {}
            return data, 'yaml'
        elif ext in ('.toml',):
            if not HAS_TOML:
                raise RuntimeError("TOML support requires tomli and tomli-w. Install with: pip install tomli tomli-w")
            data = tomli.loads(content)
            return data, 'toml'
        else:
            raise ValueError(f"Unsupported file format: {ext}")

    @staticmethod
    def save(file_path: str, data: Dict[str, Any], format_: str) -> None:
        """Save config data to a file in the specified format."""
        if format_ == 'json':
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write('\n')
        elif format_ in ('yaml', 'yml'):
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        elif format_ == 'toml':
            if not HAS_TOML:
                raise RuntimeError("TOML support requires tomli and tomli-w")
            with open(file_path, 'wb') as f:
                tomlkit.dump(data, f)
        else:
            raise ValueError(f"Unsupported format: {format_}")

    @staticmethod
    def deep_merge(base: Dict[str, Any], merge: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deep merge two dictionaries.
        Child values override parent values at all nesting levels.
        Child arrays completely replace parent arrays (no element-wise merge).
        """
        result = deepcopy(base)
        for key, value in merge.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = ConfigLoader.deep_merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result


class Matcher:
    """Match configuration key paths against patterns with array support."""

    @staticmethod
    def _parse_pattern_component(component: str) -> tuple:
        """Parse a pattern component (like 'services[*]', 'users[?active=true]'.
        Returns (base_name, matcher) where matcher is a function that checks
        if a value matches this component.
        """
        arr_match = re.match(r'^(.+)\[(.+)$', component)
        if not arr_match:
            # Simple component - literal match
            return component, lambda val: val is not None

        base_name = arr_match.group(1)
        arr_spec = arr_match.group(2)

        if arr_spec.endswith(']'):
            arr_spec = arr_spec[:-1]

        if arr_spec == '*':
            # Match all array elements
            return base_name, lambda val: isinstance(val, list)

        # Try to parse as index
        if arr_spec.isdigit():
            idx = int(arr_spec)
            return base_name, lambda val: isinstance(val, list) and len(val) > idx

        # Parse as filter: ?key=value or ?key1=val1&key2=val2
        if arr_spec.startswith('?'):
            filter_str = arr_spec[1:]
            conditions = []

            for cond in filter_str.split('&'):
                eq_match = re.match(r'^(\w+)=(.+)$', cond)
                if eq_match:
                    key = eq_match.group(1)
                    value_str = eq_match.group(2)

                    if value_str == 'true':
                        value = True
                    elif value_str == 'false':
                        value = False
                    elif value_str == 'null':
                        value = None
                    elif value_str.startswith('"') and value_str.endswith('"'):
                        value = value_str[1:-1]
                    elif '.' in value_str:
                        try:
                            value = float(value_str)
                        except ValueError:
                            value = value_str
                    else:
                        try:
                            value = int(value_str)
                        except ValueError:
                            value = value_str

                    conditions.append((key, value))

            def filter_matcher(val):
                if not isinstance(val, list):
                    return False
                for item in val:
                    if not isinstance(item, dict):
                        continue
                    matches_all = True
                    for cond_key, cond_val in conditions:
                        if cond_key not in item or item[cond_key] != cond_val:
                            matches_all = False
                            break
                    if matches_all:
                        return True
                return False

            return base_name, filter_matcher

        return base_name, lambda val: False

    @staticmethod
    def _parse_pattern(pattern: str) -> List[tuple]:
        """Parse a full pattern into a list of (base_name, matcher) tuples."""
        parts = pattern.split('.')
        parsed = []
        for part in parts:
            parsed.append(Matcher._parse_pattern_component(part))
        return parsed

    @staticmethod
    def match_pattern(pattern: str, key_path: str) -> Optional[Dict[str, Any]]:
        """Match a key path against a pattern."""
        pattern_parts = Matcher._parse_pattern(pattern)
        key_parts = key_path.split('.')

        if len(pattern_parts) != len(key_parts):
            return None

        variables = {}
        for (p_base, p_matcher), k_part in zip(pattern_parts, key_parts):
            if p_base.startswith('$'):
                var_name = p_base[1:]
                variables[var_name] = k_part
                if not p_matcher(k_part):
                    return None
            elif p_base == '*':
                pass
            else:
                arr_match = re.match(r'^(.+)\[(.+)$', k_part)
                if arr_match:
                    k_base = arr_match.group(1)
                    k_arr_spec = arr_match.group(2)
                    if k_arr_spec.endswith(']'):
                        k_arr_spec = k_arr_spec[:-1]
                    if p_base != k_base:
                        return None
                else:
                    if p_base != k_part:
                        return None

        return variables

    @staticmethod
    def find_matches(pattern: str, data: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any], List[Any]]]:
        """
        Find all keys in the data that match the pattern.
        Supports array patterns: array[*], array[0], array[?filter]

        Yields (full_key_path, variables, [parent, key]) tuples.
        """
        parsed_pattern = Matcher._parse_pattern(pattern)

        def _match_path(obj: Any, path_components: List[tuple], current_path: List[str],
                        parent_chain: List[Any]) -> Iterator[Tuple[str, Dict[str, Any], List[Any]]]:
            if not path_components:
                if current_path:
                    full_path = '.'.join(current_path)
                    yield (full_path, {}, parent_chain)
                return

            p_base, p_matcher = path_components[0]
            remaining = path_components[1:]

            if isinstance(obj, dict):
                for key, value in obj.items():
                    if p_base.startswith('$'):
                        if p_base != '*' and p_base != key:
                            continue
                        if not p_matcher(value):
                            continue
                        new_path = current_path + [key]
                        yield from _match_path(value, remaining, new_path, parent_chain + [(obj, key)])
                    elif p_base == '*':
                        new_path = current_path + [key]
                        yield from _match_path(value, remaining, new_path, parent_chain + [(obj, key)])
                    else:
                        arr_match = re.match(r'^(.+)\[(.+)$', key)
                        if arr_match:
                            key_base = arr_match.group(1)
                        else:
                            key_base = key

                        if p_base == key_base:
                            if p_matcher(value):
                                new_path = current_path + [key]
                                yield from _match_path(value, remaining, new_path, parent_chain + [(obj, key)])

            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    arr_key = f"{current_path[-1]}[{i}]" if current_path else f"[{i}]"
                    new_path = current_path[:-1] + [arr_key]

                    if p_matcher(obj):
                        yield from _match_path(item, remaining, new_path, parent_chain + [(obj, i)])

        yield from _match_path(data, parsed_pattern, [], [])


class Rule(ABC):
    """Abstract base class for transformation rules."""

    def __init__(self, name: str, rule_data: Dict[str, Any]):
        self.name = name
        self.rule_data = rule_data

    @staticmethod
    @abstractmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        pass

    @abstractmethod
    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Apply the rule to the config data."""
        pass


class ReplaceValueRule(Rule):
    """Rule for replacing a value at a specific key."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'key' not in rule_data:
            raise RuleValidationError("replace_value rule must have a 'key' field")
        if 'value' not in rule_data:
            raise RuleValidationError("replace_value rule must have a 'value' field")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        key = self.rule_data['key']
        new_value = self.rule_data['value']

        parts = ConfigLoader._split_key_path(key)
        parent = data
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                return updates

        last_key = parts[-1]
        if isinstance(parent, dict) and last_key in parent:
            parent[last_key] = new_value
            updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})

        return updates


class RenameKeyRule(Rule):
    """Rule for renaming a key."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'old_key' not in rule_data:
            raise RuleValidationError("rename_key rule must have an 'old_key' field")
        if 'new_key' not in rule_data:
            raise RuleValidationError("rename_key rule must have a 'new_key' field")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        old_key = self.rule_data['old_key']
        new_key = self.rule_data['new_key']

        old_parts = ConfigLoader._split_key_path(old_key)
        new_parts = ConfigLoader._split_key_path(new_key)

        parent = data
        for part in old_parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                return updates

        last_key = old_parts[-1]
        if isinstance(parent, dict) and last_key in parent:
            value = parent.pop(last_key)

            new_parent = data
            for part in new_parts[:-1]:
                if not isinstance(new_parent, dict):
                    new_parent = {}
                if part not in new_parent or not isinstance(new_parent[part], dict):
                    new_parent[part] = {}
                new_parent = new_parent[part]

            if isinstance(new_parent, dict):
                new_parent[new_parts[-1]] = value
                updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})

        return updates


class MergeDataRule(Rule):
    """Rule for merging data into the config."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'data' not in rule_data:
            raise RuleValidationError("merge_data rule must have a 'data' field")
        if not isinstance(rule_data['data'], dict):
            raise RuleValidationError("merge_data rule 'data' must be a dictionary")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        merge_data = self.rule_data['data']

        def deep_merge(target: Dict, source: Dict) -> bool:
            changed = False
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    if deep_merge(target[key], value):
                        changed = True
                else:
                    target[key] = deepcopy(value)
                    changed = True
            return changed

        if deep_merge(data, merge_data):
            updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})

        return updates


class PatternReplaceRule(Rule):
    """Rule for pattern-based replacement with array support."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'key_pattern' not in rule_data:
            raise RuleValidationError("pattern_replace rule must have a 'key_pattern' field")
        if 'value' not in rule_data:
            raise RuleValidationError("pattern_replace rule must have a 'value' field")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        pattern = self.rule_data['key_pattern']
        value_template = self.rule_data['value']

        for full_path, variables, parent_chain in Matcher.find_matches(pattern, data):
            parent = parent_chain[-2] if len(parent_chain) >= 2 else None
            key = parent_chain[-1] if parent_chain else None

            if isinstance(parent, dict) and key is not None:
                if isinstance(value_template, str):
                    final_value = self._substitute(value_template, variables)
                else:
                    final_value = value_template
                parent[key] = final_value

                if not updates:
                    updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})

        return updates

    def _substitute(self, template: str, variables: Dict[str, Any]) -> str:
        result = template
        for match in TEMPLATE_VAR_PATTERN.finditer(template):
            var_name = match.group(1)
            if var_name in variables:
                result = result.replace(match.group(0), str(variables[var_name]))
        return result


class TemplateStringRule(Rule):
    """Rule for building string values from templates."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'template' not in rule_data:
            raise RuleValidationError("template_string rule must have a 'template' field")
        if not isinstance(rule_data.get('template'), str):
            raise RuleValidationError("template_string rule 'template' must be a string")
        if 'variables' not in rule_data:
            raise RuleValidationError("template_string rule must have a 'variables' field")
        if not isinstance(rule_data['variables'], dict):
            raise RuleValidationError("template_string rule 'variables' must be a dictionary")
        for var_name, key_path in rule_data['variables'].items():
            if not isinstance(key_path, str):
                raise RuleValidationError(f"template_string rule variable '{var_name}' must have a string key path")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        key = self.rule_data['key']
        variables = self.rule_data['variables']
        template = self.rule_data['template']

        target_parts = ConfigLoader._split_key_path(key)
        target_parent = data
        for part in target_parts[:-1]:
            if isinstance(target_parent, dict) and part in target_parent:
                target_parent = target_parent[part]
            else:
                return updates

        last_key = target_parts[-1]
        if not isinstance(target_parent, dict) or last_key not in target_parent:
            return updates
        if not isinstance(target_parent[last_key], str):
            return updates

        extracted = {}
        for var_name, key_path in variables.items():
            value = ConfigLoader._get_nested_value(data, key_path)
            if value is None:
                return updates
            extracted[var_name] = str(value)

        final_value = self._substitute(template, extracted)
        target_parent[last_key] = final_value

        updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})
        return updates

    def _substitute(self, template: str, variables: Dict[str, Any]) -> str:
        result = template
        for match in TEMPLATE_VAR_PATTERN.finditer(template):
            var_name = match.group(1)
            if var_name in variables:
                result = result.replace(match.group(0), variables[var_name])
        return result


class ConditionalReplaceRule(Rule):
    """Rule for conditional replacement."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'key' not in rule_data:
            raise RuleValidationError("conditional_replace rule must have a 'key' field")
        if 'value' not in rule_data:
            raise RuleValidationError("conditional_replace rule must have a 'value' field")
        if 'condition' not in rule_data:
            raise RuleValidationError("conditional_replace rule must have a 'condition' field")

        condition = rule_data['condition']
        if not isinstance(condition, dict):
            raise RuleValidationError("conditional_replace rule 'condition' must be a dictionary")

        condition_types = ['current_value_equals', 'current_value_not_equals', 'current_value_matches']
        has_condition = any(ct in condition for ct in condition_types)
        if not has_condition:
            raise RuleValidationError("conditional_replace rule must have at least one condition type")

        if 'current_value_matches' in condition:
            pattern = condition['current_value_matches']
            if not isinstance(pattern, str):
                raise RuleValidationError("current_value_matches must be a string pattern")
            try:
                re.compile(pattern)
            except re.error as e:
                raise RuleValidationError(f"Invalid regex pattern in current_value_matches: {e}")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        updates = []
        key = self.rule_data['key']
        new_value = self.rule_data['value']
        condition = self.rule_data.get('condition', {})

        current_value = ConfigLoader._get_nested_value(data, key)
        if current_value is None:
            return updates

        if not self._check_condition(current_value, condition):
            return updates

        parts = ConfigLoader._split_key_path(key)
        parent = data
        for part in parts[:-1]:
            if isinstance(parent, dict) and part in parent:
                parent = parent[part]
            else:
                return updates

        last_key = parts[-1]
        if isinstance(parent, dict):
            parent[last_key] = new_value
            updates.append({'event': 'file_updated', 'file': '[applied]', 'rules_applied': [self.name]})

        return updates

    def _check_condition(self, current_value: Any, condition: Dict[str, Any]) -> bool:
        str_value = str(current_value)

        if 'current_value_equals' in condition:
            if str_value != str(condition['current_value_equals']):
                return False

        if 'current_value_not_equals' in condition:
            if str_value == str(condition['current_value_not_equals']):
                return False

        if 'current_value_matches' in condition:
            pattern = condition['current_value_matches']
            if not re.match(pattern, str_value):
                return False

        return True


# ============= VALIDATION RULES =============

class ValidateRule(Rule):
    """Base class for validation rules with scope='validation'."""

    def __init__(self, name: str, rule_data: Dict[str, Any], file_path: str = None):
        super().__init__(name, rule_data)
        self.file_path = file_path

    @abstractmethod
    def validate(self, data: Dict[str, Any]) -> List[str]:
        """Validate config data. Returns list of error messages."""
        pass

    @classmethod
    def validate_rule_data(cls, rule_data: Dict[str, Any]) -> None:
        """Validate rule configuration data. Override in subclasses."""
        pass


class RequireKeysRule(ValidateRule):
    """Rule for ensuring specific keys exist in the config."""

    @classmethod
    def validate_rule_data(cls, rule_data: Dict[str, Any]) -> None:
        if 'keys' not in rule_data:
            raise RuleValidationError("require_keys rule must have a 'keys' field")
        if not isinstance(rule_data['keys'], list):
            raise RuleValidationError("require_keys rule 'keys' must be a list")
        if not rule_data.get('scope') == 'validation':
            raise RuleValidationError("require_keys rule must have scope='validation'")

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors = []
        keys = self.rule_data['keys']

        for key in keys:
            value = ConfigLoader._get_nested_value(data, key)
            if value is None:
                errors.append(f"missing required key: {key}")

        return errors


class ValidateTypeRule(ValidateRule):
    """Rule for validating value types."""

    SUPPORTED_TYPES = {'string', 'number', 'boolean', 'array', 'object', 'null'}

    @classmethod
    def validate_rule_data(cls, rule_data: Dict[str, Any]) -> None:
        if 'key' not in rule_data:
            raise RuleValidationError("validate_type rule must have a 'key' field")
        if 'expected_type' not in rule_data:
            raise RuleValidationError("validate_type rule must have an 'expected_type' field")
        if rule_data.get('expected_type') not in ValidateTypeRule.SUPPORTED_TYPES:
            raise RuleValidationError(f"validate_type rule 'expected_type' must be one of: {ValidateTypeRule.SUPPORTED_TYPES}")
        if not rule_data.get('scope') == 'validation':
            raise RuleValidationError("validate_type rule must have scope='validation'")

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors = []
        key = self.rule_data['key']
        expected_type = self.rule_data['expected_type']

        # Get all matching values (including array indices for patterns like services[*].port)
        matches = self._find_all_key_matches(data, key)

        for match_key, value in matches:
            if not self._check_type(value, expected_type):
                actual_type = self._get_type_name(value)
                errors.append(f"expected type {expected_type} at key '{match_key}', got {actual_type}")

        return errors

    def _find_all_key_matches(self, data: Dict[str, Any], key_pattern: str) -> List[Tuple[str, Any]]:
        """Find all keys matching the pattern, including array indices."""
        matches = []

        # Check if it's an array pattern like services[*].port
        if '[*]' in key_pattern:
            # Use the Matcher to find matches
            base_path, suffix = key_pattern.split('[*]', 1)
            if suffix.startswith('.'):
                suffix = suffix[1:]

            # Find the array in data
            array_value = ConfigLoader._get_nested_value(data, base_path)
            if isinstance(array_value, list):
                for i, item in enumerate(array_value):
                    if isinstance(item, dict):
                        # Build the full key with index
                        full_key = f"{base_path}[{i}]"
                        if suffix:
                            full_key = f"{full_key}.{suffix}"
                        value = ConfigLoader._get_nested_value(data, full_key)
                        if value is not None:
                            matches.append((full_key, value))
            return matches

        # Simple key
        value = ConfigLoader._get_nested_value(data, key_pattern)
        if value is not None:
            matches.append((key_pattern, value))

        return matches

    def _check_type(self, value: Any, expected_type: str) -> bool:
        """Check if value matches expected type."""
        if expected_type == 'string':
            return isinstance(value, str)
        elif expected_type == 'number':
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        elif expected_type == 'boolean':
            return isinstance(value, bool)
        elif expected_type == 'array':
            return isinstance(value, list)
        elif expected_type == 'object':
            return isinstance(value, dict)
        elif expected_type == 'null':
            return value is None
        return False

    def _get_type_name(self, value: Any) -> str:
        """Get the type name for error messages."""
        if value is None:
            return "null"
        elif isinstance(value, bool):
            return "boolean"
        elif isinstance(value, (int, float)):
            return "number"
        elif isinstance(value, str):
            return "string"
        elif isinstance(value, list):
            return "array"
        elif isinstance(value, dict):
            return "object"
        return type(value).__name__


class ValidateValueRule(ValidateRule):
    """Rule for validating value constraints."""

    @classmethod
    def validate_rule_data(cls, rule_data: Dict[str, Any]) -> None:
        if 'key' not in rule_data:
            raise RuleValidationError("validate_value rule must have a 'key' field")
        if 'constraint' not in rule_data:
            raise RuleValidationError("validate_value rule must have a 'constraint' field")
        if not isinstance(rule_data.get('constraint'), dict):
            raise RuleValidationError("validate_value rule 'constraint' must be a dictionary")
        if not rule_data.get('scope') == 'validation':
            raise RuleValidationError("validate_value rule must have scope='validation'")

        # Validate constraint types
        constraint = rule_data.get('constraint', {})
        valid_constraint_keys = {'enum', 'min', 'max', 'pattern', 'length_min', 'length_max'}
        has_valid_constraint = any(k in constraint for k in valid_constraint_keys)
        if not has_valid_constraint:
            raise RuleValidationError("validate_value rule 'constraint' must have at least one valid constraint type")

        # Validate individual constraint values
        if 'enum' in constraint:
            if not isinstance(constraint['enum'], list):
                raise RuleValidationError("validate_value rule 'enum' constraint must be a list")
        if 'min' in constraint:
            if not isinstance(constraint['min'], (int, float)):
                raise RuleValidationError("validate_value rule 'min' constraint must be a number")
        if 'max' in constraint:
            if not isinstance(constraint['max'], (int, float)):
                raise RuleValidationError("validate_value rule 'max' constraint must be a number")
        if 'pattern' in constraint:
            if not isinstance(constraint['pattern'], str):
                raise RuleValidationError("validate_value rule 'pattern' constraint must be a string")
            try:
                re.compile(constraint['pattern'])
            except re.error as e:
                raise RuleValidationError(f"Invalid regex pattern in validate_value: {e}")
        if 'length_min' in constraint:
            if not isinstance(constraint['length_min'], int) or constraint['length_min'] < 0:
                raise RuleValidationError("validate_value rule 'length_min' constraint must be a non-negative integer")
        if 'length_max' in constraint:
            if not isinstance(constraint['length_max'], int) or constraint['length_max'] < 0:
                raise RuleValidationError("validate_value rule 'length_max' constraint must be a non-negative integer")

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors = []
        key = self.rule_data['key']
        constraint = self.rule_data.get('constraint', {})

        # Get all matching values
        matches = self._find_all_key_matches(data, key)

        for match_key, value in matches:
            # Check enum
            if 'enum' in constraint and value not in constraint['enum']:
                errors.append(f"value '{value}' at key '{match_key}' not in allowed values: {constraint['enum']}")

            # Check min/max (only for numbers)
            if isinstance(value, (int, float)):
                if 'min' in constraint and value < constraint['min']:
                    errors.append(f"value {value} at key '{match_key}' is less than min: {constraint['min']}")
                if 'max' in constraint and value > constraint['max']:
                    errors.append(f"value {value} at key '{match_key}' exceeds max: {constraint['max']}")

            # Check pattern (converts value to string)
            if 'pattern' in constraint:
                str_value = str(value)
                if not re.match(constraint['pattern'], str_value):
                    errors.append(f"value '{value}' at key '{match_key}' does not match pattern: {constraint['pattern']}")

            # Check length constraints
            if 'length_min' in constraint or 'length_max' in constraint:
                if isinstance(value, (str, list)):
                    length = len(value)
                    if 'length_min' in constraint and length < constraint['length_min']:
                        errors.append(f"value at key '{match_key}' length {length} is less than min length: {constraint['length_min']}")
                    if 'length_max' in constraint and length > constraint['length_max']:
                        errors.append(f"value at key '{match_key}' length {length} exceeds max length: {constraint['length_max']}")

        return errors

    def _find_all_key_matches(self, data: Dict[str, Any], key_pattern: str) -> List[Tuple[str, Any]]:
        """Find all keys matching the pattern, including array indices."""
        matches = []

        # Check if it's an array pattern like services[*].port
        if '[*]' in key_pattern:
            base_path, suffix = key_pattern.split('[*]', 1)
            if suffix.startswith('.'):
                suffix = suffix[1:]

            array_value = ConfigLoader._get_nested_value(data, base_path)
            if isinstance(array_value, list):
                for i, item in enumerate(array_value):
                    if isinstance(item, dict):
                        full_key = f"{base_path}[{i}]"
                        if suffix:
                            full_key = f"{full_key}.{suffix}"
                        value = ConfigLoader._get_nested_value(data, full_key)
                        if value is not None:
                            matches.append((full_key, value))
            return matches

        # Simple key
        value = ConfigLoader._get_nested_value(data, key_pattern)
        if value is not None:
            matches.append((key_pattern, value))

        return matches


class UniqueArrayValuesRule(ValidateRule):
    """Rule for ensuring array elements have unique values for a specific field."""

    @classmethod
    def validate_rule_data(cls, rule_data: Dict[str, Any]) -> None:
        if 'array_path' not in rule_data:
            raise RuleValidationError("unique_array_values rule must have an 'array_path' field")
        if 'field' not in rule_data:
            raise RuleValidationError("unique_array_values rule must have a 'field' field")
        if not rule_data.get('scope') == 'validation':
            raise RuleValidationError("unique_array_values rule must have scope='validation'")

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors = []
        array_path = self.rule_data['array_path']
        field = self.rule_data['field']

        array_value = ConfigLoader._get_nested_value(data, array_path)
        if not isinstance(array_value, list) or len(array_value) == 0:
            return errors  # Skip if array doesn't exist or is empty

        seen = {}
        for i, item in enumerate(array_value):
            if isinstance(item, dict):
                field_value = item.get(field)
                if field_value is not None:
                    if field_value in seen:
                        errors.append(f"duplicate values for field '{field}' in array at '{array_path}': {field_value}")
                    else:
                        seen[field_value] = i

        return errors


# ============= FILE RELOCATION RULES =============

class FileRelocationRule(Rule):
    """Base class for file relocation rules with scope='file'."""

    def __init__(self, name: str, rule_data: Dict[str, Any], target_dir: Path):
        super().__init__(name, rule_data)
        self.target_dir = target_dir

    @staticmethod
    def extract_path_variables(path_pattern: str) -> List[str]:
        """Extract variable names from a path pattern like services/$name/config.yaml."""
        variables = []
        for match in PATH_VAR_PATTERN.finditer(path_pattern):
            variables.append(match.group(1))
        return variables

    def match_path_pattern(self, file_path: Path) -> Optional[Dict[str, str]]:
        """Match a file path against a path pattern. Returns captured variables or None."""
        try:
            relative = file_path.relative_to(self.target_dir)
        except ValueError:
            return None

        pattern_parts = self.rule_data.get('path_pattern', '').split('/')
        path_parts = list(relative.parts)

        if len(pattern_parts) != len(path_parts):
            return None

        variables = {}
        for p_part, f_part in zip(pattern_parts, path_parts):
            var_match = re.match(r'^\$(\w+)$', p_part)
            if var_match:
                variables[var_match.group(1)] = f_part
            elif p_part != '*' and p_part != f_part:
                return None

        return variables


class PathRenameRule(FileRelocationRule):
    """Rename files based on path pattern matching."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'path_pattern' not in rule_data:
            raise RuleValidationError("path_rename rule must have a 'path_pattern' field")
        if 'new_path' not in rule_data:
            raise RuleValidationError("path_rename rule must have a 'new_path' field")
        if rule_data.get('scope') != 'file':
            raise RuleValidationError("path_rename rule must have scope='file'")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Returns a relocation spec instead of modifying data."""
        variables = self.match_path_pattern(Path(data.get('__file__', '.')))
        if variables is None:
            return []

        new_path_template = self.rule_data['new_path']
        new_path = substitute_template(new_path_template, variables)

        return [{
            'type': 'file_move',
            'from': str(Path(data.get('__file__', '.'))),
            'to': new_path,
            'rule_name': self.name
        }]


class ContentRenameRule(FileRelocationRule):
    """Rename files based on content variables."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'file_pattern' not in rule_data:
            raise RuleValidationError("content_rename rule must have a 'file_pattern' field")
        if 'variables' not in rule_data:
            raise RuleValidationError("content_rename rule must have a 'variables' field")
        if 'new_path' not in rule_data:
            raise RuleValidationError("content_rename rule must have a 'new_path' field")
        if rule_data.get('scope') != 'file':
            raise RuleValidationError("content_rename rule must have scope='file'")
        if not isinstance(rule_data['variables'], dict):
            raise RuleValidationError("content_rename rule 'variables' must be a dictionary")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check if file matches pattern and extract variables from content."""
        file_path = Path(data.get('__file__', '.'))

        # First check if file matches the file_pattern glob
        if not self.matches_file_pattern(file_path):
            return []

        # Extract variables from content
        variables = {}
        variables_spec = self.rule_data['variables']
        for var_name, key_path in variables_spec.items():
            value = ConfigLoader._get_nested_value(data, key_path)
            if value is None:
                return []  # Skip if any variable is missing
            variables[var_name] = str(value)

        new_path_template = self.rule_data['new_path']
        new_path = substitute_template(new_path_template, variables)

        return [{
            'type': 'file_move',
            'from': str(file_path),
            'to': new_path,
            'rule_name': self.name
        }]

    def matches_file_pattern(self, file_path: Path) -> bool:
        """Check if file matches the file_pattern using glob syntax."""
        try:
            relative = file_path.relative_to(self.target_dir)
        except ValueError:
            return False

        pattern = self.rule_data['file_pattern']

        # Convert glob pattern to regex
        # Handle ** for recursive matching
        regex_pattern = re.escape(pattern)
        regex_pattern = regex_pattern.replace(r'\*', '*')
        regex_pattern = regex_pattern.replace(r'\*', '.*')
        regex_pattern = regex_pattern.replace(r'\?', '.')

        # Anchor the pattern
        regex_pattern = '^' + regex_pattern + '$'

        # Convert path separators for regex
        relative_str = str(relative).replace('\\', '/')

        return bool(re.match(regex_pattern, relative_str))


class RelocateFilesRule(FileRelocationRule):
    """Move multiple files matching a pattern to a new directory."""

    @staticmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        if 'source_pattern' not in rule_data:
            raise RuleValidationError("relocate_files rule must have a 'source_pattern' field")
        if 'destination' not in rule_data:
            raise RuleValidationError("relocate_files rule must have a 'destination' field")
        if rule_data.get('scope') != 'file':
            raise RuleValidationError("relocate_files rule must have scope='file'")

    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Returns relocations for all files matching the source pattern."""
        file_path = Path(data.get('__file__', '.'))

        # Check if this file matches the source pattern
        variables = self.match_source_pattern(file_path)
        if variables is None:
            return []

        dest_template = self.rule_data['destination']
        dest_dir = substitute_template(dest_template, variables)

        # Get just the filename from the source
        try:
            relative = file_path.relative_to(self.target_dir)
            filename = relative.name
        except ValueError:
            filename = file_path.name

        new_path = str(Path(dest_dir) / filename)

        return [{
            'type': 'file_move',
            'from': str(file_path),
            'to': new_path,
            'rule_name': self.name
        }]

    def match_source_pattern(self, file_path: Path) -> Optional[Dict[str, str]]:
        """Match a file against the source pattern."""
        try:
            relative = file_path.relative_to(self.target_dir)
        except ValueError:
            return None

        pattern = self.rule_data['source_pattern']
        pattern_parts = pattern.split('/')
        path_parts = list(relative.parts)

        # Handle patterns like "environments/$env/*.yaml"
        if len(pattern_parts) != len(path_parts):
            # Check if last part is a wildcard
            if '*' in pattern_parts[-1]:
                # Compare all but last part
                if len(pattern_parts) - 1 != len(path_parts) - 1:
                    return None
                # Pattern parts match except last
                vars = {}
                for i in range(len(pattern_parts) - 1):
                    p_part = pattern_parts[i]
                    f_part = path_parts[i]
                    var_match = re.match(r'^\$(\w+)$', p_part)
                    if var_match:
                        vars[var_match.group(1)] = f_part
                    elif p_part != '*' and p_part != f_part:
                        return None
                # Check filename matches wildcard
                filename_pattern = pattern_parts[-1]
                filename = path_parts[-1]
                if not self.matches_wildcard(filename_pattern, filename):
                    return None
                return vars
            return None

        # Exact part matching
        variables = {}
        for p_part, f_part in zip(pattern_parts, path_parts):
            var_match = re.match(r'^\$(\w+)$', p_part)
            if var_match:
                variables[var_match.group(1)] = f_part
            elif p_part != '*' and p_part != f_part:
                return None

        return variables

    @staticmethod
    def matches_wildcard(pattern: str, filename: str) -> bool:
        """Check if filename matches a wildcard pattern."""
        # Convert to regex
        regex = re.escape(pattern)
        regex = regex.replace(r'\*', '.*')
        regex = regex.replace(r'\?', '.')
        regex = '^' + regex + '$'
        return bool(re.match(regex, filename))


class RuleFactory:
    """Factory for creating rule instances."""

    RULE_TYPES = {
        'replace_value': ReplaceValueRule,
        'rename_key': RenameKeyRule,
        'merge_data': MergeDataRule,
        'pattern_replace': PatternReplaceRule,
        'template_string': TemplateStringRule,
        'conditional_replace': ConditionalReplaceRule,
        # Validation rules (scope='validation')
        'require_keys': RequireKeysRule,
        'validate_type': ValidateTypeRule,
        'validate_value': ValidateValueRule,
        'unique_array_values': UniqueArrayValuesRule,
        # File relocation rules (scope='file')
        'path_rename': PathRenameRule,
        'content_rename': ContentRenameRule,
        'relocate_files': RelocateFilesRule,
    }

    @classmethod
    def create(cls, name: str, rule_data: Dict[str, Any]) -> Rule:
        rule_type = rule_data.get('type')
        if rule_type not in cls.RULE_TYPES:
            raise RuleValidationError(f"Unknown rule type: {rule_type}")
        return cls.RULE_TYPES[rule_type](name, rule_data)

    @classmethod
    def validate_rule(cls, name: str, rule_data: Dict[str, Any]) -> None:
        if 'type' not in rule_data:
            raise RuleValidationError(f"Rule '{name}' must have a 'type' field")

        rule_type = rule_data.get('type')
        if rule_type not in cls.RULE_TYPES:
            raise RuleValidationError(f"Rule '{name}' has unknown type: {rule_type}")

        # For validation rules, use validate_rule_data classmethod
        # For other rules, use the static validate method
        rule_class = cls.RULE_TYPES[rule_type]
        if hasattr(rule_class, 'validate_rule_data'):
            rule_class.validate_rule_data(rule_data)
        else:
            rule_class.validate(rule_data)


# Inheritance resolution
def resolve_inheritance(
    file_path: Path,
    inheritance_config: Dict[str, Dict[str, Any]],
    visited: Set[Path] = None
) -> Tuple[Dict[str, Any], str, bool]:
    """
    Resolve inheritance for a config file.
    Returns: (data, format_, was_modified)
    """
    if visited is None:
        visited = set()

    if file_path in visited:
        cycle = " -> ".join(str(p) for p in visited) + f" -> {file_path}"
        print(f"Error: circular dependency detected: {cycle}", file=sys.stderr)
        sys.exit(1)

    visited = visited | {file_path}

    try:
        data, fmt = ConfigLoader.load(str(file_path))
    except Exception as e:
        print(f"Error: failed to parse parent config: {file_path}", file=sys.stderr)
        sys.exit(1)

    was_modified = False
    ext = file_path.suffix.lower()

    if ext in inheritance_config:
        inherit_spec = inheritance_config[ext]
        if isinstance(inherit_spec, dict) and 'pattern' in inherit_spec:
            pattern = inherit_spec['pattern']
            parts = pattern.split('.')

            current = data
            for part in parts[:-1]:
                if isinstance(current, dict) and part in current:
                    current = current[part]
                else:
                    current = None
                    break

            if current is not None and isinstance(current, dict):
                last_key = parts[-1]
                if last_key in current:
                    parent_path_str = current[last_key]
                    if isinstance(parent_path_str, str):
                        parent_path = (file_path.parent / parent_path_str).resolve()

                        if parent_path.exists():
                            parent_data, parent_fmt, _ = resolve_inheritance(
                                parent_path, inheritance_config, visited
                            )
                            original_data = deepcopy(data)
                            data = ConfigLoader.deep_merge(parent_data, data)
                            was_modified = (data != original_data)
                        else:
                            print(f"Warning: parent config not found: {parent_path}, skipping inheritance", file=sys.stderr)

                        # Remove the inheritance directive key and clean up empty parents
                        current_cleanup = data
                        for part in parts[:-1]:
                            if isinstance(current_cleanup, dict) and part in current_cleanup:
                                current_cleanup = current_cleanup[part]
                            else:
                                break
                        else:
                            if isinstance(current_cleanup, dict) and last_key in current_cleanup:
                                del current_cleanup[last_key]
                                was_modified = True

                            # Clean up empty parent objects
                            parent_chain = parts[:-1]
                            if parent_chain:
                                current_check = data
                                for p in parent_chain:
                                    if isinstance(current_check, dict) and p in current_check:
                                        current_check = current_check[p]
                                if isinstance(current_check, dict) and len(current_check) == 0:
                                    current_cleanup = data
                                    for p in parent_chain[:-1]:
                                        if isinstance(current_cleanup, dict) and p in current_cleanup:
                                            current_cleanup = current_cleanup[p]
                                    if isinstance(current_cleanup, dict) and parent_chain[-1] in current_cleanup:
                                        del current_cleanup[parent_chain[-1]]
                                        was_modified = True

    return data, fmt, was_modified


def process_file(file_path: str, rules: Dict[str, Dict[str, Any]],
                 inheritance_config: Optional[Dict[str, Dict[str, Any]]] = None,
                 dry_run: bool = False) -> List[Dict[str, Any]]:
    """Process a single config file with optional inheritance."""
    try:
        if inheritance_config:
            resolved_data, fmt, was_modified = resolve_inheritance(
                Path(file_path).resolve(), inheritance_config
            )
        else:
            resolved_data, fmt = ConfigLoader.load(file_path)
            was_modified = False
    except Exception as e:
        print(f"Error loading {file_path}: {e}", file=sys.stderr)
        return []

    events = apply_rules_to_data(resolved_data, rules)

    should_save = (events or was_modified) and not dry_run

    if should_save:
        try:
            ConfigLoader.save(file_path, resolved_data, fmt)
        except Exception as e:
            print(f"Error saving {file_path}: {e}", file=sys.stderr)
            return []

    for event in events:
        event['file'] = file_path

    if was_modified and not events:
        events.append({
            'event': 'file_updated',
            'file': file_path,
            'rules_applied': []
        })

    return events


def load_inheritance_config(inheritance_path: str) -> Dict[str, Dict[str, Any]]:
    """Load inheritance configuration from JSON file."""
    try:
        with open(inheritance_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: inheritance config file not found: {inheritance_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in inheritance config: {inheritance_path}", file=sys.stderr)
        sys.exit(1)


def load_rules(rules_path: str) -> Dict[str, Dict[str, Any]]:
    """Load rules from a JSON file."""
    with open(rules_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_all_rules(rules: Dict[str, Dict[str, Any]]) -> None:
    """Validate all rules in the rules dict."""
    for name, rule_data in rules.items():
        try:
            RuleFactory.validate_rule(name, rule_data)
        except RuleValidationError as e:
            print(f"Rule validation error: Rule '{name}': {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            # Handle case where old-style static validate method was used
            if "missing 1 required positional argument" in str(e):
                # This means we need to fix the rule class - for backward compatibility,
                # we'll call validate_rule_data as classmethod
                rule_type = rule_data.get('type')
                if rule_type in RuleFactory.RULE_TYPES:
                    rule_class = RuleFactory.RULE_TYPES[rule_type]
                    if hasattr(rule_class, 'validate_rule_data'):
                        try:
                            rule_class.validate_rule_data(rule_data)
                        except RuleValidationError as e2:
                            print(f"Rule validation error: Rule '{name}': {e2}", file=sys.stderr)
                            sys.exit(1)
            raise


def run_validation(
    file_path: Path,
    rules: Dict[str, Dict[str, Any]],
    inheritance_config: Optional[Dict[str, Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Run validation rules on a config file. Returns list of validation_failed events."""
    errors = []

    try:
        # Load data with inheritance resolution (same as transformations)
        if inheritance_config:
            data, fmt, _ = resolve_inheritance(file_path.resolve(), inheritance_config)
        else:
            data, fmt = ConfigLoader.load(str(file_path))
    except Exception as e:
        return []

    # Get validation rules (those with scope='validation') sorted by name
    validation_rules = {n: r for n, r in sorted(rules.items()) if r.get('scope') == 'validation'}

    for rule_name, rule_data in validation_rules.items():
        try:
            RuleFactory.validate_rule(rule_name, rule_data)
            rule = RuleFactory.create(rule_name, rule_data)
            # Call the validate method on the rule instance
            if hasattr(rule, 'validate') and callable(rule.validate):
                rule_errors = rule.validate(data)
                for error in rule_errors:
                    errors.append({
                        'event': 'validation_failed',
                        'file': str(file_path),
                        'rule': rule_name,
                        'error': error
                    })
        except Exception:
            continue

    return errors


def apply_rules_to_data(data: Dict[str, Any], rules: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply all transformation rules to config data and return list of update events."""
    events = []

    # Only apply rules that are NOT validation scope
    for name in sorted(rules.keys()):
        rule_data = rules[name]
        if rule_data.get('scope') == 'validation':
            continue  # Skip validation rules - they're handled in run_validation()
        rule = RuleFactory.create(name, rule_data)
        events.extend(rule.apply(data))

    return events


def find_config_files(target_dir: Path) -> List[Path]:
    """Find all config files in target directory recursively."""
    extensions = {'.json', '.yaml', '.yml', '.toml', '.ini'}
    files = []
    for ext in extensions:
        files.extend(target_dir.rglob(f'*{ext}'))
    return sorted(files)


def apply_content_transformation_rules(
    file_path: Path,
    rules: Dict[str, Dict[str, Any]],
    inheritance_config: Optional[Dict[str, Dict[str, Any]]],
    target_dir: Path,
    dry_run: bool
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """Apply content transformation rules to a file. Returns (events, modified_data, format)."""
    try:
        if inheritance_config:
            data, fmt, _ = resolve_inheritance(file_path.resolve(), inheritance_config)
        else:
            data, fmt = ConfigLoader.load(str(file_path))
    except Exception as e:
        print(f"Error loading {file_path}: {e}", file=sys.stderr)
        return [], None, ''

    original_data = deepcopy(data)

    # Apply all content transformation rules (those without scope='file')
    content_rules = {n: r for n, r in rules.items() if r.get('scope') != 'file'}
    events = apply_rules_to_data(data, content_rules)

    if data != original_data and not dry_run:
        try:
            ConfigLoader.save(str(file_path), data, fmt)
        except Exception as e:
            print(f"Error saving {file_path}: {e}", file=sys.stderr)
            return [], None, ''

    for event in events:
        event['file'] = str(file_path.relative_to(target_dir))

    return events, data if data != original_data else None, fmt


def find_file_relocation_moves(
    all_files: List[Path],
    rules: Dict[str, Dict[str, Any]],
    target_dir: Path
) -> List[Dict[str, Any]]:
    """Find all file relocation moves without applying them."""
    moves = []

    # Get file relocation rules (scope='file') sorted by name
    file_rules = {n: r for n, r in sorted(rules.items()) if r.get('scope') == 'file'}

    for file_path in all_files:
        # Load file data for content-based rules
        try:
            data, _ = ConfigLoader.load(str(file_path))
            data['__file__'] = str(file_path)  # Add for rule use
        except Exception:
            continue

        # Apply each file relocation rule
        for rule_name, rule_data in file_rules.items():
            try:
                RuleFactory.validate_rule(rule_name, rule_data)
                rule = RuleFactory.create(rule_name, rule_data)
                # FileRelocationRule subclasses need target_dir
                if isinstance(rule, FileRelocationRule):
                    rule = RuleFactory.create(rule_name, rule_data)
                    # Re-create with target_dir
                    rule_class = type(rule)
                    rule = rule_class(rule_name, rule_data, target_dir)

                result = rule.apply(data)
                moves.extend(result)
            except Exception:
                continue

    return moves


def validate_relocation_collisions(moves: List[Dict[str, Any]]) -> Optional[str]:
    """Check for destination collisions. Returns error message if found."""
    dest_to_sources = {}
    for move in moves:
        dest = move['to']
        if dest in dest_to_sources:
            dest_to_sources[dest].append(move['from'])
        else:
            dest_to_sources[dest] = [move['from']]

    for dest, sources in dest_to_sources.items():
        if len(sources) > 1:
            return f"Error: destination already exists: {dest}"

    return None


def apply_file_relocations(
    moves: List[Dict[str, Any]],
    target_dir: Path,
    dry_run: bool
) -> List[Dict[str, Any]]:
    """Apply file relocations and return file_relocated events."""
    events = []

    for move in moves:
        from_path = Path(move['from'])
        to_path = Path(move['to'])

        # Make paths absolute relative to target_dir
        if not from_path.is_absolute():
            from_path = target_dir / from_path
        if not to_path.is_absolute():
            to_path = target_dir / to_path

        event = {
            'event': 'file_relocated',
            'from': str(Path(move['from'])),
            'to': str(Path(move['to']))
        }
        events.append(event)

        if not dry_run:
            # Create destination directory
            to_path.parent.mkdir(parents=True, exist_ok=True)

            # Move the file
            try:
                import shutil
                shutil.move(str(from_path), str(to_path))
            except Exception as e:
                print(f"Error moving file: {e}", file=sys.stderr)

    return events


def update_inheritance_references(
    all_files: List[Path],
    moves: List[Dict[str, Any]],
    inheritance_config: Optional[Dict[str, Dict[str, Any]]],
    target_dir: Path
) -> List[Dict[str, Any]]:
    """Update inheritance references after file moves."""
    if not inheritance_config:
        return []

    events = []

    # Build a map of old to new paths
    old_to_new = {m['from']: m['to'] for m in moves}

    # Find which files were moved
    moved_files = {m['from'] for m in moves}

    # Get inheritance pattern
    inheritance_pattern = None
    for ext, spec in inheritance_config.items():
        if isinstance(spec, dict) and 'pattern' in spec:
            inheritance_pattern = spec['pattern']
            break

    if not inheritance_pattern:
        return []

    # Get the key path for inheritance
    key_parts = inheritance_pattern.split('.')
    inherit_key = key_parts[-1]

    for file_path in all_files:
        rel_path = str(file_path.relative_to(target_dir))

        # Skip if this file was moved
        if rel_path in moved_files:
            # For moved files, we need to update the extends path to be relative to new location
            try:
                data, fmt = ConfigLoader.load(str(file_path))

                # Find and update inheritance reference
                parent, last_key, value = ConfigLoader._get_nested_with_parent(data, inherit_key)
                if parent is not None and last_key is not None and isinstance(value, str):
                    # Check if this value is a path that was moved
                    old_ext_path = value
                    if old_ext_path in old_to_new:
                        new_ext_path = old_to_new[old_ext_path]

                        # Calculate relative path from new file location to new extendee location
                        file_dir = file_path.parent
                        new_ext_path_full = target_dir / new_ext_path
                        relative = get_relative_path(file_dir, new_ext_path_full)

                        parent[last_key] = relative

                        # Save if changed
                        ConfigLoader.save(str(file_path), data, fmt)

                        events.append({
                            'event': 'file_updated',
                            'file': rel_path,
                            'rules_applied': [],
                            'reason': 'inheritance_reference_update'
                        })
            except Exception:
                pass
        else:
            # For files that weren't moved but reference moved files
            try:
                data, fmt = ConfigLoader.load(str(file_path))

                parent, last_key, value = ConfigLoader._get_nested_with_parent(data, inherit_key)
                if parent is not None and last_key is not None and isinstance(value, str):
                    if value in old_to_new:
                        parent[last_key] = old_to_new[value]

                        ConfigLoader.save(str(file_path), data, fmt)

                        events.append({
                            'event': 'file_updated',
                            'file': rel_path,
                            'rules_applied': [],
                            'reason': 'inheritance_reference_update'
                        })
            except Exception:
                pass

    return events


def get_relative_path(from_dir: Path, to_path: Path) -> str:
    """Get relative path from from_dir to to_path."""
    try:
        return str(to_path.relative_to(from_dir))
    except ValueError:
        # Need to use os.path.relpath
        import os
        return os.path.relpath(to_path, from_dir)


def apply_content_transformation_rules(
    file_path: Path,
    rules: Dict[str, Dict[str, Any]],
    inheritance_config: Optional[Dict[str, Dict[str, Any]]],
    target_dir: Path,
    dry_run: bool
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], str]:
    """Apply content transformation rules to a file. Returns (events, modified_data, format)."""
    try:
        if inheritance_config:
            data, fmt, _ = resolve_inheritance(file_path.resolve(), inheritance_config)
        else:
            data, fmt = ConfigLoader.load(str(file_path))
    except Exception as e:
        print(f"Error loading {file_path}: {e}", file=sys.stderr)
        return [], None, ''

    original_data = deepcopy(data)

    # Apply all content transformation rules (those without scope='file' or 'validation')
    content_rules = {n: r for n, r in rules.items() if r.get('scope') not in ('file', 'validation')}
    events = apply_rules_to_data(data, content_rules)

    if data != original_data and not dry_run:
        try:
            ConfigLoader.save(str(file_path), data, fmt)
        except Exception as e:
            print(f"Error saving {file_path}: {e}", file=sys.stderr)
            return [], None, ''

    for event in events:
        event['file'] = str(file_path.relative_to(target_dir))

    return events, data if data != original_data else None, fmt


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Apply transformation rules to configuration files with file relocation.'
    )
    parser.add_argument(
        'rules_file',
        help='Path to the rules.json file'
    )
    parser.add_argument(
        'target_dir',
        help='Target directory containing config files'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without saving'
    )
    parser.add_argument(
        '--inheritance',
        help='Path to inheritance config JSON file'
    )

    args = parser.parse_args()

    target_dir = Path(args.target_dir)

    # Validate target directory
    if not target_dir.exists():
        print(f"Error: target directory not found: {target_dir}", file=sys.stderr)
        sys.exit(1)
    if not target_dir.is_dir():
        print(f"Error: target is not a directory: {target_dir}", file=sys.stderr)
        sys.exit(1)

    # Load inheritance config if provided
    inheritance_config = None
    if args.inheritance:
        try:
            inheritance_config = load_inheritance_config(args.inheritance)
        except Exception as e:
            print(f"Error: invalid inheritance config: {args.inheritance}", file=sys.stderr)
            sys.exit(1)

    # Load and validate rules
    try:
        rules = load_rules(args.rules_file)
    except Exception as e:
        print(f"Error loading rules: {e}", file=sys.stderr)
        sys.exit(1)

    validate_all_rules(rules)

    # Find all config files
    all_files = find_config_files(target_dir)

    # ============= Phase 1: Inheritance Resolution (if applicable) =============
    # Note: Inheritance resolution happens inline within each phase as needed.

    # ============= Phase 2: Validation (NEW) =============
    # Run validation rules against merged configs (after inheritance)
    # Collect ALL validation errors and exit with code 1 if any fail
    all_validation_errors = []
    for file_path in all_files:
        errors = run_validation(file_path, rules, inheritance_config)
        all_validation_errors.extend(errors)

    # If any validation failed, output all validation_failed events and exit with code 1
    # Do NOT modify any files or output other events
    if all_validation_errors:
        for error in all_validation_errors:
            print(json.dumps(error))
        sys.exit(1)

    # ============= Phase 3: Content Transformation Rules =============
    content_events = []
    for file_path in all_files:
        events, _, _ = apply_content_transformation_rules(
            file_path, rules, inheritance_config, target_dir, args.dry_run
        )
        content_events.extend(events)

    # ============= Phase 4: File Relocation Rules =============
    relocation_events = []
    inheritance_update_events = []

    if not args.dry_run:
        # Find all potential moves
        all_moves = find_file_relocation_moves(all_files, rules, target_dir)

        # Validate for collisions
        collision_error = validate_relocation_collisions(all_moves)
        if collision_error:
            print(collision_error, file=sys.stderr)
            sys.exit(1)

        # Apply file relocations
        relocation_events = apply_file_relocations(all_moves, target_dir, args.dry_run)

        # Update inheritance references
        inheritance_update_events = update_inheritance_references(
            all_files, all_moves, inheritance_config, target_dir
        )

    # Output all events in correct order
    for event in content_events:
        print(json.dumps(event))
    for event in relocation_events:
        print(json.dumps(event))
    for event in inheritance_update_events:
        print(json.dumps(event))


if __name__ == '__main__':
    main()


if __name__ == '__main__':
    main()
