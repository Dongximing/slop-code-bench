#!/usr/bin/env python3
"""cfgpipe - Core Resolution

A command-line configuration resolver that reads a JSON schema document,
resolves parameters from local sources, and outputs resolved configuration.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode


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


class PrimaryStoreError(ConfigError):
    """Error for primary-store related issues."""
    pass


class PrimaryStoreConfigError(ConfigError):
    """Error when primary-store is used without configuration."""
    pass


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


def parse_port(value: str) -> str:
    """Parse a port value (0-65535) and return canonical form.

    Accepts base-10 integer strings in range 0-65535.
    Returns plain decimal with no leading zeros except '0'.
    """
    stripped = value.strip()

    # Reject empty strings
    if not stripped:
        raise ValueError("empty value")

    # Check for negative
    if stripped[0] == '-':
        raise ValueError("negative value")

    # Must be all digits
    if not stripped.isdigit():
        raise ValueError("not a valid integer")

    # Check for leading zeros (except '0' itself)
    if len(stripped) > 1 and stripped[0] == '0':
        raise ValueError("leading zeros not allowed")

    # Parse and check range
    port_num = int(stripped)
    if port_num > 65535:
        raise ValueError("value out of range 0-65535")
    return str(port_num)


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
        'port': parse_port,
    }

    # Valid types (built-in and custom)
    valid_types = {'string', 'integer', 'float', 'boolean', 'port'}

    if type_name not in valid_types:
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

    # Validate root is a non-empty object
    if not isinstance(schema, dict):
        raise SchemaError("Schema root must be an object")
    if len(schema) == 0:
        raise SchemaError("Schema root must be a non-empty object")

    # Validate the schema structure recursively
    _validate_schema_node(schema, "", set())

    return schema


def _validate_schema_node(
    node: Dict[str, Any],
    path: str,
    primary_store_keys: set
) -> None:
    """Recursively validate a schema node (group or parameter).

    Args:
        node: The schema node to validate
        path: The composed path to this node
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates

    Raises:
        SchemaError: If validation fails
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store'}

    # Check if this node has a string-valued 'type' field making it a parameter declaration
    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration
        _validate_parameter(node, path, primary_store_keys)
    else:
        # This is a group
        _validate_group(node, path, primary_store_keys, source_annotations)


def _validate_parameter(
    declaration: Dict[str, Any],
    path: str,
    primary_store_keys: set
) -> None:
    """Validate a parameter declaration.

    Args:
        declaration: The parameter declaration
        path: The composed path to this parameter
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates

    Raises:
        SchemaError: If validation fails
    """
    # Type is guaranteed to be a string by _validate_schema_node

    # Validate type value
    valid_types = {'string', 'integer', 'float', 'boolean', 'port'}
    param_type = declaration['type']
    if param_type not in valid_types:
        raise SchemaError(f"Parameter '{path}' has unrecognized type '{param_type}'")

    # Validate source fields are strings if present
    source_fields = ['default', 'env', 'file', 'arg']
    for field in source_fields:
        if field in declaration:
            if not isinstance(declaration[field], str):
                raise SchemaError(f"Parameter '{path}' field '{field}' must be a string")

    # Validate primary-store field
    if 'primary-store' in declaration:
        if not isinstance(declaration['primary-store'], str):
            raise SchemaError(f"Parameter '{path}' field 'primary-store' must be a string")
        key = declaration['primary-store']
        # Check for duplicates
        for existing_key, existing_path in primary_store_keys:
            if existing_key == key:
                raise SchemaError(
                    f"Duplicate primary-store key '{key}' used by parameters "
                    f"'{existing_path}' and '{path}'"
                )
        primary_store_keys.add((key, path))


