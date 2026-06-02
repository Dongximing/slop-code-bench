#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver."""

import json
import os
import sys
from typing import Any


def parse_cli_args(args: list[str]) -> dict[str, str]:
    """Parse CLI arguments in --name=value or -name=value format.

    Last value wins for duplicate keys.
    """
    result = {}
    for arg in args:
        if '=' in arg:
            name, value = arg.split('=', 1)
            result[name.lstrip("-")] = value
    return result


def get_env_value(env_name: str) -> str | None:
    """Return environment variable value if set and non-empty."""
    trimmed = os.environ.get(env_name, "").strip()
    return trimmed if trimmed else None


def get_file_value(file_path: str) -> str | None:
    """Return file content if it exists as a regular file with non-empty content."""
    try:
        if not os.path.isfile(file_path):
            return None
        trimmed = open(file_path).read().strip()
        return trimmed if trimmed else None
    except (OSError, IOError):
        return None


def validate_schema(schema: dict[str, Any]) -> str | None:
    """Validate schema structure. Return error message or None if valid."""
    seen = set()
    valid_types = {'string', 'integer', 'float', 'boolean'}

    for name, decl in schema.items():
        if name in seen:
            return f"duplicate parameter name: {name}"
        seen.add(name)

        if not isinstance(decl, dict):
            return f"parameter '{name}' must be an object"

        if 'type' not in decl:
            return f"parameter '{name}' is missing required 'type' field"

        if decl['type'] not in valid_types:
            return f"unknown type: {decl['type']}"

        for field in ['default', 'env', 'file', 'arg']:
            if field in decl and not isinstance(decl[field], str):
                return f"parameter '{name}' has non-string '{field}' field"

    return None


def parse_value(value: str, type_name: str, _param_name: str = "") -> str:
    """Parse a value against a type and return canonical string representation."""
    if type_name == 'string':
        return value

    if type_name == 'integer':
        if '.' in value:
            raise ValueError("integer type does not accept decimal values")
        int(value)
        return value

    if type_name == 'float':
        if 'e' in value.lower():
            raise ValueError("scientific notation not allowed")
        float(value)
        return value

    if type_name == 'boolean':
        if value.lower() in {'true', 'yes', 'on', '1', 't', 'y'}:
            return 'true'
        if value.lower() in {'false', 'no', 'off', '0', 'f', 'n'}:
            return 'false'
        raise ValueError(f"not a valid boolean representation: {value}")

    raise ValueError(f"unknown type: {type_name}")


def resolve_parameter(name: str, decl: dict[str, Any], cli_args: dict[str, str]):
    """Return (value, source) for the highest-priority source providing a value.

    Priority: arg (if declared) -> arg (default to param name) -> file -> env -> default
    Returns (None, None) if no source provides a value.
    """
    # First check for explicit 'arg' key in decl
    if 'arg' in decl:
        val = cli_args.get(decl['arg'])
        if val is not None:
            return val, 'arg'
    else:
        # Also check with param name (for backward compatibility)
        val = cli_args.get(name)
        if val is not None:
            return val, 'arg'

    # File, env, default sources
    if 'file' in decl:
        val = get_file_value(decl['file'])
        if val is not None:
            return val, 'file'

    if 'env' in decl:
        val = get_env_value(decl['env'])
        if val is not None:
            return val, 'env'

    if 'default' in decl:
        return decl['default'], 'default'

    return None, None


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    schema_path = sys.argv[1]

    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except OSError as e:
        print(f"Error reading schema file: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON schema: {e}", file=sys.stderr)
        return 1

    if not isinstance(schema, dict):
        print("Error: schema must be a JSON object", file=sys.stderr)
        return 1

    err = validate_schema(schema)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    cli_args = parse_cli_args(sys.argv[2:])
    resolved = {}

    for name, decl in schema.items():
        value, source = resolve_parameter(name, decl, cli_args)
        if value is None:
            print(f"Error: unresolved parameter: {name}", file=sys.stderr)
            return 1
        try:
            resolved[name] = parse_value(value, decl['type'], name)
        except ValueError as e:
            print(f"Error: parameter '{name}' from source '{source}': {e}", file=sys.stderr)
            return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
