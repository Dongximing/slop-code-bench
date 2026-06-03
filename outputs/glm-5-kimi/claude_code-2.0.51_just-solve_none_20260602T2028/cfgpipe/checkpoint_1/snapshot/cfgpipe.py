#!/usr/bin/env python3
"""cfgpipe - Core Resolution

A command-line configuration resolver that reads a JSON schema document,
resolves parameters from local sources, and outputs resolved configuration.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


class ConfigError(Exception):
    """Base exception for configuration errors."""
    pass


class SchemaError(ConfigError):
    """Error in schema loading or validation."""
    pass


class ParseError(ConfigError):
    """Error parsing a value against its declared type."""
    def __init__(self, param_name: str, source: str, reason: str):
        self.param_name = param_name
        self.source = source
        self.reason = reason
        super().__init__(f"Failed to parse parameter '{param_name}' from {source}: {reason}")


class UnresolvedError(ConfigError):
    """Error for parameters that could not be resolved."""
    def __init__(self, param_names: List[str]):
        self.param_names = param_names
        super().__init__(f"Unresolved parameters: {', '.join(sorted(param_names))}")


def parse_boolean(value: str) -> str:
    """Parse a boolean string representation and return canonical form.

    Accepts common boolean string representations and returns lowercase 'true' or 'false'.
    """
    true_values = {'true', 'yes', '1', 'on', 'enabled'}
    false_values = {'false', 'no', '0', 'off', 'disabled'}

    lower_value = value.lower().strip()

    if lower_value in true_values:
        return 'true'
    elif lower_value in false_values:
        return 'false'
    else:
        raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> str:
    """Parse an integer string and return canonical form.

    Only accepts decimal integers, no scientific notation.
    """
    stripped = value.strip()
    # Check for scientific notation
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not supported for integer: '{value}'")

    # Validate it's a proper integer format
    try:
        parsed = int(stripped)
        # Ensure it wasn't parsed from a float-like string
        if '.' in stripped:
            raise ValueError(f"Float-like value not accepted as integer: '{value}'")
        return str(parsed)
    except ValueError as e:
        raise ValueError(f"Cannot parse '{value}' as integer: {e}")


def parse_float(value: str) -> str:
    """Parse a float string and return canonical form.

    Accepts decimal inputs.
    """
    stripped = value.strip()
    try:
        parsed = float(stripped)
        return str(parsed)
    except ValueError as e:
        raise ValueError(f"Cannot parse '{value}' as float: {e}")


def parse_string(value: str) -> str:
    """Parse a string value (returns as-is)."""
    return value


def parse_value(value: str, type_name: str, param_name: str, source: str) -> str:
    """Parse a value according to its declared type.

    Args:
        value: The string value to parse
        type_name: The declared type (string, integer, float, boolean)
        param_name: Parameter name for error messages
        source: Source name for error messages

    Returns:
        The canonical string representation of the parsed value

    Raises:
        ParseError: If parsing fails
    """
    parsers = {
        'string': parse_string,
        'integer': parse_integer,
        'float': parse_float,
        'boolean': parse_boolean,
    }

    if type_name not in parsers:
        raise ParseError(param_name, source, f"Unknown type '{type_name}'")

    try:
        return parsers[type_name](value)
    except ValueError as e:
        raise ParseError(param_name, source, str(e))


def load_schema(schema_path: str) -> Dict[str, Any]:
    """Load and validate the schema document.

    Args:
        schema_path: Path to the JSON schema file

    Returns:
        The parsed schema dictionary

    Raises:
        SchemaError: If the file is missing, invalid JSON, or schema validation fails
    """
    # Check file exists
    if not os.path.exists(schema_path):
        raise SchemaError(f"Schema file not found: {schema_path}")

    # Parse JSON
    try:
        with open(schema_path, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise SchemaError(f"Invalid JSON in schema file: {e}")
    except IOError as e:
        raise SchemaError(f"Cannot read schema file: {e}")

    # Validate root is an object
    if not isinstance(schema, dict):
        raise SchemaError("Schema root must be an object")

    # Validate parameter names are unique (they are by virtue of being dict keys)
    # Validate each parameter declaration
    for param_name, declaration in schema.items():
        if not isinstance(declaration, dict):
            raise SchemaError(f"Parameter '{param_name}' declaration must be an object")

        # Check required 'type' field
        if 'type' not in declaration:
            raise SchemaError(f"Parameter '{param_name}' missing required 'type' field")

        # Validate source fields are strings if present
        source_fields = ['default', 'env', 'file', 'arg']
        for field in source_fields:
            if field in declaration:
                if not isinstance(declaration[field], str):
                    raise SchemaError(f"Parameter '{param_name}' field '{field}' must be a string")

    return schema


def parse_cli_args(arg_candidates: List[str]) -> Dict[str, str]:
    """Parse CLI argument candidates into a dictionary.

    Supports --name=value and -name=value formats.
    Last occurrence wins for duplicate names.

    Args:
        arg_candidates: List of argument strings (after schema file path)

    Returns:
        Dictionary mapping argument names to values
    """
    result = {}

    for arg in arg_candidates:
        # Support --name=value and -name=value
        if arg.startswith('--') and '=' in arg:
            name, value = arg[2:].split('=', 1)
            result[name] = value
        elif arg.startswith('-') and '=' in arg:
            name, value = arg[1:].split('=', 1)
            result[name] = value

    return result


def resolve_from_file(file_path: str) -> Optional[str]:
    """Resolve a value from a file.

    Args:
        file_path: Path to the file

    Returns:
        The file content if available, None otherwise
    """
    # Check if path exists
    if not os.path.exists(file_path):
        return None

    # Check if it's a regular file (not a directory)
    if not os.path.isfile(file_path):
        return None

    # Try to read the file
    try:
        with open(file_path, 'r') as f:
            content = f.read()
    except (IOError, OSError):
        return None

    # Trim surrounding whitespace
    trimmed = content.strip()

    # Empty after trimming counts as absent
    if not trimmed:
        return None

    return trimmed


def resolve_parameter(
    param_name: str,
    declaration: Dict[str, Any],
    cli_args: Dict[str, str]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a single parameter from its declared sources.

    Priority order (highest to lowest): arg, file, env, default.
    The first source that provides a value wins.

    Args:
        param_name: Name of the parameter
        declaration: Parameter declaration from schema
        cli_args: Parsed CLI arguments

    Returns:
        Tuple of (resolved_value, source_name) or (None, None) if unresolved

    Raises:
        ParseError: If a source provides a value that fails to parse
    """
    param_type = declaration['type']

    # Try arg (highest priority)
    if 'arg' in declaration:
        arg_name = declaration['arg']
        arg_value = cli_args.get(arg_name)
        if arg_value is not None:
            try:
                parsed = parse_value(arg_value, param_type, param_name, 'arg')
                return parsed, 'arg'
            except ParseError:
                raise

    # Try file
    if 'file' in declaration:
        file_path = declaration['file']
        file_value = resolve_from_file(file_path)
        if file_value is not None:
            try:
                parsed = parse_value(file_value, param_type, param_name, 'file')
                return parsed, 'file'
            except ParseError:
                raise

    # Try env
    if 'env' in declaration:
        env_var = declaration['env']
        env_value = os.environ.get(env_var)
        if env_value is not None:
            try:
                parsed = parse_value(env_value, param_type, param_name, 'env')
                return parsed, 'env'
            except ParseError:
                raise

    # Try default (lowest priority)
    if 'default' in declaration:
        value = declaration['default']
        try:
            parsed = parse_value(value, param_type, param_name, 'default')
            return parsed, 'default'
        except ParseError:
            raise

    # No source provided a value
    return None, None