def _validate_group(
    group: Dict[str, Any],
    path: str,
    primary_store_keys: set,
    source_annotations: set
) -> None:
    """Validate a group (container without a parameter value).

    Args:
        group: The group to validate
        path: The composed path to this group
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates
        source_annotations: Set of source annotation keys

    Raises:
        SchemaError: If validation fails
    """
    # Groups must not have source annotations with non-object values
    for key in source_annotations:
        if key in group and not isinstance(group[key], dict):
            raise SchemaError(
                f"Group '{path}' carries source annotation '{key}'"
            )

    # Every entry in a group must be an object
    for name, child in group.items():
        # Skip source annotations that are objects (they're not children)
        if name in source_annotations and isinstance(child, dict):
            continue

        if not isinstance(child, dict):
            raise SchemaError(
                f"Entry '{name}' in group '{path if path else 'root'}' must be an object"
            )

        # Build the composed path
        if path:
            child_path = f"{path}.{name}"
        else:
            child_path = name

        # Recursively validate the child
        _validate_schema_node(child, child_path, primary_store_keys)


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
    cli_args: Dict[str, str],
    primary_store_url: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a single parameter from its declared sources.

    Priority order (highest to lowest): arg, primary-store, file, env, default.
    The first source that provides a value wins.

    Args:
        param_name: Name of the parameter
        declaration: Parameter declaration from schema
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL

    Returns:
        Tuple of (resolved_value, source_name) or (None, None) if unresolved

    Raises:
        ParseError: If a source provides a value that fails to parse
        PrimaryStoreError: If primary-store lookup fails
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

    # Try primary-store
    if 'primary-store' in declaration and primary_store_url:
        key = declaration['primary-store']
        try:
            value, found = lookup_primary_store(primary_store_url, key, param_name)
            if found and value is not None:
                try:
                    parsed = parse_value(value, param_type, param_name, 'primary-store')
                    return parsed, 'primary-store'
                except ParseError:
                    raise
        except PrimaryStoreError:
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
    cli_args: Dict[str, str],
    primary_store_url: Optional[str] = None
) -> Dict[str, Any]:
    """Resolve all parameters from the schema.

    Args:
        schema: The validated schema dictionary
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL

    Returns:
        Nested dictionary mirroring the schema group hierarchy with resolved values

    Raises:
        ParseError: If any parameter fails to parse
        UnresolvedError: If any parameters remain unresolved
        PrimaryStoreError: If primary-store lookup fails
    """
    result: Dict[str, Any] = {}
    unresolved: List[str] = []

    _resolve_schema_node(schema, "", result, unresolved, cli_args, primary_store_url)

    if unresolved:
        raise UnresolvedError(unresolved)

    return result


