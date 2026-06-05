#!/usr/bin/env python3
"""Config Migration Tool - Apply transformation rules to configuration files."""

import argparse
import json
import os
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Dict, List, Tuple

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
    def load(file_path: str) -> Tuple[Dict[str, Any], str]:
        """Load a config file and return (data, format). Format is 'json', 'yaml', or 'toml'."""
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
                json.dump(data, f, indent=2)
        elif format_ in ('yaml', 'yml'):
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        elif format_ == 'toml':
            if not HAS_TOML:
                raise RuntimeError("TOML support requires tomli and tomli-w")
            with open(file_path, 'wb') as f:
                tomli_w.dump(data, f)
        else:
            raise ValueError(f"Unsupported format: {format_}")


class Matcher:
    """Match configuration key paths against patterns."""

    @staticmethod
    def _parse_pattern(pattern: str) -> List[str]:
        """Parse a pattern into a list of components."""
        return pattern.split('.')

    @staticmethod
    def match_pattern(pattern: str, key_path: str) -> Optional[Dict[str, Any]]:
        """
        Match a key path against a pattern.
        Returns a dict of captured variables if match, None otherwise.

        Pattern syntax:
        - $variable - Captures a path component
        - * - Matches any component without capturing
        - Literal text must match exactly

        Patterns and key paths are dot-separated paths.
        """
        pattern_parts = Matcher._parse_pattern(pattern)
        key_parts = Matcher._parse_pattern(key_path)

        if len(pattern_parts) != len(key_parts):
            return None

        variables = {}
        for p_part, k_part in zip(pattern_parts, key_parts):
            if p_part.startswith('$'):
                # Variable capture
                var_name = p_part[1:]
                variables[var_name] = k_part
            elif p_part == '*':
                # Wildcard - matches anything without capturing
                pass
            elif p_part != k_part:
                # Literal mismatch
                return None

        return variables

    @staticmethod
    def find_matches(pattern: str, data: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any], List[Any]]]:
        """
        Find all keys in the data that match the pattern.

        Yields (full_key_path, variables, [parent, key]) tuples.
        The parent is the dict containing the value, key is the component name.
        """
        def _traverse(obj: Any, path: List[str], parent_chain: List[Any]) -> Iterator[Tuple[str, Dict[str, Any], List[Any]]]:
            if isinstance(obj, dict):
                for key, value in obj.items():
                    current_path = path + [key]
                    full_path = '.'.join(current_path)

                    # Check if this path matches the pattern
                    variables = Matcher.match_pattern(pattern, full_path)
                    if variables is not None:
                        # Get parent and key for modification
                        parent = parent_chain[-1] if parent_chain else None
                        parent_key = current_path[-1] if current_path else None
                        yield (full_path, variables, parent_chain + [obj, key])

                    # Continue traversing
                    yield from _traverse(value, current_path, parent_chain + [obj])
            elif isinstance(obj, list):
                for i, item in enumerate(obj):
                    current_path = path + [str(i)]
                    parent = parent_chain[-1] if parent_chain else None
                    parent_key = str(i)
                    yield from _traverse(item, current_path, parent_chain + [obj])

        yield from _traverse(data, [], [])


