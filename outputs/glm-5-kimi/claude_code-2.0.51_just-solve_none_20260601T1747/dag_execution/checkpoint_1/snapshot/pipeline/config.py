"""
Configuration file parser for TOML config.
"""

import os
import re
import toml
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass


@dataclass
class Config:
    entry: Optional[str] = None  # Task name or task call like "A(1, x=2)"
    clean_cwd: bool = False
    env: Dict[str, str] = None

    def __post_init__(self):
        if self.env is None:
            self.env = {}


class ConfigParseError(Exception):
    pass


def parse_config(config_path: str) -> Config:
    """Parse a TOML configuration file."""
    if not os.path.exists(config_path):
        raise ConfigParseError(f"SYNTAX_ERROR: Config file not found: {config_path}")

    try:
        with open(config_path, 'r') as f:
            data = toml.load(f)
    except toml.TomlDecodeError as e:
        raise ConfigParseError(f"SYNTAX_ERROR: Invalid TOML: {e}")

    entry = data.get('entry')
    clean_cwd = data.get('clean_cwd', False)
    env = data.get('env', {})

    return Config(entry=entry, clean_cwd=clean_cwd, env=env)


def parse_entry_call(entry: str) -> Tuple[str, List[Any], Dict[str, Any]]:
    """
    Parse an entry call like "A" or "A(1, x=2)" into (task_name, positional_args, named_args).

    Args:
        entry: Entry string like "A" or "A(1, x=2)"

    Returns:
        Tuple of (task_name, positional_args, named_args)
    """
    if entry is None:
        return None, [], {}

    # Check if it has arguments
    match = re.match(r'^(\w+)(?:\((.*)\))?$', entry.strip())
    if not match:
        raise ConfigParseError(f"SYNTAX_ERROR: Invalid entry format: {entry}")

    task_name = match.group(1)
    args_str = match.group(2)

    if args_str is None:
        return task_name, [], {}

    positional_args = []
    named_args = {}

    # Parse arguments
    args_str = args_str.strip()
    if args_str:
        # Split by comma, but respect nested structures
        parts = split_args(args_str)

        for part in parts:
            part = part.strip()
            if '=' in part:
                # Named argument
                eq_pos = part.index('=')
                name = part[:eq_pos].strip()
                value_str = part[eq_pos + 1:].strip()
                value = parse_value(value_str)
                named_args[name] = value
            else:
                # Positional argument
                value = parse_value(part)
                positional_args.append(value)

    return task_name, positional_args, named_args


def split_args(args_str: str) -> List[str]:
    """Split arguments by comma, respecting nested structures."""
    parts = []
    current = []
    depth = 0
    in_string = False
    string_char = None

    i = 0
    while i < len(args_str):
        ch = args_str[i]

        if in_string:
            current.append(ch)
            if ch == string_char and (i == 0 or args_str[i-1] != '\\'):
                in_string = False
        elif ch in '"\'':
            in_string = True
            string_char = ch
            current.append(ch)
        elif ch in '([{':
            depth += 1
            current.append(ch)
        elif ch in ')]}':
            depth -= 1
            current.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
        else:
            current.append(ch)

        i += 1

    if current:
        parts.append(''.join(current))

    return parts


def parse_value(value_str: str) -> Any:
    """Parse a value string into a Python value."""
    value_str = value_str.strip()

    # Boolean
    if value_str.lower() == 'true':
        return True
    if value_str.lower() == 'false':
        return False

    # String (quoted)
    if (value_str.startswith('"') and value_str.endswith('"')) or \
       (value_str.startswith("'") and value_str.endswith("'")):
        return value_str[1:-1]

    # Integer
    try:
        return int(value_str)
    except ValueError:
        pass

    # Float
    try:
        return float(value_str)
    except ValueError:
        pass

    # List
    if value_str.startswith('[') and value_str.endswith(']'):
        inner = value_str[1:-1].strip()
        if not inner:
            return []
        parts = split_args(inner)
        return [parse_value(p) for p in parts]

    # Default to string
    return value_str
