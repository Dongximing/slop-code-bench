#!/usr/bin/env python3
"""
cfgpipe - Command-line configuration resolver.

Reads a JSON schema document, resolves each declared parameter from local sources,
and writes the resolved configuration to stdout as JSON.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


def parse_boolean(value: str) -> bool:
    """
    Parse common boolean string representations.
    Returns the boolean value or raises ValueError if unrecognized.
    """
    normalized = value.strip().lower()
    if normalized in ('true', 'yes', '1', 'on', 'y'):
        return True
    if normalized in ('false', 'no', '0', 'off', 'n'):
        return False

    raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    """
    Parse decimal integer.
    Raises ValueError if not a valid decimal integer.
    """
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for integer: '{value}'")

    try:
        result = int(stripped)
        if '.' in stripped:
            raise ValueError(f"Float value '{value}' cannot be parsed as integer")
        return result
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as integer")


def parse_float(value: str) -> float:
    """
    Parse decimal float.
    Raises ValueError if not a valid decimal float.
    """
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not supported: '{value}'")

    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def format_boolean(value: bool) -> str:
    """Format boolean as lowercase 'true' or 'false'."""
    return 'true' if value else 'false'


def format_integer(value: int) -> str:
    """Format integer as plain decimal string."""
    return str(value)


def format_float(value: float) -> str:
    """Format float as decimal string."""
    if value == int(value):
        return str(int(value))
    return str(value)


def parse_value(value: str, type_name: str) -> Tuple[Any, str]:
    """
    Parse a string value according to the declared type.
    Returns (parsed_value, formatted_string).
    Raises ValueError on parse failure.
    """
    if type_name == 'string':
        return value, value
    elif type_name == 'integer':
        parsed = parse_integer(value)
        return parsed, format_integer(parsed)
    elif type_name == 'float':
        parsed = parse_float(value)
        return parsed, format_float(parsed)
    elif type_name == 'boolean':
        parsed = parse_boolean(value)
        return parsed, format_boolean(parsed)
    else:
        raise ValueError(f"Unknown type: '{type_name}'")


def validate_schema(schema: Dict[str, Any]) -> None:
    """
    Validate the schema structure.
    Raises ValueError with descriptive message on validation failure.
    """
    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")

    seen_names = set()
    primary_store_keys: Dict[str, str] = {}  # key -> parameter name

    for name, param in schema.items():
        if name in seen_names:
            raise ValueError(f"Duplicate parameter name: '{name}'")
        seen_names.add(name)

        if not isinstance(param, dict):
            raise ValueError(f"Parameter '{name}' must be an object")

        if 'type' not in param:
            raise ValueError(f"Parameter '{name}' missing required 'type' field")

        type_name = param['type']
        if type_name not in ('string', 'integer', 'float', 'boolean'):
            raise ValueError(f"Parameter '{name}' has unrecognized type: '{type_name}'")

        for field in ('default', 'env', 'file', 'arg', 'primary-store'):
            if field in param:
                if not isinstance(param[field], str):
                    raise ValueError(f"Parameter '{name}' has non-string '{field}' field")

        # Check for duplicate primary-store keys
        if 'primary-store' in param:
            ps_key = param['primary-store']
            if ps_key in primary_store_keys:
                other_param = primary_store_keys[ps_key]
                raise ValueError(
                    f"Schema error: parameters '{other_param}' and '{name}' share the same primary-store key '{ps_key}'"
                )
            primary_store_keys[ps_key] = name


def parse_global_flags(args: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """
    Parse global flags from command-line arguments.

    Returns:
        (flags_dict, remaining_args) where flags_dict contains parsed global flags
        and remaining_args contains the rest of the arguments.
    """
    flags: Dict[str, str] = {}
    remaining: List[str] = []
    i = 0

    while i < len(args):
        arg = args[i]

        if arg == '--primary-store':
            if i + 1 >= len(args):
                print("Error: --primary-store requires a value", file=sys.stderr)
                sys.exit(1)
            flags['primary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--primary-store='):
            flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        else:
            # Non-global-flag argument, stop parsing global flags
            remaining.append(arg)
            i += 1

    return flags, remaining


def get_arg_value(arg_candidates: List[str], arg_name: str) -> Optional[str]:
    """
    Extract value from CLI argument candidates.
    Supports --name=value and -name=value formats.
    Last match wins.
    """
    result = None

    for arg in arg_candidates:
        if arg.startswith(f"--{arg_name}="):
            result = arg.split('=', 1)[1]
        elif arg.startswith(f"-{arg_name}="):
            result = arg.split('=', 1)[1]

    return result


def get_env_value(env_var: str) -> Optional[str]:
    """Get value from environment variable."""
    return os.environ.get(env_var)


def get_file_value(file_path: str) -> Optional[str]:
    """
    Get value from file.
    Returns None if file doesn't exist, is a directory, or is empty after trimming.
    """
    if not os.path.exists(file_path):
        return None

    if not os.path.isfile(file_path):
        return None

    try:
        with open(file_path, 'r') as f:
            content = f.read()

        trimmed = content.strip()
        if not trimmed:
            return None

        return trimmed
    except (IOError, OSError):
        return None


def query_primary_store(base_url: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Query the primary store for a key.

    Returns:
        (value, None) if key found
        (None, None) if key not found (404 with found=false)
        (None, error_message) on connector failure
    """
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        request = urllib.request.Request(url, method='GET')
        request.add_header('Accept', 'application/json')

        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                return None, f"Primary store returned non-200 status: {response.status}"

            body = response.read().decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None, f"Primary store returned malformed JSON: {body}"

            if not isinstance(data, dict):
                return None, f"Primary store returned non-object response: {body}"

            if 'found' not in data:
                return None, f"Primary store response missing 'found' field: {body}"

            if data['found'] is False:
                return None, None  # Key not found, fall through

            if data['found'] is True:
                if 'value' not in data:
                    return None, f"Primary store response missing 'value' field: {body}"
                if not isinstance(data['value'], str):
                    return None, f"Primary store 'value' is not a string: {body}"
                return data['value'], None

            return None, f"Primary store returned invalid 'found' value: {body}"

    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                body = e.read().decode('utf-8')
                data = json.loads(body)
                if isinstance(data, dict) and data.get('found') is False:
                    return None, None  # Key not found, fall through
                return None, f"Primary store 404 response malformed: {body}"
            except (json.JSONDecodeError, Exception):
                return None, f"Primary store 404 response malformed: {body}"
        else:
            return None, f"Primary store returned HTTP error {e.code}: {e.reason}"

    except urllib.error.URLError as e:
        return None, f"Primary store network error: {e.reason}"

    except Exception as e:
        return None, f"Primary store connector failure: {str(e)}"


