#!/usr/bin/env python3
"""cfgpipe - Core Resolution

A command-line configuration resolver that reads a JSON schema document,
resolves parameters from local sources, and outputs resolved configuration.

Supports watch mode with primary-store and secondary-store monitoring.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple, Set
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


class SecondaryStoreError(ConfigError):
    """Error for secondary-store related issues."""
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
    """Parse a float string and return canonical form with exactly 6 decimal digits.

    Accepts decimal inputs.
    """
    stripped = value.strip()
    try:
        parsed = float(stripped)
        # Format with exactly 6 decimal places
        return f"{parsed:.6f}"
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
    _validate_schema_node(schema, "", set(), set())

    return schema


def _validate_schema_node(
    node: Dict[str, Any],
    path: str,
    primary_store_keys: Set[Tuple[str, str]],
    secondary_store_keys: Set[Tuple[str, str]]
) -> None:
    """Recursively validate a schema node (group or parameter).

    Args:
        node: The schema node to validate
        path: The composed path to this node
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates
        secondary_store_keys: Set of (key, param_path) tuples for detecting duplicates

    Raises:
        SchemaError: If validation fails
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'}

    # Check if this node has a string-valued 'type' field making it a parameter declaration
    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration
        _validate_parameter(node, path, primary_store_keys, secondary_store_keys)
    else:
        # This is a group
        _validate_group(node, path, primary_store_keys, secondary_store_keys, source_annotations)


def _validate_parameter(
    declaration: Dict[str, Any],
    path: str,
    primary_store_keys: Set[Tuple[str, str]],
    secondary_store_keys: Set[Tuple[str, str]]
) -> None:
    """Validate a parameter declaration.

    Args:
        declaration: The parameter declaration
        path: The composed path to this parameter
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates
        secondary_store_keys: Set of (key, param_path) tuples for detecting duplicates

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

    # Validate secondary-store field
    if 'secondary-store' in declaration:
        if not isinstance(declaration['secondary-store'], str):
            raise SchemaError(f"Parameter '{path}' field 'secondary-store' must be a string")
        key = declaration['secondary-store']
        # Check for duplicates
        for existing_key, existing_path in secondary_store_keys:
            if existing_key == key:
                raise SchemaError(
                    f"Duplicate secondary-store key '{key}' used by parameters "
                    f"'{existing_path}' and '{path}'"
                )
        secondary_store_keys.add((key, path))


def _validate_group(
    group: Dict[str, Any],
    path: str,
    primary_store_keys: Set[Tuple[str, str]],
    secondary_store_keys: Set[Tuple[str, str]],
    source_annotations: Set[str]
) -> None:
    """Validate a group (container without a parameter value).

    Args:
        group: The group to validate
        path: The composed path to this group
        primary_store_keys: Set of (key, param_path) tuples for detecting duplicates
        secondary_store_keys: Set of (key, param_path) tuples for detecting duplicates
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
        _validate_schema_node(child, child_path, primary_store_keys, secondary_store_keys)


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


def parse_duration(duration_str: str) -> float:
    """Parse a duration string like '2s', '500ms' into seconds.

    Args:
        duration_str: Duration string (e.g., '2s', '500ms', '1m')

    Returns:
        Duration in seconds as a float

    Raises:
        ValueError: If the format is invalid
    """
    match = re.match(r'^(\d+(?:\.\d+)?)(ms|s|m|h)?$', duration_str.strip())
    if not match:
        raise ValueError(f"Invalid duration format: '{duration_str}'")

    value = float(match.group(1))
    unit = match.group(2) or 's'

    if unit == 'ms':
        return value / 1000.0
    elif unit == 's':
        return value
    elif unit == 'm':
        return value * 60.0
    elif unit == 'h':
        return value * 3600.0
    else:
        return value


