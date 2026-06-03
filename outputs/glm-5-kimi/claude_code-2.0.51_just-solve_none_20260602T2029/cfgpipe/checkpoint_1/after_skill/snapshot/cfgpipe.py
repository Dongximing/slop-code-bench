#!/usr/bin/env python3
"""
cfgpipe - Core Resolution

A command-line configuration resolver that reads a JSON schema document,
resolves each declared parameter from local sources, and writes the resolved
configuration to stdout as JSON.
"""

import json
import os
import sys
from typing import Any


def parse_boolean(value: str) -> bool:
    """Parse a boolean string representation."""
    lower = value.lower().strip()
    if lower in ('true', 'yes', '1', 'on', 'y'):
        return True
    if lower in ('false', 'no', '0', 'off', 'n'):
        return False
    raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    """Parse an integer from string."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as integer")
    check_str = stripped.lstrip('+-')
    if not check_str or not check_str.isdigit():
        raise ValueError(f"Cannot parse '{value}' as integer")
    return int(stripped)


def parse_float(value: str) -> float:
    """Parse a float from string."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as float")
    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def format_value(value: Any, type_name: str) -> str:
    """Format a parsed value according to its type."""
    if type_name == 'boolean':
        return 'true' if value else 'false'
    if type_name in ('integer', 'float'):
        return str(value)
    return value


def parse_value(value: str, type_name: str) -> Any:
    """Parse a string value according to the declared type."""
    if type_name == 'string':
        return value
    if type_name == 'integer':
        return parse_integer(value)
    if type_name == 'float':
        return parse_float(value)
    if type_name == 'boolean':
        return parse_boolean(value)
    raise ValueError(f"Unknown type: {type_name}")


def load_schema(schema_path: str) -> dict:
    """Load and validate the schema file."""
    if not os.path.isfile(schema_path):
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        with open(schema_path, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in schema file: {e}")

    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")

    for param_name, param_decl in schema.items():
        if not isinstance(param_decl, dict):
            raise ValueError(f"Parameter '{param_name}' declaration must be an object")

        if 'type' not in param_decl:
            raise ValueError(f"Parameter '{param_name}' missing required 'type' field")

        type_name = param_decl['type']
        if type_name not in ('string', 'integer', 'float', 'boolean'):
            raise ValueError(f"Parameter '{param_name}' has unrecognized type: {type_name}")

        for source in ('default', 'env', 'file', 'arg'):
            if source in param_decl and not isinstance(param_decl[source], str):
                raise ValueError(f"Parameter '{param_name}' source '{source}' must be a string")

    return schema


def parse_cli_args(arg_candidates: list) -> dict:
    """Parse CLI arguments into a dict for arg matching."""
    result = {}
    for arg in arg_candidates:
        if '=' in arg:
            if arg.startswith('--'):
                key, value = arg[2:].split('=', 1)
                result[key] = value
            elif arg.startswith('-'):
                key, value = arg[1:].split('=', 1)
                result[key] = value
    return result


def resolve_from_sources(param_name: str, param_decl: dict, cli_args: dict) -> tuple[bool, Any, str, str]:
    """
    Resolve a parameter from its sources.
    Priority order (highest to lowest): arg, file, env, default
    Returns: (found, parsed_value, source_type, raw_value_or_error)
    """
    type_name = param_decl['type']

    # Try arg (highest priority)
    if 'arg' in param_decl:
        arg_name = param_decl['arg']
        if arg_name in cli_args:
            arg_val = cli_args[arg_name]
            try:
                parsed = parse_value(arg_val, type_name)
                return (True, parsed, 'arg', arg_val)
            except ValueError as e:
                return (False, None, 'arg', str(e))

    # Try file
    if 'file' in param_decl:
        file_path = param_decl['file']
        if os.path.isfile(file_path):
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                content = content.strip()
                if content:
                    try:
                        parsed = parse_value(content, type_name)
                        return (True, parsed, 'file', content)
                    except ValueError as e:
                        return (False, None, 'file', str(e))
            except (IOError, OSError):
                pass  # Treat as absent

    # Try env
    if 'env' in param_decl:
        env_var = param_decl['env']
        if env_var in os.environ:
            env_val = os.environ[env_var]
            try:
                parsed = parse_value(env_val, type_name)
                return (True, parsed, 'env', env_val)
            except ValueError as e:
                return (False, None, 'env', str(e))

    # Try default (lowest priority)
    if 'default' in param_decl:
        default_val = param_decl['default']
        try:
            parsed = parse_value(default_val, type_name)
            return (True, parsed, 'default', default_val)
        except ValueError as e:
            return (False, None, 'default', str(e))

    return (False, None, None, None)


def main():
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    schema_path = sys.argv[1]
    arg_candidates = sys.argv[2:]

    # Load and validate schema
    try:
        schema = load_schema(schema_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse CLI args for arg matching
    cli_args = parse_cli_args(arg_candidates)

    # Resolve each parameter
    resolved = {}
    unresolved = []

    for param_name, param_decl in schema.items():
        found, parsed_value, source_type, raw_or_error = resolve_from_sources(
            param_name, param_decl, cli_args
        )

        if found:
            resolved[param_name] = format_value(parsed_value, param_decl['type'])
        elif source_type is not None:
            print(f"Error: Parameter '{param_name}' failed to parse from {source_type}: {raw_or_error}", file=sys.stderr)
            sys.exit(1)
        else:
            unresolved.append(param_name)

    # Check for unresolved parameters
    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Output resolved configuration
    print(json.dumps(resolved))
    sys.exit(0)


if __name__ == '__main__':
    main()