def validate_primary_store_config(
    schema: Dict[str, Any],
    primary_store_url: Optional[str]
) -> None:
    """
    Validate that primary-store configuration is correct.
    Raises ValueError and exits if a parameter declares primary-store but the flag is absent.
    """
    params_with_primary_store = [
        name for name, param in schema.items()
        if 'primary-store' in param
    ]

    if params_with_primary_store and primary_store_url is None:
        param_name = params_with_primary_store[0]
        print(
            f"Error: Parameter '{param_name}' declares primary-store but --primary-store flag is not set",
            file=sys.stderr
        )
        sys.exit(1)


def resolve_parameter(
    name: str,
    param: Dict[str, Any],
    arg_candidates: List[str],
    primary_store_url: Optional[str]
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]]]:
    """
    Resolve a single parameter from its declared sources.

    Resolution order (priority): default, env, file, primary-store, arg

    Returns:
        (formatted_value, None) on success
        (None, (param_name, source, reason)) on parse failure
        (None, None) if unresolved
    """
    type_name = param['type']
    sources: List[Tuple[str, str]] = []

    # Highest priority: arg
    if 'arg' in param:
        arg_value = get_arg_value(arg_candidates, param['arg'])
        if arg_value is not None:
            sources.append(('arg', arg_value))

    # Next: primary-store
    if 'primary-store' in param and primary_store_url is not None:
        ps_key = param['primary-store']
        ps_value, ps_error = query_primary_store(primary_store_url, ps_key)
        if ps_error is not None:
            # Connector failure is fatal
            return None, (name, 'primary-store', ps_error)
        if ps_value is not None:
            sources.append(('primary-store', ps_value))

    # Next: file
    if 'file' in param:
        file_value = get_file_value(param['file'])
        if file_value is not None:
            sources.append(('file', file_value))

    # Next: env
    if 'env' in param:
        env_value = get_env_value(param['env'])
        if env_value is not None:
            sources.append(('env', env_value))

    # Lowest priority: default
    if 'default' in param:
        sources.append(('default', param['default']))

    for source_name, source_value in sources:
        try:
            _, formatted = parse_value(source_value, type_name)
            return formatted, None
        except ValueError as e:
            return None, (name, source_name, str(e))

    return None, None


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    # Parse global flags
    flags, remaining = parse_global_flags(sys.argv[1:])

    if not remaining:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    schema_file = remaining[0]
    arg_candidates = remaining[1:]

    primary_store_url = flags.get('primary-store')

    if not os.path.exists(schema_file):
        print(f"Error: Schema file not found: '{schema_file}'", file=sys.stderr)
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

    try:
        validate_schema(schema)
    except ValueError as e:
        print(f"Error: Schema validation failed: {e}", file=sys.stderr)
        return 1

    # Validate primary-store configuration
    validate_primary_store_config(schema, primary_store_url)

    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for name, param in schema.items():
        formatted_value, parse_error = resolve_parameter(
            name, param, arg_candidates, primary_store_url
        )

        if parse_error is not None:
            param_name, source, reason = parse_error
            print(f"Error: Failed to parse parameter '{param_name}' from source '{source}': {reason}", file=sys.stderr)
            return 1

        if formatted_value is not None:
            resolved[name] = formatted_value
        else:
            unresolved.append(name)

    if unresolved:
        unresolved_str = ", ".join(f"'{name}'" for name in unresolved)
        print(f"Error: Unresolved parameters: {unresolved_str}", file=sys.stderr)
        return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
