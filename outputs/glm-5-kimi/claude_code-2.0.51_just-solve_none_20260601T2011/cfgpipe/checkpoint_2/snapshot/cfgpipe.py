#!/usr/bin/env python3
"""
cfgpipe - Primary Store Resolution

A command-line configuration resolver that reads a JSON schema document,
resolves each declared parameter from local and remote sources, and writes
the resolved configuration to stdout as JSON.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


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
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as integer")

    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for integer: '{value}'")

    try:
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
        if arg.startswith('--'):
            remaining = arg[2:]
            if '=' in remaining:
                name, value = remaining.split('=', 1)
                if name == arg_name:
                    result = value
        elif arg.startswith('-'):
            remaining = arg[1:]
            if '=' in remaining:
                name, value = remaining.split('=', 1)
                if name == arg_name:
                    result = value
    return result


def parse_global_flags(args: List[str]) -> Tuple[Optional[str], List[str]]:
    """
    Parse global flags from the front of the argument list.

    Returns: (primary_store_base_url, remaining_args)
    remaining_args[0] will be the schema file.
    """
    primary_store = None
    remaining = list(args)
    i = 0

    while i < len(remaining):
        if remaining[i] == '--primary-store':
            if i + 1 >= len(remaining):
                print("Error: --primary-store requires a value", file=sys.stderr)
                sys.exit(1)
            primary_store = remaining[i + 1]
            i += 2
        elif remaining[i].startswith('--primary-store='):
            primary_store = remaining[i].split('=', 1)[1]
            i += 1
        else:
            break

    return primary_store, remaining[i:]


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

        if 'type' not in declaration:
            errors.append(f"Parameter '{name}' missing required 'type' field")
        else:
            type_name = declaration['type']
            if type_name not in ('string', 'integer', 'float', 'boolean'):
                errors.append(f"Parameter '{name}' has unrecognized type: {type_name}")

        for source_field in ('default', 'env', 'file', 'arg', 'primary-store'):
            if source_field in declaration:
                if not isinstance(declaration[source_field], str):
                    errors.append(f"Parameter '{name}' has non-string '{source_field}' field")

    # Check for duplicate primary-store keys
    seen_ps_keys: Dict[str, str] = {}
    for name, declaration in schema.items():
        if not isinstance(declaration, dict):
            continue
        ps_key = declaration.get('primary-store')
        if ps_key is not None:
            if ps_key in seen_ps_keys:
                first_param = seen_ps_keys[ps_key]
                errors.append(
                    f"Parameters '{first_param}' and '{name}' share the same primary-store key '{ps_key}'"
                )
            else:
                seen_ps_keys[ps_key] = name

    return errors


def primary_store_lookup(base_url: str, key: str, param_name: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Look up a key from the primary store.

    Returns: (value_or_None, error_message_or_None)
    - On found key: (value_string, None)
    - On missing key (404 with found:false): (None, None) -- signals fallthrough
    - On any error: (None, error_message)
    """
    url = f"{base_url.rstrip('/')}/v1/primary/kv?{urlencode({'key': key})}"

    try:
        req = Request(url)
        with urlopen(req) as response:
            if response.status != 200:
                return (None, f"Primary store returned status {response.status} for parameter '{param_name}'")
            body = response.read().decode('utf-8')
    except HTTPError as e:
        if e.code == 404:
            try:
                body = e.read().decode('utf-8')
                data = json.loads(body)
                if isinstance(data, dict) and data.get('found') is False:
                    return (None, None)  # missing key, fall through
                else:
                    return (None, f"Primary store returned unexpected 404 response for parameter '{param_name}': {body}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return (None, f"Primary store returned malformed 404 response for parameter '{param_name}'")
        else:
            return (None, f"Primary store returned HTTP {e.code} for parameter '{param_name}': {e.reason}")
    except URLError as e:
        return (None, f"Primary store network error for parameter '{param_name}': {e.reason}")
    except Exception as e:
        return (None, f"Primary store error for parameter '{param_name}': {e}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return (None, f"Primary store returned malformed response for parameter '{param_name}': {body}")

    if not isinstance(data, dict):
        return (None, f"Primary store returned non-object response for parameter '{param_name}': {body}")

    if data.get('found') is not True:
        return (None, f"Primary store returned unexpected response for parameter '{param_name}': {body}")

    if 'value' not in data:
        return (None, f"Primary store returned found=true but missing 'value' for parameter '{param_name}'")

    return (str(data['value']), None)


def resolve_parameter(
    name: str,
    declaration: Dict[str, Any],
    cli_args: List[str],
    primary_store_base_url: Optional[str]
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve a single parameter.

    Returns: (resolved_value, source_name, error_message)
    - If resolved: (formatted_value, source_name, None)
    - If error: (None, source_name, error_message)
    - If unresolved: (None, None, None)
    """
    type_name = declaration.get('type', 'string')

    # Sources in priority order: arg > primary-store > file > env > default
    sources = [
        ('arg', lambda: match_arg(declaration['arg'], cli_args) if 'arg' in declaration else None),
        ('primary-store', lambda: _ps_lookup_wrapper(declaration, primary_store_base_url, name)),
        ('file', lambda: read_file_content(declaration['file']) if 'file' in declaration else None),
        ('env', lambda: os.environ.get(declaration['env']) if 'env' in declaration else None),
        ('default', lambda: declaration.get('default') if 'default' in declaration else None),
    ]

    for source_name, get_value in sources:
        raw_value = get_value()

        if raw_value is None:
            continue

        try:
            parsed_value = parse_value(raw_value, type_name)
            formatted_value = format_value(parsed_value, type_name)
            return (formatted_value, source_name, None)
        except ValueError as e:
            return (None, source_name, str(e))

    return (None, None, None)


_PS_SENTINEL = object()


def _ps_lookup_wrapper(declaration: Dict[str, Any], base_url: Optional[str], param_name: str) -> Optional[str]:
    """Wrapper for primary-store lookup that integrates with the source lambda pattern.

    Returns a string value if found, or None if missing/not applicable.
    Raises _PSLookupError on connector failures.
    """
    ps_key = declaration.get('primary-store')
    if ps_key is None or base_url is None:
        return None

    value, error = primary_store_lookup(base_url, ps_key, param_name)
    if error:
        raise _PSLookupError(error)
    return value


class _PSLookupError(Exception):
    """Internal exception to propagate primary-store errors through the source chain."""
    pass


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    # Parse global flags from the front of argv
    primary_store_base_url, remaining = parse_global_flags(sys.argv[1:])

    if not remaining:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    schema_file = remaining[0]
    cli_args = remaining[1:]

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

    validation_errors = validate_schema(schema)
    if validation_errors:
        for error in validation_errors:
            print(f"Error: {error}", file=sys.stderr)
        return 1

    # Check: if any parameter declares primary-store but --primary-store is absent, fail
    if primary_store_base_url is None:
        affected = []
        for name, declaration in schema.items():
            if isinstance(declaration, dict) and 'primary-store' in declaration:
                affected.append(name)
        if affected:
            for name in affected:
                print(f"Error: Parameter '{name}' declares primary-store but --primary-store is not configured", file=sys.stderr)
            return 1

    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for name, declaration in schema.items():
        try:
            value, source, error = resolve_parameter(name, declaration, cli_args, primary_store_base_url)
        except _PSLookupError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        if error:
            print(f"Error: Failed to parse parameter '{name}' from source '{source}': {error}", file=sys.stderr)
            return 1

        if value is not None:
            resolved[name] = value
        else:
            unresolved.append(name)

    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(sorted(unresolved))}", file=sys.stderr)
        return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
