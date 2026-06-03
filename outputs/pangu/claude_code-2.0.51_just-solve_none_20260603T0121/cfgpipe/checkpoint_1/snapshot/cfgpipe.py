#!/usr/bin/env python3
"""cfgpipe - Command-line configuration resolver."""

import json
import os
import sys
from typing import Any


def parse_type(value: str, param_type: str, param_name: str, source: str) -> str:
    """Parse a string value against the declared type. Returns the value as a properly formatted string.

    Raises ValueError with a descriptive message on parse failure.
    """
    value = value.strip()

    if param_type == "string":
        return value

    elif param_type == "integer":
        # Must be a decimal integer, no scientific notation
        try:
            int(value)  # Validate it's an integer
        except ValueError:
            raise ValueError(
                f"parameter '{param_name}': cannot parse value '{value}' from source '{source}' as integer"
            )
        return value

    elif param_type == "float":
        # Decimal inputs, no scientific notation required
        try:
            float(value)
        except ValueError:
            raise ValueError(
                f"parameter '{param_name}': cannot parse value '{value}' from source '{source}' as float"
            )
        return value

    elif param_type == "boolean":
        # Only accept common boolean string representations
        lower_value = value.lower()
        if lower_value in ("true", "false"):
            return lower_value
        raise ValueError(
            f"parameter '{param_name}': cannot parse value '{value}' from source '{source}' as boolean"
        )

    else:
        # Should not happen with valid schema, but handle gracefully
        raise ValueError(
            f"parameter '{param_name}': unknown type '{param_type}'"
        )


def resolve_parameter(
    param_name: str,
    param_decl: dict[str, Any],
    arg_candidates: dict[str, str]
) -> str | None:
    """Resolve a single parameter from its sources in priority order.

    Priority order: default, env, file, arg
    Highest to lowest: arg > file > env > default
    The first source that provides a value wins (checked from highest to lowest priority).
    Returns the resolved value as a string, or None if unresolved.
    Raises ValueError on parse failure.
    """
    param_type = param_decl.get("type")
    if not param_type:
        raise ValueError(f"parameter '{param_name}': missing required 'type' field")

    # Check sources from highest to lowest priority
    # 1. CLI argument (highest priority)
    if "arg" in param_decl:
        arg_name = param_decl["arg"]
        if not isinstance(arg_name, str):
            raise ValueError(
                f"parameter '{param_name}': 'arg' must be a string, got {type(arg_name).__name__}"
            )
        if arg_name in arg_candidates:
            # Check if value is non-empty after stripping
            arg_value = arg_candidates[arg_name].strip()
            if arg_value != "":
                return parse_type(arg_candidates[arg_name], param_type, param_name, "arg")

    # 2. File
    if "file" in param_decl:
        file_path = param_decl["file"]
        if not isinstance(file_path, str):
            raise ValueError(
                f"parameter '{param_name}': 'file' must be a string, got {type(file_path).__name__}"
            )
        try:
            with open(file_path, "r") as f:
                file_val = f.read()
        except (OSError, IOError):
            # File doesn't exist or can't be read - treat as absent
            file_val = None

        if file_val is not None:
            stripped = file_val.strip()
            if stripped != "":
                return parse_type(stripped, param_type, param_name, "file")

    # 3. Environment variable
    if "env" in param_decl:
        env_name = param_decl["env"]
        if not isinstance(env_name, str):
            raise ValueError(
                f"parameter '{param_name}': 'env' must be a string, got {type(env_name).__name__}"
            )
        env_val = os.environ.get(env_name)
        if env_val is not None and env_val.strip() != "":
            return parse_type(env_val, param_type, param_name, "env")

    # 4. Default (lowest priority)
    if "default" in param_decl:
        default_val = param_decl["default"]
        if not isinstance(default_val, str):
            raise ValueError(
                f"parameter '{param_name}': 'default' must be a string, got {type(default_val).__name__}"
            )
        # Default values should always be valid (non-empty)
        return parse_type(default_val, param_type, param_name, "default")

    # Unresolved
    return None


def validate_schema(schema: dict[str, Any]) -> list[str] | None:
    """Validate the schema structure. Returns None if valid, or a list of error messages."""
    errors = []

    if not isinstance(schema, dict):
        errors.append("Schema root must be an object")
        return errors

    for param_name, param_decl in schema.items():
        if not isinstance(param_decl, dict):
            errors.append(f"parameter '{param_name}': declaration must be an object")
            continue

        # Check type field exists and is a string
        if "type" not in param_decl:
            errors.append(f"parameter '{param_name}': missing required 'type' field")
        elif not isinstance(param_decl["type"], str):
            errors.append(
                f"parameter '{param_name}': 'type' must be a string, got {type(param_decl['type']).__name__}"
            )

        # Validate source fields are strings if present
        for source_field in ("default", "env", "file", "arg"):
            if source_field in param_decl and not isinstance(param_decl[source_field], str):
                errors.append(
                    f"parameter '{param_name}': '{source_field}' must be a string, got {type(param_decl[source_field]).__name__}"
                )

    return errors if errors else None


def parse_cli_arguments(arg_candidates: list[str]) -> dict[str, str]:
    """Parse CLI arguments in --name=value or -name=value format.

    Returns a dict mapping argument names to values. Last wins for duplicates.
    """
    result = {}
    for arg in arg_candidates:
        if "=" in arg:
            name_part, value = arg.split("=", 1)
            # Strip leading dashes
            while name_part.startswith("-"):
                name_part = name_part[1:]
            if name_part:
                result[name_part] = value
    return result


def load_schema(schema_path: str) -> dict[str, Any] | None:
    """Load and parse the schema file. Returns the schema dict or None on error."""
    try:
        with open(schema_path, "r") as f:
            content = f.read()
    except (OSError, IOError):
        return None

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    schema_path = sys.argv[1]
    cli_args = sys.argv[2:]

    # Load schema
    schema = load_schema(schema_path)
    if schema is None:
        print(f"Error: Cannot load or parse schema file '{schema_path}'", file=sys.stderr)
        return 1

    # Validate schema
    validation_errors = validate_schema(schema)
    if validation_errors:
        for error in validation_errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    # Parse CLI arguments
    arg_candidates = parse_cli_arguments(cli_args)

    # Resolve all parameters
    resolved = {}
    unresolved = []

    for param_name, param_decl in schema.items():
        try:
            value = resolve_parameter(param_name, param_decl, arg_candidates)
            if value is None:
                unresolved.append(param_name)
            else:
                resolved[param_name] = value
        except ValueError as e:
            # Parse failure halts resolution immediately
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Handle unresolved parameters
    if unresolved:
        if len(unresolved) == 1:
            print(f"Error: Unresolved parameter '{unresolved[0]}'", file=sys.stderr)
        else:
            params_str = "', '".join(unresolved)
            print(f"Error: Unresolved parameters '{params_str}'", file=sys.stderr)
        return 1

    # Success - output JSON
    output = json.dumps(resolved)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
