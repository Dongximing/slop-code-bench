#!/usr/bin/env python3
"""
cfgpipe - Command-line configuration resolver.

Reads a JSON schema document, resolves each declared parameter from local sources,
and writes the resolved configuration to stdout as JSON.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


def parse_boolean(value: str) -> bool:
    """
    Parse common boolean string representations.
    Returns the boolean value or raises ValueError if unrecognized.
    """
    normalized = value.strip().lower()
    if normalized in ('true', 'yes', '1', 'on', 'y'):
        return True
    if normalized in ('false', 'no', '0', 'off', 'n'):
        return False

    raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    """
    Parse decimal integer.
    Raises ValueError if not a valid decimal integer.
    """
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for integer: '{value}'")

    try:
        result = int(stripped)
        if '.' in stripped:
            raise ValueError(f"Float value '{value}' cannot be parsed as integer")
        return result
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as integer")


def parse_float(value: str) -> float:
    """
    Parse decimal float.
    Raises ValueError if not a valid decimal float.
    """
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not supported: '{value}'")

    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def format_boolean(value: bool) -> str:
    """Format boolean as lowercase 'true' or 'false'."""
    return 'true' if value else 'false'


def format_integer(value: int) -> str:
    """Format integer as plain decimal string."""
    return str(value)


def format_float(value: float) -> str:
    """Format float as decimal string."""
    if value == int(value):
        return str(int(value))
    return str(value)


def parse_value(value: str, type_name: str) -> Tuple[Any, str]:
    """
    Parse a string value according to the declared type.
    Returns (parsed_value, formatted_string).
    Raises ValueError on parse failure.
    """
    if type_name == 'string':
        return value, value
    elif type_name == 'integer':
        parsed = parse_integer(value)
        return parsed, format_integer(parsed)
    elif type_name == 'float':
        parsed = parse_float(value)
        return parsed, format_float(parsed)
    elif type_name == 'boolean':
        parsed = parse_boolean(value)
        return parsed, format_boolean(parsed)
    else:
        raise ValueError(f"Unknown type: '{type_name}'")


def validate_schema(schema: Dict[str, Any]) -> None:
    """
    Validate the schema structure.
    Raises ValueError with descriptive message on validation failure.
    """
    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")

    seen_names = set()
    for name, param in schema.items():
        if name in seen_names:
            raise ValueError(f"Duplicate parameter name: '{name}'")
        seen_names.add(name)

        if not isinstance(param, dict):
            raise ValueError(f"Parameter '{name}' must be an object")

        if 'type' not in param:
            raise ValueError(f"Parameter '{name}' missing required 'type' field")

        type_name = param['type']
        if type_name not in ('string', 'integer', 'float', 'boolean'):
            raise ValueError(f"Parameter '{name}' has unrecognized type: '{type_name}'")

        for field in ('default', 'env', 'file', 'arg'):
            if field in param:
                if not isinstance(param[field], str):
                    raise ValueError(f"Parameter '{name}' has non-string '{field}' field")


def get_arg_value(arg_candidates: List[str], arg_name: str) -> Optional[str]:
    """
    Extract value from CLI argument candidates.
    Supports --name=value and -name=value formats.
    Last match wins.
    """
    result = None

    for arg in arg_candidates:
        if arg.startswith(f"--{arg_name}="):
            result = arg.split('=', 1)[1]
        elif arg.startswith(f"-{arg_name}="):
            result = arg.split('=', 1)[1]

    return result


def get_env_value(env_var: str) -> Optional[str]:
    """Get value from environment variable."""
    return os.environ.get(env_var)


def get_file_value(file_path: str) -> Optional[str]:
    """
    Get value from file.
    Returns None if file doesn't exist, is a directory, or is empty after trimming.
    """
    if not os.path.exists(file_path):
        return None

    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, 'r') as f:
            content = f.read()

        trimmed = content.strip()
        if not trimmed:
            return None

        return trimmed
    except (IOError, OSError):
        return None


def resolve_parameter(
    name: str,
    param: Dict[str, Any],
    arg_candidates: List[str]
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]]]:
    """
    Resolve a single parameter from its declared sources.

    Returns:
        (formatted_value, None) on success
        (None, (param_name, source, reason)) on parse failure
        (None, None) if unresolved
    """
    type_name = param['type']
    sources = []

    if 'arg' in param:
        arg_value = get_arg_value(arg_candidates, param['arg'])
        if arg_value is not None:
            sources.append(('arg', arg_value))

    if 'file' in param:
        file_value = get_file_value(param['file'])
        if file_value is not None:
            sources.append(('file', file_value))

    if 'env' in param:
        env_value = get_env_value(param['env'])
        if env_value is not None:
            sources.append(('env', env_value))

    if 'default' in param:
        sources.append(('default', param['default']))

    for source_name, source_value in sources:
        try:
            _, formatted = parse_value(source_value, type_name)
            return formatted, None
        except ValueError as e:
            return None, (name, source_name, str(e))

    return None, None


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    schema_file = sys.argv[1]
    arg_candidates = sys.argv[2:]

    if not os.path.exists(schema_file):
        print(f"Error: Schema file not found: '{schema_file}'", file=sys.stderr)
        return 1

    try:
        with open(schema_file, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
        return 1
    except (IOError, OSError) as e:
        print(f"Error: Cannot read schema file: {e}", file=sys.stderr)
        return 1

    try:
        validate_schema(schema)
    except ValueError as e:
        print(f"Error: Schema validation failed: {e}", file=sys.stderr)
        return 1

    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for name, param in schema.items():
        formatted_value, parse_error = resolve_parameter(name, param, arg_candidates)

        if parse_error is not None:
            param_name, source, reason = parse_error
            print(f"Error: Failed to parse parameter '{param_name}' from source '{source}': {reason}", file=sys.stderr)
            return 1

        if formatted_value is not None:
            resolved[name] = formatted_value
        else:
            unresolved.append(name)

    if unresolved:
        unresolved_str = ", ".join(f"'{name}'" for name in unresolved)
        print(f"Error: Unresolved parameters: {unresolved_str}", file=sys.stderr)
        return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
