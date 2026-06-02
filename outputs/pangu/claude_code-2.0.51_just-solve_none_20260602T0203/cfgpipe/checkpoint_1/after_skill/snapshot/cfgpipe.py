#!/usr/bin/env python3
"""cfgpipe - Configuration resolver CLI."""

import json
import os
import sys
from typing import Any


def parse_cli_args(cli_args: list[str]) -> dict[str, str]:
    """Parse CLI argument candidates into a dict of name -> value.

    Supports --name=value and -name=value formats.

    Args:
        cli_args: List of argument strings after the schema file path.

    Returns:
        Dictionary mapping argument names to their values.
    """
    result = {}
    for arg in cli_args:
        if '=' in arg:
            name_part, value = arg.split('=', 1)
            # Strip leading dashes
            name = name_part.lstrip('-')
            result[name] = value
    return result


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate the schema structure and field types.

    Raises:
        ValueError: If schema is invalid.
    """
    if not isinstance(schema, dict):
        raise ValueError("Schema must be a JSON object")

    for param_name, param_spec in schema.items():
        if not isinstance(param_spec, dict):
            raise ValueError(f"Parameter '{param_name}': specification must be an object")

        if 'type' not in param_spec:
            raise ValueError(f"Parameter '{param_name}': missing required 'type' field")

        param_type = param_spec['type']
        if param_type not in ('string', 'integer', 'float', 'boolean'):
            raise ValueError(f"Parameter '{param_name}': unrecognized type '{param_type}'")

        # Validate source fields are strings if present
        for source_field in ('default', 'env', 'file', 'arg'):
            if source_field in param_spec and not isinstance(param_spec[source_field], str):
                raise ValueError(
                    f"Parameter '{param_name}': '{source_field}' must be a string, "
                    f"got {type(param_spec[source_field]).__name__}"
                )


def load_schema(schema_path: str) -> dict[str, Any]:
    """Load and parse the schema file.

    Raises:
        FileNotFoundError: If schema file doesn't exist.
        ValueError: If JSON is invalid or schema is malformed.
    """
    try:
        with open(schema_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in schema file: {e}")

    validate_schema(schema)
    return schema


def parse_type(value: str, expected_type: str, param_name: str) -> str:
    """Parse and validate a string value against the expected type.

    Returns the value as a string after validation/normalization.
    Raises ValueError if parsing fails.
    """
    if expected_type == 'string':
        return value

    elif expected_type == 'integer':
        value = value.strip()
        try:
            int(value)
        except ValueError:
            raise ValueError(
                f"Parameter '{param_name}': cannot parse '{value}' as integer"
            )
        if 'e' in value.lower() or 'E' in value:
            raise ValueError(
                f"Parameter '{param_name}': scientific notation not allowed for integers"
            )
        return value

    elif expected_type == 'float':
        value = value.strip()
        try:
            fval = float(value)
            if 'e' in value.lower() or 'E' in value:
                raise ValueError(
                    f"Parameter '{param_name}': scientific notation not allowed"
                )
            return str(fval)
        except ValueError:
            raise ValueError(
                f"Parameter '{param_name}': cannot parse '{value}' as float"
            )

    elif expected_type == 'boolean':
        value_lower = value.strip().lower()
        if value_lower in ('true', 'false'):
            return value_lower
        else:
            raise ValueError(
                f"Parameter '{param_name}': boolean must be 'true' or 'false', got '{value}'"
            )

    raise ValueError(f"Parameter '{param_name}': unknown type '{expected_type}'")


def resolve_parameter(
    param_name: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str]
) -> str | None:
    """Resolve a single parameter from its sources.

    Priority order: default, env, file, arg.
    Returns the resolved value as a string, or None if unresolved.
    Raises ValueError if parsing fails.
    """
    param_type = param_spec['type']

    # 1. Check default
    if 'default' in param_spec:
        value = parse_type(param_spec['default'], param_type, param_name)
        return value

    # 2. Check env
    if 'env' in param_spec:
        env_var = param_spec['env']
        env_value = os.environ.get(env_var)
        if env_value is not None and env_value.strip():
            value = parse_type(env_value, param_type, param_name)
            return value

    # 3. Check file
    if 'file' in param_spec:
        file_path = param_spec['file']
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
        except (FileNotFoundError, OSError, IsADirectoryError):
            # File doesn't exist or can't be read - count as absent
            pass
        else:
            # Trim surrounding whitespace
            trimmed = file_content.strip()
            if trimmed:
                value = parse_type(trimmed, param_type, param_name)
                return value
            # Empty after trimming - count as absent

    # 4. Check arg
    if 'arg' in param_spec:
        arg_name = param_spec['arg']
        if arg_name in arg_values:
            arg_value = arg_values[arg_name]
            value = parse_type(arg_value, param_type, param_name)
            return value

    # Unresolved
    return None


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    schema_path = sys.argv[1]
    cli_args = sys.argv[2:]

    # Load schema
    try:
        schema = load_schema(schema_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Parse CLI arguments for arg matching
    arg_values = parse_cli_args(cli_args)

    # Resolve each parameter
    results: dict[str, str] = {}
    unresolved: list[str] = []

    for param_name in schema:
        try:
            resolved = resolve_parameter(param_name, schema[param_name], arg_values)
            if resolved is None:
                unresolved.append(param_name)
            else:
                results[param_name] = resolved
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    # Check for unresolved parameters
    if unresolved:
        if len(unresolved) == 1:
            print(f"Error: Unresolved parameter: {unresolved[0]}", file=sys.stderr)
        else:
            print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Output resolved configuration as JSON
    output = json.dumps(results)
    print(output)
    sys.exit(0)


if __name__ == '__main__':
    main()
