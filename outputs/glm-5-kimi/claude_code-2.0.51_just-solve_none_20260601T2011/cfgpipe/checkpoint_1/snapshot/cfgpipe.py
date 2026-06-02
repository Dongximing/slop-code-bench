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
from typing import Any, Dict, List, Optional, Tuple


def parse_boolean(value: str) -> bool:
    """Parse a boolean string representation."""
    true_values = {'true', 'yes', '1', 'on', 'enabled'}
    false_values = {'false', 'no', '0', 'off', 'disabled'}

    lower_value = value.lower().strip()
    if lower_value in true_values:
        return True
    elif lower_value in false_values:
        return False
    else:
        raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    """Parse an integer string representation."""
    stripped = value.strip()
    # Check for valid decimal integer (no scientific notation)
    # Allow optional leading sign
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as integer")

    # Check for scientific notation
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for integer: '{value}'")

    try:
        # Try parsing as int
        return int(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as integer")


def parse_float(value: str) -> float:
    """Parse a float string representation."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as float")
    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def parse_value(value: str, type_name: str) -> Any:
    """Parse a value according to the declared type."""
    if type_name == 'string':
        return value
    elif type_name == 'integer':
        return parse_integer(value)
    elif type_name == 'float':
        return parse_float(value)
    elif type_name == 'boolean':
        return parse_boolean(value)
    else:
        raise ValueError(f"Unknown type: {type_name}")


def format_value(value: Any, type_name: str) -> str:
    """Format a parsed value as a string for output."""
    if type_name == 'boolean':
        return 'true' if value else 'false'
    elif type_name == 'float':
        # Python's default str() for floats is acceptable
        return str(value)
    elif type_name == 'integer':
        return str(value)
    else:
        return str(value)


def read_file_content(file_path: str) -> Optional[str]:
    """
    Read file content if it exists and is a regular file.
    Returns None if file doesn't exist, is a directory, or is empty after trim.
    """
    try:
        if not os.path.isfile(file_path):
            return None
        with open(file_path, 'r') as f:
            content = f.read()
        trimmed = content.strip()
        if not trimmed:
            return None
        return content
    except (IOError, OSError):
        return None


def match_arg(arg_name: str, cli_args: List[str]) -> Optional[str]:
    """
    Match an argument from CLI args.
    Supports --name=value and -name=value formats.
    Last match wins.
    """
    result = None
    for arg in cli_args:
        # Check for --name=value format
        if arg.startswith('--'):
            remaining = arg[2:]
            if '=' in remaining:
                name, value = remaining.split('=', 1)
                if name == arg_name:
                    result = value
        # Check for -name=value format
        elif arg.startswith('-'):
            remaining = arg[1:]
            if '=' in remaining:
                name, value = remaining.split('=', 1)
                if name == arg_name:
                    result = value
    return result


def validate_schema(schema: Dict[str, Any]) -> List[str]:
    """
    Validate the schema structure.
    Returns a list of error messages, empty if valid.
    """
    errors = []

    if not isinstance(schema, dict):
        errors.append("Schema root must be an object")
        return errors

    seen_names = set()

    for name, declaration in schema.items():
        if name in seen_names:
            errors.append(f"Duplicate parameter name: {name}")
        seen_names.add(name)

        if not isinstance(declaration, dict):
            errors.append(f"Parameter '{name}' declaration must be an object")
            continue

        # Check required 'type' field
        if 'type' not in declaration:
            errors.append(f"Parameter '{name}' missing required 'type' field")
        else:
            type_name = declaration['type']
            if type_name not in ('string', 'integer', 'float', 'boolean'):
                errors.append(f"Parameter '{name}' has unrecognized type: {type_name}")

        # Check that all source fields are strings
        for source_field in ('default', 'env', 'file', 'arg'):
            if source_field in declaration:
                if not isinstance(declaration[source_field], str):
                    errors.append(f"Parameter '{name}' has non-string '{source_field}' field")

    return errors


def resolve_parameter(
    name: str,
    declaration: Dict[str, Any],
    cli_args: List[str]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve a single parameter.

    Returns: (resolved_value, source_name, error_message)
    - If resolved: (formatted_value, source_name, None)
    - If error: (None, source_name, error_message)
    - If unresolved: (None, None, None)
    """
    type_name = declaration.get('type', 'string')

    # Define sources in priority order: default (lowest) -> arg (highest).
    # The highest-priority source that provides a value wins.
    sources = [
        ('arg', lambda: match_arg(declaration['arg'], cli_args) if 'arg' in declaration else None),
        ('file', lambda: read_file_content(declaration['file']) if 'file' in declaration else None),
        ('env', lambda: os.environ.get(declaration['env']) if 'env' in declaration else None),
        ('default', lambda: declaration.get('default') if 'default' in declaration else None),
    ]

    for source_name, get_value in sources:
        raw_value = get_value()

        if raw_value is None:
            continue

        # Try to parse the value
        try:
            parsed_value = parse_value(raw_value, type_name)
            formatted_value = format_value(parsed_value, type_name)
            return (formatted_value, source_name, None)
        except ValueError as e:
            return (None, source_name, str(e))

    return (None, None, None)


def main() -> int:
    """Main entry point."""
    # Parse arguments
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    schema_file = sys.argv[1]
    cli_args = sys.argv[2:]

    # Read and parse schema file
    if not os.path.isfile(schema_file):
        print(f"Error: Schema file not found: {schema_file}", file=sys.stderr)
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

    # Validate schema
    validation_errors = validate_schema(schema)
    if validation_errors:
        for error in validation_errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    # Resolve each parameter
    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for name, declaration in schema.items():
        value, source, error = resolve_parameter(name, declaration, cli_args)

        if error is not None:
            print(f"Error: Failed to parse parameter '{name}' from source '{source}': {error}", file=sys.stderr)
            return 1

        if value is not None:
            resolved[name] = value
        else:
            unresolved.append(name)

    # Check for unresolved parameters
    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(sorted(unresolved))}", file=sys.stderr)
        return 1

    # Output resolved configuration
    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