class Rule(ABC):
    """Abstract base class for transformation rules."""

    def __init__(self, name: str, rule_data: Dict[str, Any]):
        self.name = name
        self.rule_data = rule_data

    @staticmethod
    @abstractmethod
    def validate(rule_data: Dict[str, Any]) -> None:
        """Validate rule configuration. Raises RuleValidationError if invalid."""
        pass

    @abstractmethod
    def apply(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Apply the rule to the config data.
        Returns a list of updates, each as {'event': 'file_updated', ...}
        """
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

        keys = key.split('.')
        parent = data
        for k in keys[:-1]:
            if not isinstance(parent, dict) or k not in parent:
                return updates
            parent = parent[k]

        last_key = keys[-1]
        if isinstance(parent, dict) and last_key in parent:
            parent[last_key] = new_value
            updates.append({
                'event': 'file_updated',
                'file': '[applied]',
                'rules_applied': [self.name]
            })

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

        old_parts = old_key.split('.')
        new_parts = new_key.split('.')

        # Navigate to parent of old key
        parent = data
        for k in old_parts[:-1]:
            if not isinstance(parent, dict) or k not in parent:
                return updates
            parent = parent[k]

        last_key = old_parts[-1]
        if isinstance(parent, dict) and last_key in parent:
            value = parent.pop(last_key)

            # Navigate to parent of new key (may need to create intermediate dicts)
            new_parent = data
            for k in new_parts[:-1]:
                if k not in new_parent or not isinstance(new_parent[k], dict):
                    new_parent[k] = {}
                new_parent = new_parent[k]

            new_parent[new_parts[-1]] = value
            updates.append({
                'event': 'file_updated',
                'file': '[applied]',
                'rules_applied': [self.name]
            })

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
            """Merge source into target, returns True if any change was made."""
            changed = False
            for key, value in source.items():
                if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                    if deep_merge(target[key], value):
                        changed = True
                else:
                    target[key] = value
                    changed = True
            return changed

        if deep_merge(data, merge_data):
            updates.append({
                'event': 'file_updated',
                'file': '[applied]',
                'rules_applied': [self.name]
            })

        return updates


class PatternReplaceRule(Rule):
    """Rule for pattern-based replacement."""

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
            # Get the parent dict and key from the chain
            parent = parent_chain[-2] if len(parent_chain) >= 2 else None
            key = parent_chain[-1] if parent_chain else None

            if isinstance(parent, dict) and key is not None:
                # Substitute variables in the value template
                if isinstance(value_template, str):
                    final_value = self._substitute(value_template, variables)
                else:
                    final_value = value_template

                parent[key] = final_value

                if not updates:
                    updates.append({
                        'event': 'file_updated',
                        'file': '[applied]',
                        'rules_applied': [self.name]
                    })

        return updates

    def _substitute(self, template: str, variables: Dict[str, Any]) -> str:
        """Substitute ${var} in template with variable values."""
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

        # Check if target key exists and contains a string value
        target_parent = data
        target_keys = key.split('.')
        for k in target_keys[:-1]:
            if not isinstance(target_parent, dict) or k not in target_parent:
                return updates
            target_parent = target_parent[k]

        last_key = target_keys[-1]
        if not isinstance(target_parent, dict) or last_key not in target_parent:
            return updates
        if not isinstance(target_parent[last_key], str):
            return updates

        # Extract values for variables
        extracted = {}
        for var_name, key_path in variables.items():
            value = ConfigLoader._get_nested_value(data, key_path)
            if value is None:
                return updates  # Skip if any variable is missing
            extracted[var_name] = str(value)

        # Build the template
        final_value = self._substitute(template, extracted)
        target_parent[last_key] = final_value

        updates.append({
            'event': 'file_updated',
            'file': '[applied]',
            'rules_applied': [self.name]
        })

        return updates

    def _substitute(self, template: str, variables: Dict[str, Any]) -> str:
        """Substitute ${var} in template with variable values."""
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

        # Get current value
        current_value = ConfigLoader._get_nested_value(data, key)
        if current_value is None:
            return updates

        # Check condition
        if not self._check_condition(current_value, condition):
            return updates

        # Apply replacement
        keys = key.split('.')
        parent = data
        for k in keys[:-1]:
            if not isinstance(parent, dict) or k not in parent:
                return updates
            parent = parent[k]

        last_key = keys[-1]
        if isinstance(parent, dict):
            parent[last_key] = new_value
            updates.append({
                'event': 'file_updated',
                'file': '[applied]',
                'rules_applied': [self.name]
            })

        return updates

    def _check_condition(self, current_value: Any, condition: Dict[str, Any]) -> bool:
        """Check if the current value satisfies the condition."""
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
        """Create a rule instance."""
        rule_type = rule_data.get('type')
        if rule_type not in cls.RULE_TYPES:
            raise RuleValidationError(f"Unknown rule type: {rule_type}")

        return cls.RULE_TYPES[rule_type](name, rule_data)

    @classmethod
    def validate_rule(cls, name: str, rule_data: Dict[str, Any]) -> None:
        """Validate a rule configuration."""
        if 'type' not in rule_data:
            raise RuleValidationError(f"Rule '{name}' must have a 'type' field")

        rule_type = rule_data.get('type')
        if rule_type not in cls.RULE_TYPES:
            raise RuleValidationError(f"Rule '{name}' has unknown type: {rule_type}")

        cls.RULE_TYPES[rule_type].validate(rule_data)


class ConfigLoader:
    """Load and save configuration files in various formats."""

    @staticmethod
    def _get_nested_value(data: Dict[str, Any], key_path: str) -> Any:
        """Get a nested value by key path."""
        keys = key_path.split('.')
        value = data
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return None
        return value

    @staticmethod
    def load(file_path: str) -> Tuple[Dict[str, Any], str]:
        """Load a config file and return (data, format). Format is 'json', 'yaml', or 'toml'."""
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
                json.dump(data, f, indent=2)
        elif format_ in ('yaml', 'yml'):
            with open(file_path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        elif format_ == 'toml':
            if not HAS_TOML:
                raise RuntimeError("TOML support requires tomli and tomli-w")
            with open(file_path, 'wb') as f:
                tomli_w.dump(data, f)
        else:
            raise ValueError(f"Unsupported format: {format_}")


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

    # Apply rules in lexicographic order by name
    for name in sorted(rules.keys()):
        rule_data = rules[name]
        rule = RuleFactory.create(name, rule_data)
        events.extend(rule.apply(data))

    return events


def process_file(file_path: str, rules: Dict[str, Dict[str, Any]], dry_run: bool = False) -> List[Dict[str, Any]]:
    """Process a single config file."""
    try:
        data, format_ = ConfigLoader.load(file_path)
    except Exception as e:
        print(f"Error loading {file_path}: {e}", file=sys.stderr)
        return []

    events = apply_rules_to_data(data, rules)

    if events and not dry_run:
        try:
            ConfigLoader.save(file_path, data, format_)
        except Exception as e:
            print(f"Error saving {file_path}: {e}", file=sys.stderr)
            return []

    # Update events with actual file path
    for event in events:
        event['file'] = file_path

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

    args = parser.parse_args()

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

        events = process_file(file_path, rules, args.dry_run)
        all_events.extend(events)

    # Output in JSONL format
    for event in all_events:
        print(json.dumps(event))


if __name__ == '__main__':
    main()
