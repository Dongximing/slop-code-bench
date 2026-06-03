#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver."""

import argparse
import json
import os
import sys
from typing import Any


def parse_args(args: list[str]) -> tuple[str, list[str]]:
    """Parse command-line arguments manually.

    Args:
        args: Command-line arguments (excluding script name)

    Returns:
        Tuple of (schema_file_path, arg_candidates)
    """
    if not args:
        print("Error: No schema file specified", file=sys.stderr)
        sys.exit(1)

    schema_file = args[0]
    arg_candidates = args[1:]
    return schema_file, arg_candidates


def parse_arg_candidates(arg_candidates: list[str]) -> dict[str, str]:
    """Parse argument candidates into a dictionary.

    Supports --name=value and -name=value formats.
    Last value wins for duplicate keys.

    Args:
        arg_candidates: List of argument strings like ["--port=9000", "-v=true"]

    Returns:
        Dictionary mapping argument names to values
    """
    result: dict[str, str] = {}

    for arg in arg_candidates:
        if "=" in arg:
            # Handle --name=value or -name=value
            name_part, value = arg.split("=", 1)
            # Strip leading dashes to get the name
            name = name_part.lstrip("-")
            result[name] = value

    return result


def load_schema(schema_file: str) -> dict[str, dict[str, Any]]:
    """Load and validate the schema file.

    Args:
        schema_file: Path to the JSON schema file

    Returns:
        Parsed schema as a dictionary

    Raises:
        SystemExit: If file doesn't exist or JSON is invalid
    """
    try:
        with open(schema_file, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: Schema file missing or invalid: {schema_file}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: Cannot read schema file: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(schema, dict):
        print("Error: Schema must be a JSON object", file=sys.stderr)
        sys.exit(1)

    return schema


def validate_schema(schema: dict[str, dict[str, Any]]) -> None:
    """Validate the schema structure.

    Args:
        schema: The parsed schema dictionary

    Raises:
        SystemExit: If schema validation fails
    """
    valid_types = {"string", "integer", "float", "boolean"}
    source_fields = {"default", "env", "file", "arg"}

    for param_name, param_spec in schema.items():
        if not isinstance(param_spec, dict):
            print(f"Error: Parameter '{param_name}' must be an object", file=sys.stderr)
            sys.exit(1)

        # Check type field
        if "type" not in param_spec:
            print(f"Error: Parameter '{param_name}' missing required 'type' field", file=sys.stderr)
            sys.exit(1)

        param_type = param_spec["type"]
        if param_type not in valid_types:
            print(f"Error: Parameter '{param_name}' has invalid type '{param_type}'", file=sys.stderr)
            sys.exit(1)

        # Check source fields are strings
        for field in source_fields:
            if field in param_spec and not isinstance(param_spec[field], str):
                print(f"Error: Parameter '{param_name}' field '{field}' must be a string", file=sys.stderr)
                sys.exit(1)


def parse_value(value: str, expected_type: str, param_name: str, source: str) -> str:
    """Parse a value according to the expected type.

    Args:
        value: The raw value string to parse
        expected_type: The expected type (string, integer, float, boolean)
        param_name: Name of the parameter (for error messages)
        source: Source name (for error messages)

    Returns:
        The parsed value as a string

    Raises:
        SystemExit: If parsing fails
    """
    try:
        if expected_type == "string":
            return value

        elif expected_type == "integer":
            stripped = value.strip()
            if not stripped:
                raise ValueError("empty value")
            # Check for scientific notation
            if 'e' in stripped.lower() or 'E' in stripped:
                raise ValueError("scientific notation not supported")
            # Check if it's a valid integer
            if not stripped.lstrip('-').isdigit():
                raise ValueError("not a decimal integer")
            return stripped

        elif expected_type == "float":
            stripped = value.strip()
            if not stripped:
                raise ValueError("empty value")
            # Try to parse as float
            float(stripped)
            return stripped

        elif expected_type == "boolean":
            lower = value.strip().lower()
            if lower in ("true", "false"):
                return lower
            else:
                raise ValueError(f"must be 'true' or 'false', got '{value}'")

    except ValueError as e:
        error_reason = str(e) if str(e) else "invalid value"
        print(f"Error: Parameter '{param_name}' parse failure from source '{source}': {error_reason}", file=sys.stderr)
        sys.exit(1)

    # Should not reach here
    return value


def resolve_parameter(
    param_name: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str]
) -> str | None:
    """Resolve a parameter from its sources.

    Priority order: default (lowest), env, file, arg (highest).
    Check from highest to lowest priority; first one with a value wins.

    Args:
        param_name: Name of the parameter
        param_spec: Parameter specification dictionary
        arg_values: Parsed command-line argument values

    Returns:
        Resolved value as string, or None if unresolved

    Raises:
        SystemExit: If parsing fails
    """
    param_type = param_spec["type"]

    # 1. Check command-line argument (highest priority)
    if "arg" in param_spec:
        arg_name = param_spec["arg"]
        if arg_name in arg_values:
            arg_value = arg_values[arg_name]
            if arg_value:  # Non-empty
                value = parse_value(arg_value, param_type, param_name, "arg")
                return value

    # 2. Check file
    if "file" in param_spec:
        file_path = param_spec["file"]
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
        except (FileNotFoundError, OSError, IsADirectoryError):
            # File doesn't exist or can't be read, continue to next source
            pass
        else:
            stripped = file_content.strip()
            if stripped:
                value = parse_value(stripped, param_type, param_name, "file")
                return value

    # 3. Check environment variable
    if "env" in param_spec:
        env_value = os.environ.get(param_spec["env"])
        if env_value is not None and env_value.strip():
            value = parse_value(env_value, param_type, param_name, "env")
            return value

    # 4. Check default (lowest priority)
    if "default" in param_spec:
        value = parse_value(param_spec["default"], param_type, param_name, "default")
        return value

    # Unresolved
    return None


def main() -> None:
    """Main entry point."""
    schema_file, arg_candidates = parse_args(sys.argv[1:])
    arg_values = parse_arg_candidates(arg_candidates)

    schema = load_schema(schema_file)
    validate_schema(schema)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    for param_name in schema:
        param_spec = schema[param_name]
        result = resolve_parameter(param_name, param_spec, arg_values)

        if result is not None:
            resolved[param_name] = result
        else:
            unresolved.append(param_name)

    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Output resolved configuration as JSON
    print(json.dumps(resolved))
    sys.exit(0)


if __name__ == "__main__":
    main()
