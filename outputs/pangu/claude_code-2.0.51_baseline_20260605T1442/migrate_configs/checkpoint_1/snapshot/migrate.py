#!/usr/bin/env python3
"""
CLI tool to apply transformation rules to config files in various formats.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
import tomllib
import tomlkit


def parse_args():
    parser = argparse.ArgumentParser(
        description="Apply transformation rules to config files"
    )
    parser.add_argument("rules_file", help="Path to rules.json file")
    parser.add_argument("target_dir", help="Path to target directory containing config files")
    return parser.parse_args()


def load_rules(rules_path: Path) -> dict[str, dict]:
    """Load rules from JSON file."""
    try:
        with open(rules_path, "r", encoding="utf-8") as f:
            rules = json.load(f)
    except FileNotFoundError:
        print(f"Error: rules file not found: {rules_path}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in rules file: {rules_path}", file=sys.stderr)
        sys.exit(1)
    return rules


def load_config(file_path: Path) -> tuple[str, dict]:
    """Load config file and return (format, data)."""
    suffix = file_path.suffix.lower()

    if suffix == ".json":
        with open(file_path, "r", encoding="utf-8") as f:
            return "json", json.load(f)
    elif suffix in (".yaml", ".yml"):
        with open(file_path, "r", encoding="utf-8") as f:
            return "yaml", yaml.safe_load(f)
    elif suffix == ".toml":
        with open(file_path, "rb") as f:
            return "toml", tomllib.load(f)
    elif suffix == ".ini":
        # For INI files, we'll use configparser
        from configparser import ConfigParser
        parser = ConfigParser()
        parser.read(file_path, encoding="utf-8")
        # Convert to dict structure
        data = {}
        for section in parser.sections():
            data[section] = dict(parser[section])
        return "ini", data
    else:
        raise ValueError(f"Unsupported format: {suffix}")


def save_config(file_path: Path, fmt: str, data: dict) -> None:
    """Save config file preserving format."""
    suffix = file_path.suffix.lower()

    if suffix == ".json":
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
    elif suffix in (".yaml", ".yml"):
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    elif suffix == ".toml":
        with open(file_path, "w", encoding="utf-8") as f:
            tomlkit.dump(data, f)
    elif suffix == ".ini":
        from configparser import ConfigParser
        parser = ConfigParser()
        # Read existing to preserve structure
        parser.read(file_path, encoding="utf-8")
        # Update with new data
        for section, values in data.items():
            if not parser.has_section(section):
                parser.add_section(section)
            for key, value in values.items():
                parser[section][key] = str(value)
        with open(file_path, "w", encoding="utf-8") as f:
            parser.write(f)


def get_nested(data: dict, key_path: str) -> tuple[Optional[dict], Optional[str], Optional[Any]]:
    """Get nested value using dot notation. Returns (parent_dict, last_key, value)."""
    keys = key_path.split(".")
    current = data

    for i, key in enumerate(keys[:-1]):
        if not isinstance(current, dict) or key not in current:
            return None, None, None
        current = current[key]

    last_key = keys[-1]
    if not isinstance(current, dict) or last_key not in current:
        return None, None, None

    return current, last_key, current[last_key]


def set_nested(data: dict, key_path: str, value: Any) -> bool:
    """Set nested value using dot notation. Returns True if successful."""
    keys = key_path.split(".")
    current = data

    for i, key in enumerate(keys[:-1]):
        if not isinstance(current, dict):
            return False
        if key not in current:
            # Create intermediate dicts for new paths
            current[key] = {}
        current = current[key]

    last_key = keys[-1]
    if not isinstance(current, dict):
        return False

    current[last_key] = value
    return True


def deep_merge(base: dict, merge: dict) -> dict:
    """Deep merge two dictionaries."""
    result = base.copy()
    for key, value in merge.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def matches_glob(file_path: Path, glob_pattern: str, target_dir: Path) -> bool:
    """Check if file path matches glob pattern."""
    # Convert to relative path
    try:
        relative_path = file_path.relative_to(target_dir)
    except ValueError:
        return False

    # Handle the special case where pattern is "**/*"
    # This should match all files at any depth, including directly in target dir
    if glob_pattern == "**/*":
        # Always match for **/* pattern - all files
        return True

    # For other patterns, use pathlib's match which handles ** correctly
    # Path.match uses the same semantics as fnmatch with path separators
    return relative_path.match(glob_pattern)


def apply_rule(data: dict, rule: dict, file_path: Path, target_dir: Path) -> bool:
    """Apply a single rule to config data. Returns True if modified."""
    rule_type = rule.get("type")

    if rule_type == "replace_value":
        key = rule.get("key")
        value = rule.get("value")
        if key is None or value is None:
            return False
        parent, last_key, _ = get_nested(data, key)
        if parent is not None and last_key is not None:
            parent[last_key] = value
            return True
        return False

    elif rule_type == "rename_key":
        old_key = rule.get("old_key")
        new_key = rule.get("new_key")
        if old_key is None or new_key is None:
            return False
        parent, last_key, value = get_nested(data, old_key)
        if parent is not None and last_key is not None:
            # Remove old key
            del parent[last_key]
            # Set new key
            return set_nested(data, new_key, value)
        return False

    elif rule_type == "merge_data":
        glob_pattern = rule.get("glob", "**/*")
        merge_data = rule.get("data", {})
        if not merge_data:
            return False
        # Check if file matches glob
        if not matches_glob(file_path, glob_pattern, target_dir):
            return False
        # Perform deep merge
        merged = deep_merge(data, merge_data)
        # Only update if something changed
        if merged != data:
            # Update data in place
            data.clear()
            data.update(merged)
            return True
        return False

    return False


def find_config_files(target_dir: Path) -> list[Path]:
    """Find all config files in target directory recursively."""
    extensions = {".json", ".yaml", ".yml", ".toml", ".ini"}
    files = []
    for ext in extensions:
        files.extend(target_dir.rglob(f"*{ext}"))
    return sorted(files)


def main():
    args = parse_args()

    rules_path = Path(args.rules_file)
    target_dir = Path(args.target_dir)

    # Validate target directory
    if not target_dir.exists():
        print(f"Error: target directory not found: {target_dir}", file=sys.stderr)
        sys.exit(1)
    if not target_dir.is_dir():
        print(f"Error: target is not a directory: {target_dir}", file=sys.stderr)
        sys.exit(1)

    # Load rules and sort lexicographically by rule name
    rules = load_rules(rules_path)
    sorted_rule_names = sorted(rules.keys())
    sorted_rules = [(name, rules[name]) for name in sorted_rule_names]

    # Find all config files
    config_files = find_config_files(target_dir)

    # Process each file
    for file_path in config_files:
        try:
            fmt, data = load_config(file_path)
        except Exception as e:
            print(f"Error: failed to parse {file_path}", file=sys.stderr)
            sys.exit(1)

        # Apply rules and track which ones modified the file
        rules_applied = []
        for rule_name, rule in sorted_rules:
            if apply_rule(data, rule, file_path, target_dir):
                rules_applied.append(rule_name)

        # Save if modified
        if rules_applied:
            save_config(file_path, fmt, data)
            relative_path = str(file_path.relative_to(target_dir))
            print(json.dumps({
                "event": "file_updated",
                "file": relative_path,
                "rules_applied": rules_applied
            }))


if __name__ == "__main__":
    main()
