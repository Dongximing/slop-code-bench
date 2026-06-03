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
import urllib.parse
from typing import Any, Optional

import requests


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


def parse_port(value: str) -> int:
    """Parse a port number (0-65535) from string."""
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as port")
    # Check for leading zeros except for '0' itself
    if len(stripped) > 1 and stripped[0] == '0':
        raise ValueError(f"Cannot parse '{value}' as port: invalid leading zeros")
    if not stripped.isdigit():
        raise ValueError(f"Cannot parse '{value}' as port: not a valid integer")
    port_num = int(stripped)
    if port_num < 0 or port_num > 65535:
        raise ValueError(f"Port {port_num} out of valid range 0-65535")
    return port_num


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
    if type_name in ('integer', 'float', 'port'):
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
    if type_name == 'port':
        return parse_port(value)
    raise ValueError(f"Unknown type: {type_name}")


# Valid type names
BUILTIN_TYPES = ('string', 'integer', 'float', 'boolean')
CUSTOM_TYPES = ('port',)
ALL_VALID_TYPES = BUILTIN_TYPES + CUSTOM_TYPES

# Source annotation keys
SOURCE_ANNOTATIONS = ('default', 'env', 'file', 'arg', 'primary-store')


def load_schema(schema_path: str) -> dict:
    """Load and validate the schema file."""
    if not os.path.isfile(schema_path):
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        with open(schema_path, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in schema file: {e}")

    return schema


def validate_schema_recursive(
    node: Any,
    path: str,
    primary_store_map: dict
) -> list:
    """
    Recursively validate a schema node.

    Returns a list of error messages (empty if valid).

    A parameter declaration has a string 'type' field.
    A group is an object without a string 'type' field.
    """
    errors = []

    # Node must be an object
    if not isinstance(node, dict):
        errors.append(f"Schema node at '{path}' must be an object")
        return errors

    # Check if this is a parameter declaration (has string 'type' field)
    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration
        type_name = node['type']

        # Validate type is recognized
        if type_name not in ALL_VALID_TYPES:
            errors.append(f"Parameter '{path}' has unrecognized type: {type_name}")
            return errors

        # Validate source annotations are strings
        for source in SOURCE_ANNOTATIONS:
            if source in node and not isinstance(node[source], str):
                errors.append(f"Parameter '{path}' source '{source}' must be a string")

        # Track primary-store for duplicate detection
        if 'primary-store' in node:
            key = node['primary-store']
            if key not in primary_store_map:
                primary_store_map[key] = []
            primary_store_map[key].append(path)
    else:
        # This is a group - validate it doesn't have source annotations with non-object values
        # and that all children are objects
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key

            # Check for source annotations at group level (not allowed with non-object values)
            if key in SOURCE_ANNOTATIONS:
                # Source annotations are only valid on parameters, not groups
                # If the value is not an object, it's an error
                if not isinstance(value, dict):
                    errors.append(f"Group '{path}' has invalid annotation '{key}'")
                    continue

            # Children must be objects
            if not isinstance(value, dict):
                errors.append(f"Entry '{child_path}' must be an object")
                continue

            # Check if child has a string 'type' - then it's a parameter
            # Otherwise, it's a nested group
            if 'type' in value and isinstance(value['type'], str):
                # This is a parameter declaration - validate it
                child_errors = validate_schema_recursive(value, child_path, primary_store_map)
                errors.extend(child_errors)
            else:
                # This is a nested group
                child_errors = validate_schema_recursive(value, child_path, primary_store_map)
                errors.extend(child_errors)

    return errors


def validate_schema(schema: dict) -> None:
    """
    Validate the entire schema structure.

    Raises ValueError with appropriate error message on failure.
    """
    # Root must be a non-empty JSON object
    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")

    if len(schema) == 0:
        raise ValueError("Schema root must be a non-empty object")

    # Track primary-store keys across all parameters
    primary_store_map = {}

    # Validate recursively
    errors = validate_schema_recursive(schema, "", primary_store_map)

    if errors:
        # Report any one error
        raise ValueError(errors[0])

    # Check for duplicate primary-store keys
    duplicate_errors = []
    for key, paths in primary_store_map.items():
        if len(paths) > 1:
            duplicate_errors.append((key, sorted(paths)))

    if duplicate_errors:
        # Report the first duplicate found
        key, paths = duplicate_errors[0]
        paths_str = ', '.join(paths)
        raise ValueError(f"Duplicate primary-store key '{key}' used by parameters: {paths_str}")


def collect_parameters(schema: dict, path: str = "") -> list:
    """
    Collect all parameter declarations from the schema.

    Returns a list of (composed_path, param_decl) tuples.
    """
    params = []

    for key, value in schema.items():
        child_path = f"{path}.{key}" if path else key

        if 'type' in value and isinstance(value['type'], str):
            # This is a parameter
            params.append((child_path, value))
        else:
            # This is a group - recurse
            params.extend(collect_parameters(value, child_path))

    return params


def parse_global_flags(args: list) -> tuple[dict, list]:
    """
    Parse global flags from arguments.
    Returns: (global_flags_dict, remaining_args)
    """
    global_flags = {}
    remaining_args = []
    i = 0

    while i < len(args):
        arg = args[i]
        if arg == '--primary-store':
            if i + 1 >= len(args):
                raise ValueError("--primary-store requires a value")
            global_flags['primary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--primary-store='):
            global_flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        else:
            remaining_args.append(arg)
            i += 1

    return global_flags, remaining_args


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


def lookup_primary_store(base_url: str, key: str) -> tuple[Optional[str], Optional[str]]:
    """
    Look up a key in the primary store.
    Returns: (value, error_message)
    - On success: (value_string, None)
    - On missing key: (None, None)
    - On error: (None, error_message)
    """
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            try:
                data = response.json()
                if data.get('found') is True:
                    return (data.get('value'), None)
                return (None, None)
            except (json.JSONDecodeError, ValueError) as e:
                return (None, f"Malformed response from primary store: {e}")
        elif response.status_code == 404:
            try:
                data = response.json()
                if data.get('found') is False:
                    return (None, None)
                return (None, f"Unexpected 404 response from primary store")
            except (json.JSONDecodeError, ValueError):
                return (None, f"Malformed 404 response from primary store")
        else:
            return (None, f"Primary store returned status {response.status_code}")
    except requests.exceptions.Timeout:
        return (None, "Primary store request timed out")
    except requests.exceptions.ConnectionError as e:
        return (None, f"Failed to connect to primary store: {e}")
    except requests.exceptions.RequestException as e:
        return (None, f"Primary store request failed: {e}")


def resolve_from_sources(
    param_path: str,
    param_decl: dict,
    cli_args: dict,
    primary_store_url: Optional[str]
) -> tuple[bool, Any, str, str]:
    """
    Resolve a parameter from its sources.
    Priority order (highest to lowest): arg, primary-store, file, env, default
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

    # Try primary-store (second highest priority)
    if 'primary-store' in param_decl and primary_store_url:
        key = param_decl['primary-store']
        value, error = lookup_primary_store(primary_store_url, key)

        if error is not None:
            # Connector failure - fatal
            return (False, None, 'primary-store', error)

        if value is not None:
            # Key found - try to parse
            try:
                parsed = parse_value(value, type_name)
                return (True, parsed, 'primary-store', value)
            except ValueError as e:
                return (False, None, 'primary-store', str(e))

        # Key not found - fall through to next source

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


def build_output_structure(resolved_params: dict, schema: dict) -> dict:
    """
    Build the nested output structure from resolved parameters.

    resolved_params: dict mapping composed_path -> formatted_value
    schema: the original schema

    Returns: nested dict matching schema structure
    """
    result = {}

    for key, value in schema.items():
        child_path = key

        if 'type' in value and isinstance(value['type'], str):
            # This is a parameter - use resolved value
            result[key] = resolved_params[child_path]
        else:
            # This is a group - recurse
            result[key] = build_output_structure(resolved_params, value)

    return result


def build_output_structure_with_path(resolved_params: dict, path_prefix: str, schema: dict) -> dict:
    """
    Build the nested output structure from resolved parameters.

    resolved_params: dict mapping composed_path -> formatted_value
    path_prefix: current path prefix for resolving
    schema: the current schema node

    Returns: nested dict matching schema structure
    """
    result = {}

    for key, value in schema.items():
        child_path = f"{path_prefix}.{key}" if path_prefix else key

        if 'type' in value and isinstance(value['type'], str):
            # This is a parameter - use resolved value
            result[key] = resolved_params[child_path]
        else:
            # This is a group - recurse
            result[key] = build_output_structure_with_path(resolved_params, child_path, value)

    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    # Parse global flags
    try:
        global_flags, remaining_args = parse_global_flags(sys.argv[1:])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if len(remaining_args) < 1:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    schema_path = remaining_args[0]
    arg_candidates = remaining_args[1:]

    primary_store_url = global_flags.get('primary-store')

    # Load schema
    try:
        schema = load_schema(schema_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate schema (including nested structure and duplicate primary-store)
    try:
        validate_schema(schema)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Collect all parameters with their composed paths
    params = collect_parameters(schema)

    # Check if any parameter requires primary-store but it's not configured
    params_with_primary_store = [
        path for path, decl in params
        if 'primary-store' in decl
    ]

    if params_with_primary_store and not primary_store_url:
        param_list = ', '.join(sorted(params_with_primary_store))
        print(f"Error: Parameter(s) {param_list} declare primary-store but --primary-store is not configured", file=sys.stderr)
        sys.exit(1)

    # Parse CLI args for arg matching
    cli_args = parse_cli_args(arg_candidates)

    # Resolve each parameter
    resolved = {}
    unresolved = []

    for param_path, param_decl in params:
        found, parsed_value, source_type, raw_or_error = resolve_from_sources(
            param_path, param_decl, cli_args, primary_store_url
        )

        if found:
            resolved[param_path] = format_value(parsed_value, param_decl['type'])
        elif source_type is not None:
            print(f"Error: Parameter '{param_path}' failed to parse from {source_type}: {raw_or_error}", file=sys.stderr)
            sys.exit(1)
        else:
            unresolved.append(param_path)

    # Check for unresolved parameters
    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Build nested output structure
    output = build_output_structure_with_path(resolved, "", schema)

    # Output resolved configuration
    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()