def parse_global_flags(args: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Parse global flags from command-line arguments.

    Global flags must precede the schema file path.
    Supported flags: --primary-store <base-url>, --secondary-store <base-url>,
                     --secondary-store-poll-interval <duration>, --watch

    Args:
        args: List of command-line arguments (excluding program name)

    Returns:
        Tuple of (global_flags_dict, remaining_args)
        global_flags_dict contains keys like 'primary-store', 'secondary-store',
        'secondary-store-poll-interval', 'watch'
        remaining_args are the args after global flags (schema file and arg-candidates)
    """
    global_flags: Dict[str, Any] = {}
    remaining = []
    i = 0

    while i < len(args):
        arg = args[i]

        # Check for --watch (boolean flag)
        if arg == '--watch':
            global_flags['watch'] = True
            i += 1
        # Check for --primary-store
        elif arg == '--primary-store':
            if i + 1 < len(args):
                global_flags['primary-store'] = args[i + 1]
                i += 2
            else:
                raise ValueError("Error: --primary-store requires a base-url argument")
        elif arg.startswith('--primary-store='):
            global_flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        # Check for --secondary-store
        elif arg == '--secondary-store':
            if i + 1 < len(args):
                global_flags['secondary-store'] = args[i + 1]
                i += 2
            else:
                raise ValueError("Error: --secondary-store requires a base-url argument")
        elif arg.startswith('--secondary-store='):
            global_flags['secondary-store'] = arg.split('=', 1)[1]
            i += 1
        # Check for --secondary-store-poll-interval
        elif arg == '--secondary-store-poll-interval':
            if i + 1 < len(args):
                try:
                    global_flags['secondary-store-poll-interval'] = parse_duration(args[i + 1])
                except ValueError as e:
                    raise ValueError(f"Error: {e}")
                i += 2
            else:
                raise ValueError("Error: --secondary-store-poll-interval requires a duration argument")
        elif arg.startswith('--secondary-store-poll-interval='):
            try:
                global_flags['secondary-store-poll-interval'] = parse_duration(arg.split('=', 1)[1])
            except ValueError as e:
                raise ValueError(f"Error: {e}")
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


def lookup_secondary_store(base_url: str, key: str, param_name: str) -> Tuple[Optional[str], bool]:
    """Look up a key in the secondary store.

    Seed lookup: GET <base-url>/v1/secondary/kv?key=<url-encoded-key>

    Args:
        base_url: The base URL of the secondary store
        key: The key to look up
        param_name: Parameter name for error messages

    Returns:
        Tuple of (value, found) where:
        - value is the string value if found, None otherwise
        - found is True if key exists, False if key is missing

    Raises:
        SecondaryStoreError: If the request fails (non-200, malformed response, network error)
    """
    # Construct the URL
    endpoint = f"{base_url.rstrip('/')}/v1/secondary/kv"
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
                    raise SecondaryStoreError(
                        f"Failed to parse secondary-store response for parameter '{param_name}': {e}"
                    )

                # Validate response structure
                if not isinstance(data, dict):
                    raise SecondaryStoreError(
                        f"Malformed secondary-store response for parameter '{param_name}': "
                        "expected JSON object"
                    )

                if 'found' not in data:
                    raise SecondaryStoreError(
                        f"Malformed secondary-store response for parameter '{param_name}': "
                        "missing 'found' field"
                    )

                if data['found']:
                    if 'value' not in data:
                        raise SecondaryStoreError(
                            f"Malformed secondary-store response for parameter '{param_name}': "
                            "missing 'value' field for found key"
                        )
                    # Return the string value
                    return str(data['value']), True
                else:
                    # Key not found
                    return None, False

            else:
                # Non-200 status
                raise SecondaryStoreError(
                    f"Secondary-store request failed for parameter '{param_name}': "
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
                    raise SecondaryStoreError(
                        f"Malformed secondary-store 404 response for parameter '{param_name}'"
                    )
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Malformed 404 response
                raise SecondaryStoreError(
                    f"Malformed secondary-store 404 response for parameter '{param_name}'"
                )
        else:
            # Other HTTP error
            raise SecondaryStoreError(
                f"Secondary-store request failed for parameter '{param_name}': "
                f"HTTP {e.code}"
            )

    except urllib.error.URLError as e:
        # Network error
        raise SecondaryStoreError(
            f"Secondary-store network error for parameter '{param_name}': {e.reason}"
        )

    except TimeoutError:
        raise SecondaryStoreError(
            f"Secondary-store timeout for parameter '{param_name}'"
        )


def batch_read_secondary_store(base_url: str, keys: List[str]) -> List[Dict[str, Any]]:
    """Batch read keys from the secondary store.

    POST <base-url>/v1/secondary/batch-read with JSON body containing keys array.

    Args:
        base_url: The base URL of the secondary store
        keys: List of keys to read

    Returns:
        List of items matching request key order. Each item has:
        - key: the key string
        - status: one of 'ok', 'missing', 'error'
        - value: for 'ok' status
        - error: for 'error' status

    Raises:
        SecondaryStoreError: If the request fails
    """
    endpoint = f"{base_url.rstrip('/')}/v1/secondary/batch-read"

    try:
        body_data = json.dumps({'keys': keys}).encode('utf-8')
        request = urllib.request.Request(endpoint, data=body_data, method='POST')
        request.add_header('Content-Type', 'application/json')
        request.add_header('Accept', 'application/json')

        with urllib.request.urlopen(request, timeout=30) as response:
            status_code = response.status
            body = response.read().decode('utf-8')

            if status_code == 200:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as e:
                    raise SecondaryStoreError(f"Failed to parse secondary-store batch-read response: {e}")

                if not isinstance(data, dict) or 'items' not in data:
                    raise SecondaryStoreError("Malformed secondary-store batch-read response: missing 'items' field")

                return data['items']
            else:
                raise SecondaryStoreError(f"Secondary-store batch-read failed: HTTP {status_code}")

    except urllib.error.URLError as e:
        raise SecondaryStoreError(f"Secondary-store batch-read network error: {e.reason}")
    except TimeoutError:
        raise SecondaryStoreError("Secondary-store batch-read timeout")


def watch_primary_store(base_url: str, keys: List[str], cursor: int) -> Tuple[int, List[Dict[str, Any]]]:
    """Watch for changes in the primary store.

    GET <base-url>/v1/primary/watch?cursor=<int>&key=<url-encoded-key>...
    One key parameter per monitored key.

    Args:
        base_url: The base URL of the primary store
        keys: List of keys to watch
        cursor: Current cursor position (0 for first request)

    Returns:
        Tuple of (next_cursor, events) where events is a list of objects
        each having 'key', 'value', 'version'

    Raises:
        PrimaryStoreError: If the request fails or response is malformed
    """
    endpoint = f"{base_url.rstrip('/')}/v1/primary/watch"
    params = [('cursor', str(cursor))]
    for key in keys:
        params.append(('key', key))
    url = f"{endpoint}?{urlencode(params)}"

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
                    raise PrimaryStoreError(f"Failed to parse primary-store watch response: {e}")

                if not isinstance(data, dict):
                    raise PrimaryStoreError("Malformed primary-store watch response: expected JSON object")

                if 'cursor' not in data:
                    raise PrimaryStoreError("Malformed primary-store watch response: missing 'cursor' field")

                if 'events' not in data:
                    raise PrimaryStoreError("Malformed primary-store watch response: missing 'events' field")

                return data['cursor'], data['events']
            else:
                raise PrimaryStoreError(f"Primary-store watch request failed: HTTP {status_code}")

    except urllib.error.URLError as e:
        raise PrimaryStoreError(f"Primary-store watch network error: {e.reason}")
    except TimeoutError:
        raise PrimaryStoreError("Primary-store watch timeout")


def emit_change_event(path: str, type_name: str, previous: str, current: str) -> None:
    """Emit a change event as a single-line JSON object to stdout.

    Args:
        path: Dot-separated parameter path
        type_name: Type name of the parameter
        previous: Prior string representation ("" for first assignment)
        current: New string representation
    """
    event = {
        "path": path,
        "type": type_name,
        "previous": previous,
        "current": current
    }
    print(json.dumps(event), flush=True)


class ParameterInfo:
    """Information about a parameter for watch mode."""

    def __init__(self, path: str, declaration: Dict[str, Any]):
        self.path = path
        self.declaration = declaration
        self.type_name = declaration['type']
        self.primary_store_key: Optional[str] = declaration.get('primary-store')
        self.secondary_store_key: Optional[str] = declaration.get('secondary-store')
        self.current_value: Optional[str] = None
        self.current_version: int = 0  # For primary-store version tracking
        self.secondary_baseline: Optional[str] = None  # For secondary-store baseline


def collect_parameters(schema: Dict[str, Any], path: str = "") -> List[ParameterInfo]:
    """Collect all parameters from schema for watch mode.

    Args:
        schema: The schema dictionary
        path: Current path prefix

    Returns:
        List of ParameterInfo objects
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'}
    params = []

    for name, child in schema.items():
        # Skip source annotations that are objects
        if name in source_annotations and isinstance(child, dict):
            continue

        # Build the composed path
        if path:
            child_path = f"{path}.{name}"
        else:
            child_path = name

        if 'type' in child and isinstance(child['type'], str):
            # This is a parameter declaration
            params.append(ParameterInfo(child_path, child))
        else:
            # This is a group - recurse
            params.extend(collect_parameters(child, child_path))

    return params


def resolve_parameter_from_sources(
    param: ParameterInfo,
    cli_args: Dict[str, str],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a parameter from its declared sources.

    Priority order (highest to lowest): arg, secondary-store, primary-store, file, env, default.
    This matches the spec: default, env, file, primary-store, secondary-store, arg (lowest to highest).

    Args:
        param: ParameterInfo object
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL
        secondary_store_url: Optional secondary-store base URL

    Returns:
        Tuple of (resolved_value, source_name) or (None, None) if unresolved
    """
    declaration = param.declaration
    param_type = param.type_name
    param_name = param.path

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

    # Try secondary-store (second highest priority)
    if 'secondary-store' in declaration and secondary_store_url:
        key = declaration['secondary-store']
        try:
            value, found = lookup_secondary_store(secondary_store_url, key, param_name)
            if found and value is not None:
                try:
                    parsed = parse_value(value, param_type, param_name, 'secondary-store')
                    return parsed, 'secondary-store'
                except ParseError:
                    raise
        except SecondaryStoreError:
            raise

    # Try primary-store (third highest priority)
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

    # Try file (fourth highest priority)
    if 'file' in declaration:
        file_path = declaration['file']
        file_value = resolve_from_file(file_path)
        if file_value is not None:
            try:
                parsed = parse_value(file_value, param_type, param_name, 'file')
                return parsed, 'file'
            except ParseError:
                raise

    # Try env (fifth highest priority)
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
    primary_store_url: Optional[str] = None,
    secondary_store_url: Optional[str] = None
) -> Dict[str, Any]:
    """Resolve all parameters from the schema.

    Args:
        schema: The validated schema dictionary
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL
        secondary_store_url: Optional secondary-store base URL

    Returns:
        Nested dictionary mirroring the schema group hierarchy with resolved values

    Raises:
        ParseError: If any parameter fails to parse
        UnresolvedError: If any parameters remain unresolved
        PrimaryStoreError: If primary-store lookup fails
        SecondaryStoreError: If secondary-store lookup fails
    """
    result: Dict[str, Any] = {}
    unresolved: List[str] = []

    _resolve_schema_node(schema, "", result, unresolved, cli_args, primary_store_url, secondary_store_url)

    if unresolved:
        raise UnresolvedError(unresolved)

    return result


def _resolve_schema_node(
    node: Dict[str, Any],
    path: str,
    result: Dict[str, Any],
    unresolved: List[str],
    cli_args: Dict[str, str],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str]
) -> None:
    """Recursively resolve a schema node (group or parameter).

    Args:
        node: The schema node to resolve
        path: The composed path to this node
        result: The result dictionary to populate
        unresolved: List to collect unresolved parameter paths
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL
        secondary_store_url: Optional secondary-store base URL
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'}

    # Check if this node has a string-valued 'type' field making it a parameter declaration
    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration - resolve it
        pass  # Handled by parent
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
                param_info = ParameterInfo(child_path, child)
                try:
                    value, source = resolve_parameter_from_sources(
                        param_info, cli_args, primary_store_url, secondary_store_url
                    )
                except ParseError:
                    raise
                except PrimaryStoreError:
                    raise
                except SecondaryStoreError:
                    raise

                if value is not None:
                    result[name] = value
                else:
                    unresolved.append(child_path)
            else:
                # This is a group - create nested object
                result[name] = {}
                _resolve_schema_node(
                    child, child_path, result[name], unresolved,
                    cli_args, primary_store_url, secondary_store_url
                )


def build_resolved_config(params: List[ParameterInfo]) -> Dict[str, Any]:
    """Build the resolved configuration dictionary from parameter values.

    Args:
        params: List of ParameterInfo objects with current_value set

    Returns:
        Nested dictionary mirroring the schema structure
    """
    result: Dict[str, Any] = {}

    for param in params:
        if param.current_value is None:
            continue

        parts = param.path.split('.')
        current = result

        for i, part in enumerate(parts[:-1]):
            if part not in current:
                current[part] = {}
            current = current[part]

        current[parts[-1]] = param.current_value

    return result


def run_watch_mode(
    schema: Dict[str, Any],
    cli_args: Dict[str, str],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    secondary_poll_interval: Optional[float]
) -> int:
    """Run watch mode.

    Args:
        schema: The validated schema dictionary
        cli_args: Parsed CLI arguments
        primary_store_url: Optional primary-store base URL
        secondary_store_url: Optional secondary-store base URL
        secondary_poll_interval: Poll interval for secondary store (seconds)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Collect all parameters
    params = collect_parameters(schema)

    # Build lookup maps
    primary_key_to_param: Dict[str, ParameterInfo] = {}
    secondary_key_to_param: Dict[str, ParameterInfo] = {}

    for param in params:
        if param.primary_store_key:
            primary_key_to_param[param.primary_store_key] = param
        if param.secondary_store_key:
            secondary_key_to_param[param.secondary_store_key] = param

    # Validate watch setup requirements
    if secondary_store_url:
        if secondary_poll_interval is None or secondary_poll_interval <= 0:
            print("Error: --secondary-store-poll-interval must be strictly positive when --watch and --secondary-store are both present", file=sys.stderr)
            return 1

        if not secondary_key_to_param:
            print("Error: --watch and --secondary-store require at least one parameter to declare secondary-store", file=sys.stderr)
            return 1

    # Check if there are any monitorable sources
    has_primary_store = primary_store_url and primary_key_to_param
    has_secondary_store = secondary_store_url and secondary_key_to_param
    has_monitorable = has_primary_store or has_secondary_store

    # Initial resolution - collect seed events
    for param in params:
        try:
            value, source = resolve_parameter_from_sources(
                param, cli_args, primary_store_url, secondary_store_url
            )
        except ParseError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except PrimaryStoreError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except SecondaryStoreError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        if value is None:
            print(f"Error: Unresolved parameter '{param.path}'", file=sys.stderr)
            return 1

        param.current_value = value

        # Track version for primary-store from seed lookup
        # We need to get the version from the primary store response
        # For now, we'll do a separate lookup to get the version
        if param.primary_store_key and primary_store_url:
            # We already have the value, but we need the version
            # Let's do a fresh lookup to get version info
            # Actually, the seed endpoint doesn't return version, so we'll
            # start with version 0 and the first watch will update it
            param.current_version = 0

        # Set baseline for secondary store
        if param.secondary_store_key and secondary_store_url:
            param.secondary_baseline = value

        # Emit seed-time change event
        emit_change_event(param.path, param.type_name, "", value)

    # Emit the full resolved configuration
    config = build_resolved_config(params)
    print(json.dumps(config), flush=True)

    # If no monitorable sources, exit normally
    if not has_monitorable:
        return 0

    # Watch loop
    try:
        _watch_loop(
            params, primary_key_to_param, secondary_key_to_param,
            primary_store_url, secondary_store_url,
            secondary_poll_interval, cli_args
        )
    except PrimaryStoreError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except SecondaryStoreError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0

    return 0


def _watch_loop(
    params: List[ParameterInfo],
    primary_key_to_param: Dict[str, ParameterInfo],
    secondary_key_to_param: Dict[str, ParameterInfo],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    secondary_poll_interval: Optional[float],
    cli_args: Dict[str, str]
) -> None:
    """Main watch loop for monitoring store changes.

    Args:
        params: List of all parameters
        primary_key_to_param: Map of primary-store keys to parameters
        secondary_key_to_param: Map of secondary-store keys to parameters
        primary_store_url: Optional primary-store base URL
        secondary_store_url: Optional secondary-store base URL
        secondary_poll_interval: Poll interval for secondary store
        cli_args: Parsed CLI arguments
    """
    primary_cursor = 0
    last_secondary_poll = time.time()

    while True:
        now = time.time()

        # Poll primary store if configured
        if primary_store_url and primary_key_to_param:
            try:
                primary_keys = list(primary_key_to_param.keys())
                new_cursor, events = watch_primary_store(primary_store_url, primary_keys, primary_cursor)

                for event in events:
                    key = event['key']
                    value = event['value']
                    version = event['version']

                    if key in primary_key_to_param:
                        param = primary_key_to_param[key]

                        # Check if this update is newer than the last applied
                        if version > param.current_version:
                            # Check if arg override is active
                            if 'arg' in param.declaration:
                                arg_name = param.declaration['arg']
                                if arg_name in cli_args:
                                    # Arg override is active, skip this update
                                    continue

                            # Parse the new value
                            try:
                                parsed = parse_value(value, param.type_name, param.path, 'primary-store')
                            except ParseError:
                                # Silently skip parse errors during runtime monitoring
                                continue

                            # Emit change event if value changed
                            if parsed != param.current_value:
                                previous = param.current_value or ""
                                emit_change_event(param.path, param.type_name, previous, parsed)
                                param.current_value = parsed

                            param.current_version = version

                primary_cursor = new_cursor
            except PrimaryStoreError:
                raise

        # Poll secondary store if configured
        if secondary_store_url and secondary_key_to_param and secondary_poll_interval:
            if now - last_secondary_poll >= secondary_poll_interval:
                last_secondary_poll = now

                try:
                    secondary_keys = list(secondary_key_to_param.keys())
                    items = batch_read_secondary_store(secondary_store_url, secondary_keys)

                    for item in items:
                        key = item['key']
                        status = item['status']

                        if key in secondary_key_to_param:
                            param = secondary_key_to_param[key]

                            if status == 'ok':
                                value = item['value']

                                # Check if arg override is active
                                if 'arg' in param.declaration:
                                    arg_name = param.declaration['arg']
                                    if arg_name in cli_args:
                                        # Arg override is active, skip this update
                                        continue

                                # Parse the new value
                                try:
                                    parsed = parse_value(value, param.type_name, param.path, 'secondary-store')
                                except ParseError:
                                    # Silently skip parse errors during runtime monitoring
                                    continue

                                # Check for change from last successful observation
                                if param.secondary_baseline is None:
                                    # First successful observation - may emit event
                                    # The baseline is already set from seed, so this shouldn't happen
                                    param.secondary_baseline = parsed
                                elif parsed != param.secondary_baseline:
                                    # Value changed from baseline
                                    previous = param.current_value or ""
                                    emit_change_event(param.path, param.type_name, previous, parsed)
                                    param.current_value = parsed
                                    param.secondary_baseline = parsed
                                # else: no change, no event

                except SecondaryStoreError:
                    raise

        # Small sleep to avoid busy waiting
        time.sleep(0.1)


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
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'}

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


def _find_secondary_store_params(
    node: Dict[str, Any],
    path: str,
    params: List[str]
) -> None:
    """Recursively find all parameters that use secondary-store.

    Args:
        node: The schema node to search
        path: The composed path to this node
        params: List to collect parameter paths
    """
    source_annotations = {'default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'}

    if 'type' in node and isinstance(node['type'], str):
        # This is a parameter declaration
        if 'secondary-store' in node:
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

            _find_secondary_store_params(child, child_path, params)


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
        print("  --secondary-store <base-url>  Configure secondary-store base URL", file=sys.stderr)
        print("  --secondary-store-poll-interval <duration>  Poll interval for secondary store", file=sys.stderr)
        print("  --watch  Enable watch mode", file=sys.stderr)
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

    # Get store URLs
    primary_store_url = global_flags.get('primary-store')
    secondary_store_url = global_flags.get('secondary-store')
    watch_mode = global_flags.get('watch', False)
    secondary_poll_interval = global_flags.get('secondary-store-poll-interval')

    # Check if any parameter uses primary-store (search recursively)
    params_using_primary_store: List[str] = []
    _find_primary_store_params(schema, "", params_using_primary_store)

    # Check if any parameter uses secondary-store
    params_using_secondary_store: List[str] = []
    _find_secondary_store_params(schema, "", params_using_secondary_store)

    # Validate store configuration requirements
    if params_using_primary_store and not primary_store_url:
        param_list = ', '.join(f"'{p}'" for p in sorted(params_using_primary_store))
        print(f"Error: Parameter(s) {param_list} require primary-store but --primary-store is not configured", file=sys.stderr)
        return 1

    if params_using_secondary_store and not secondary_store_url:
        param_list = ', '.join(f"'{p}'" for p in sorted(params_using_secondary_store))
        print(f"Error: Parameter(s) {param_list} require secondary-store but --secondary-store is not configured", file=sys.stderr)
        return 1

    # Parse CLI arguments (for parameter resolution)
    cli_args = parse_cli_args(arg_candidates)

    # Run watch mode or one-shot mode
    if watch_mode:
        return run_watch_mode(
            schema, cli_args, primary_store_url, secondary_store_url,
            secondary_poll_interval
        )
    else:
        # One-shot mode
        try:
            resolved = resolve_all_parameters(schema, cli_args, primary_store_url, secondary_store_url)
        except ParseError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except UnresolvedError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except PrimaryStoreError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except SecondaryStoreError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        # Output resolved configuration as JSON
        print(json.dumps(resolved))

        return 0


if __name__ == '__main__':
    sys.exit(main())
