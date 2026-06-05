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


class RuleFactory:
    """Factory for creating rule instances."""

    RULE_TYPES = {
        'replace_value': ReplaceValueRule,
        'rename_key': RenameKeyRule,
        'merge_data': MergeDataRule,
        'pattern_replace': PatternReplaceRule,
        'template_string': TemplateStringRule,
        'conditional_replace': ConditionalReplaceRule,
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

        cls.RULE_TYPES[rule_type].validate(rule_data)


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


def apply_rules_to_data(data: Dict[str, Any], rules: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply all rules to config data and return list of update events."""
    events = []

    for name in sorted(rules.keys()):
        rule_data = rules[name]
        rule = RuleFactory.create(name, rule_data)
        events.extend(rule.apply(data))

    return events


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Apply transformation rules to configuration files.'
    )
    parser.add_argument(
        'rules_file',
        help='Path to the rules.json file'
    )
    parser.add_argument(
        'config_files',
        nargs='+',
        help='Config files to process'
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

    # Process each config file
    all_events = []
    for file_path in args.config_files:
        if not os.path.exists(file_path):
            print(f"Warning: File not found: {file_path}", file=sys.stderr)
            continue

        events = process_file(file_path, rules, inheritance_config, args.dry_run)
        all_events.extend(events)

    # Output in JSONL format
    for event in all_events:
        print(json.dumps(event))


if __name__ == '__main__':
    main()
