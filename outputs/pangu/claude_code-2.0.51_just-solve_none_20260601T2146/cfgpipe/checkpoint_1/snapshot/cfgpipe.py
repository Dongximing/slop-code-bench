#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver."""

import json
import os
import sys
from typing import Any


def parse_cli_args(args: list[str]) -> dict[str, str]:
    """Parse CLI arguments of the form --name=value or -name=value.

    Only arguments after the schema-file path participate.
    Last wins for duplicate keys.
    """
    result = {}
    for arg in args:
        if '=' in arg:
            # Split on first '=' to allow values containing '='
            name_value = arg.split('=', 1)
            if len(name_value) == 2:
                name = name_value[0]
                value = name_value[1]
                # Remove leading dashes
                if name.startswith('--'):
                    name = name[2:]
                elif name.startswith('-'):
                    name = name[1:]
                result[name] = value
    return result


def get_env_value(env_name: str) -> str | None:
    """Get environment variable value if set and non-empty."""
    value = os.environ.get(env_name)
    if value is None:
        return None
    trimmed = value.strip()
    if trimmed == '':
        return None
    return trimmed


def get_file_value(file_path: str) -> str | None:
    """Read file content if it exists and is a regular file with non-empty content."""
    try:
        if not os.path.exists(file_path):
            return None
        if not os.path.isfile(file_path):
            return None
        with open(file_path, 'r') as f:
            content = f.read()
        trimmed = content.strip()
        if trimmed == '':
            return None
        return trimmed
    except (OSError, IOError):
        return None


def validate_schema(schema: dict[str, Any]) -> list[str] | None:
    """Validate the schema structure. Returns list of errors or None if valid."""
    errors = []
    param_names = set()

    for key, value in schema.items():
        # Check key is unique
        if key in param_names:
            errors.append(f"duplicate parameter name: {key}")
        param_names.add(key)

        # Check value is a dict
        if not isinstance(value, dict):
            errors.append(f"parameter '{key}' must be an object")
            continue

        # Check required 'type' field
        if 'type' not in value:
            errors.append(f"parameter '{key}' is missing required 'type' field")
            continue

        # Validate source fields are strings
        for source_field in ['default', 'env', 'file', 'arg']:
            if source_field in value and not isinstance(value[source_field], str):
                errors.append(f"parameter '{key}' has non-string '{source_field}' field")

    return errors if errors else None


def parse_value(value: str, type_name: str, param_name: str) -> str:
    """Parse a value against a type and return the canonical string representation.

    Returns the parsed value as a string on success, raises ValueError on failure.
    """
    if type_name == 'string':
        return value

    elif type_name == 'integer':
        # Must be a decimal integer, no scientific notation
        try:
            # Check it's a valid integer (no decimal point, no scientific notation)
            if '.' in value:
                raise ValueError("integer type does not accept decimal values")
            int(value)  # This will raise ValueError if invalid
            return value  # Return as-is, already in canonical form
        except ValueError as e:
            if "integer type" in str(e):
                raise
            raise ValueError(f"not a valid decimal integer: {value}")

    elif type_name == 'float':
        # Decimal inputs, no exact canonical format required
        try:
            float(value)
            # Return canonical representation without scientific notation
            fval = float(value)
            # Check if it's in scientific notation and convert if needed
            if 'e' in value.lower():
                raise ValueError("scientific notation not allowed")
            # Just return the original value, no canonical format required
            return value
        except ValueError as e:
            if "scientific notation" in str(e):
                raise
            raise ValueError(f"not a valid decimal float: {value}")

    elif type_name == 'boolean':
        # Common boolean string representations → lowercase true or false
        truthy = {'true', 'yes', 'on', '1', 't', 'y'}
        falsy = {'false', 'no', 'off', '0', 'f', 'n'}
        val_lower = value.lower()
        if val_lower in truthy:
            return 'true'
        elif val_lower in falsy:
            return 'false'
        else:
            raise ValueError(f"not a valid boolean representation: {value}")

    else:
        raise ValueError(f"unknown type: {type_name}")


def resolve_parameter(
    param_name: str,
    param_decl: dict[str, Any],
    cli_args: dict[str, str]
) -> tuple[str | None, str | None]:
    """Resolve a single parameter from its sources.

    Returns (value, source) tuple where value is the resolved string value or None,
    and source is the name of the source that provided it or None.
    """
    param_type = param_decl['type']

    # Source resolution order: highest to lowest priority = arg → file → env → default
    # (arg wins, then file, then env, then default only if nothing else provides a value)
    sources = ['arg', 'file', 'env', 'default']

    for source in sources:
        if source not in param_decl:
            continue

        value = None
        if source == 'default':
            value = param_decl.get('default')
        elif source == 'env':
            value = get_env_value(param_decl['env'])
        elif source == 'file':
            value = get_file_value(param_decl['file'])
        elif source == 'arg':
            arg_name = param_decl['arg']
            value = cli_args.get(arg_name)

        if value is not None:
            return value, source

    return None, None  # Unresolved


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    schema_path = sys.argv[1]
    cli_arg_strings = sys.argv[2:]

    # Read and parse schema file
    try:
        with open(schema_path, 'r') as f:
            schema_content = f.read()
    except OSError as e:
        print(f"Error reading schema file: {e}", file=sys.stderr)
        return 1

    try:
        schema = json.loads(schema_content)
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON schema: {e}", file=sys.stderr)
        return 1

    # Validate schema structure
    if not isinstance(schema, dict):
        print("Error: schema must be a JSON object", file=sys.stderr)
        return 1

    validation_errors = validate_schema(schema)
    if validation_errors:
        for err in validation_errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    # Parse CLI arguments
    cli_args = parse_cli_args(cli_arg_strings)

    # Resolve parameters
    resolved = {}
    unresolved = []
    parse_error = None

    for param_name, param_decl in schema.items():
        value, source = resolve_parameter(param_name, param_decl, cli_args)

        if value is None:
            unresolved.append(param_name)
            continue

        # Parse against declared type
        try:
            parsed_value = parse_value(value, param_decl['type'], param_name)
            resolved[param_name] = parsed_value
        except ValueError as e:
            # Parse failure halts immediately
            parse_error = (param_name, source, str(e))
            break

    # Check for parse error
    if parse_error:
        param_name, source, reason = parse_error
        print(f"Error: parameter '{param_name}' from source '{source}': {reason}", file=sys.stderr)
        return 1

    # Check for unresolved parameters
    if unresolved:
        print(f"Error: unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        return 1

    # Success - write JSON to stdout
    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