def _resolve_schema_node(
    node: Dict[str, Any],
    path: str,
    result: Dict[str, Any],
    unresolved: List[str],
    cli_args: Dict[str, str],
    primary_store_url: Optional[str]
) -> None:
    """Recursively resolve a schema node (group or parameter).

    Args:
        node: The schema node to resolve
        path: The composed path to this node
        result: The result dictionary to populate
        unresolved: List to collect unresolved parameter paths
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store'}

    # Check if this node has a string-valued 'type' field making it a parameter declaration
    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration - resolve it
        value, source = resolve_parameter(path, node, cli_args, primary_store_url)
        if value is not None:
            # The result dict is actually the parent group's dict
            # We need to set the value in the parent's result
            # This case should not happen as we handle it differently
            pass
        else:
            unresolved.append(path)
    else:
        # This is a group - process children
        for name, child in node.items():
            # Skip source annotations that are objects
            if name in source_annotations and isinstance(child, dict):
                continue

            # Build the composed path
            if path:
                child_path = f"{path}.{name}"
            else:
                child_path = name

            # Check if child is a parameter or group
            if 'type' in child and isinstance(child['type'], str):
                # This is a parameter declaration
                value, source = resolve_parameter(child_path, child, cli_args, primary_store_url)
                if value is not None:
                    result[name] = value
                else:
                    unresolved.append(child_path)
            else:
                # This is a group - create nested object
                result[name] = {}
                _resolve_schema_node(
                    child, child_path, result[name], unresolved,
                    cli_args, primary_store_url
                )


def parse_global_flags(args: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """Parse global flags from command-line arguments.

    Global flags must precede the schema file path.
    Supported flags: --primary-store <base-url>

    Args:
        args: List of command-line arguments (excluding program name)

    Returns:
        Tuple of (global_flags_dict, remaining_args)
        global_flags_dict contains keys like 'primary-store'
        remaining_args are the args after global flags (schema file and arg-candidates)
    """
    global_flags = {}
    remaining = []
    i = 0

    while i < len(args):
        arg = args[i]

        # Check for --primary-store
        if arg == '--primary-store':
            if i + 1 < len(args):
                global_flags['primary-store'] = args[i + 1]
                i += 2
            else:
                # Missing value for --primary-store
                raise ValueError("Error: --primary-store requires a base-url argument")
        elif arg.startswith('--primary-store='):
            # Support --primary-store=value format
            global_flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        else:
            # Non-global-flag argument, stop processing global flags
            remaining = args[i:]
            break

    return global_flags, remaining


def lookup_primary_store(base_url: str, key: str, param_name: str) -> Tuple[Optional[str], bool]:
    """Look up a key in the primary store.

    Seed lookup: GET <base-url>/v1/primary/kv?key=<url-encoded-key>

    Args:
        base_url: The base URL of the primary store
        key: The key to look up
        param_name: Parameter name for error messages

    Returns:
        Tuple of (value, found) where:
        - value is the string value if found, None otherwise
        - found is True if key exists, False if key is missing

    Raises:
        PrimaryStoreError: If the request fails (non-200, malformed response, network error)
    """
    # Construct the URL
    endpoint = f"{base_url.rstrip('/')}/v1/primary/kv"
    url = f"{endpoint}?{urlencode({'key': key})}"

    try:
        request = urllib.request.Request(url, method='GET')
        request.add_header('Accept', 'application/json')

        with urllib.request.urlopen(request, timeout=30) as response:
            status_code = response.status
            body = response.read().decode('utf-8')

            if status_code == 200:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as e:
                    raise PrimaryStoreError(
                        f"Failed to parse primary-store response for parameter '{param_name}': {e}"
                    )

                # Validate response structure
                if not isinstance(data, dict):
                    raise PrimaryStoreError(
                        f"Malformed primary-store response for parameter '{param_name}': "
                        "expected JSON object"
                    )

                if 'found' not in data:
                    raise PrimaryStoreError(
                        f"Malformed primary-store response for parameter '{param_name}': "
                        "missing 'found' field"
                    )

                if data['found']:
                    if 'value' not in data:
                        raise PrimaryStoreError(
                            f"Malformed primary-store response for parameter '{param_name}': "
                            "missing 'value' field for found key"
                        )
                    # Return the string value
                    return str(data['value']), True
                else:
                    # Key not found
                    return None, False

            else:
                # Non-200 status
                raise PrimaryStoreError(
                    f"Primary-store request failed for parameter '{param_name}': "
                    f"HTTP {status_code}"
                )

    except urllib.error.HTTPError as e:
        # HTTP error response
        if e.code == 404:
            # Try to parse the 404 response body
            try:
                body = e.read().decode('utf-8')
                data = json.loads(body)
                if isinstance(data, dict) and 'found' in data and not data['found']:
                    # Proper 404 response indicating key not found
                    return None, False
                else:
                    # Malformed 404 response
                    raise PrimaryStoreError(
                        f"Malformed primary-store 404 response for parameter '{param_name}'"
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Malformed 404 response
                raise PrimaryStoreError(
                    f"Malformed primary-store 404 response for parameter '{param_name}'"
                )
        else:
            # Other HTTP error
            raise PrimaryStoreError(
                f"Primary-store request failed for parameter '{param_name}': "
                f"HTTP {e.code}"
            )

    except urllib.error.URLError as e:
        # Network error
        raise PrimaryStoreError(
            f"Primary-store network error for parameter '{param_name}': {e.reason}"
        )

    except TimeoutError:
        raise PrimaryStoreError(
            f"Primary-store timeout for parameter '{param_name}'"
        )


def main() -> int:
    """Main entry point for cfgpipe.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Check for minimum arguments
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        print("Global flags:", file=sys.stderr)
        print("  --primary-store <base-url>  Configure primary-store base URL", file=sys.stderr)
        return 1

    # Parse global flags (they come before schema file)
    try:
        global_flags, remaining_args = parse_global_flags(sys.argv[1:])
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    if len(remaining_args) < 1:
        print("Error: Missing schema file argument", file=sys.stderr)
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    schema_path = remaining_args[0]
    arg_candidates = remaining_args[1:]

    # Load and validate schema
    try:
        schema = load_schema(schema_path)
    except SchemaError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Check if any parameter uses primary-store (search recursively)
    params_using_primary_store: List[str] = []
    _find_primary_store_params(schema, "", params_using_primary_store)

    # If any parameter uses primary-store, --primary-store must be configured
    if params_using_primary_store and 'primary-store' not in global_flags:
        param_list = ', '.join(f"'{p}'" for p in sorted(params_using_primary_store))
        print(f"Error: Parameter(s) {param_list} require primary-store but --primary-store is not configured", file=sys.stderr)
        return 1

    # Parse CLI arguments (for parameter resolution)
    cli_args = parse_cli_args(arg_candidates)

    # Resolve all parameters
    try:
        primary_store_url = global_flags.get('primary-store')
        resolved = resolve_all_parameters(schema, cli_args, primary_store_url)
    except ParseError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except UnresolvedError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except PrimaryStoreError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Output resolved configuration as JSON
    print(json.dumps(resolved))

    return 0


def _find_primary_store_params(
    node: Dict[str, Any],
    path: str,
    params: List[str]
) -> None:
    """Recursively find all parameters that use primary-store.

    Args:
        node: The schema node to search
        path: The composed path to this node
        params: List to collect parameter paths
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store'}

    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration
        if 'primary-store' in node:
            params.append(path)
    else:
        # This is a group - search children
        for name, child in node.items():
            # Skip source annotations that are objects
            if name in source_annotations and isinstance(child, dict):
                continue

            # Build the composed path
            if path:
                child_path = f"{path}.{name}"
            else:
                child_path = name

            _find_primary_store_params(child, child_path, params)


if __name__ == '__main__':
    sys.exit(main())