def resolve_all_parameters(
    schema: Dict[str, Any],
    cli_args: Dict[str, str]
) -> Dict[str, str]:
    """Resolve all parameters from the schema.

    Args:
        schema: The validated schema dictionary
        cli_args: Parsed CLI arguments

    Returns:
        Dictionary mapping parameter names to resolved string values

    Raises:
        ParseError: If any parameter fails to parse
        UnresolvedError: If any parameters remain unresolved
    """
    result = {}
    unresolved = []

    for param_name, declaration in schema.items():
        value, source = resolve_parameter(param_name, declaration, cli_args)

        if value is not None:
            result[param_name] = value
        else:
            unresolved.append(param_name)

    if unresolved:
        raise UnresolvedError(unresolved)

    return result


def main() -> int:
    """Main entry point for cfgpipe.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check for minimum arguments
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        print("Usage: python cfgpipe.py <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    schema_path = sys.argv[1]
    arg_candidates = sys.argv[2:]

    # Load and validate schema
    try:
        schema = load_schema(schema_path)
    except SchemaError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Parse CLI arguments
    cli_args = parse_cli_args(arg_candidates)

    # Resolve all parameters
    try:
        resolved = resolve_all_parameters(schema, cli_args)
    except ParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except UnresolvedError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Output resolved configuration as JSON
    print(json.dumps(resolved))

    return 0


if __name__ == '__main__':
    sys.exit(main())
