#!/usr/bin/env python3
"""cfgpipe - Configuration resolver CLI."""

import json
import os
import sys
import urllib.parse
from typing import Any

import requests


def parse_cli_args(cli_args: list[str]) -> tuple[dict[str, str], str | None]:
    """Parse CLI argument candidates into a dict of name -> value.

    Supports --name=value and -name=value formats.
    Also handles --primary-store <base-url> as a global flag.

    Args:
        cli_args: List of argument strings from command line.

    Returns:
        Tuple of (Dictionary mapping argument names to their values, primary_store_url or None).
    """
    result = {}
    primary_store_url = None
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]
        if arg == '--primary-store':
            # Next argument is the base URL
            if i + 1 < len(cli_args):
                primary_store_url = cli_args[i + 1]
                i += 2
            else:
                # Missing value, treat as error
                raise ValueError("--primary-store requires a base URL")
        elif '=' in arg:
            name_part, value = arg.split('=', 1)
            # Strip leading dashes
            name = name_part.lstrip('-')
            result[name] = value
            i += 1
        else:
            # Skip unrecognized arguments
            i += 1
    return result, primary_store_url


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate the schema structure and field types.

    Raises:
        ValueError: If schema is invalid.
    """
    if not isinstance(schema, dict):
        raise ValueError("Schema must be a JSON object")

    # Track primary-store keys and their associated parameters
    primary_store_keys: dict[str, list[str]] = {}

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

        # Check primary-store key uniqueness
        if 'primary-store' in param_spec:
            pstore_key = param_spec['primary-store']
            if not isinstance(pstore_key, str):
                raise ValueError(
                    f"Parameter '{param_name}': 'primary-store' must be a string, "
                    f"got {type(pstore_key).__name__}"
                )
            if pstore_key not in primary_store_keys:
                primary_store_keys[pstore_key] = []
            primary_store_keys[pstore_key].append(param_name)

    # Check for duplicate primary-store keys
    for key, params in primary_store_keys.items():
        if len(params) > 1:
            raise ValueError(
                f"Parameters {params} share duplicate primary-store key '{key}'"
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


def fetch_from_primary_store(
    param_name: str,
    param_spec: dict[str, Any],
    primary_store_url: str
) -> str | None:
    """Fetch a parameter value from the primary store.

    Args:
        param_name: Name of the parameter.
        param_spec: Parameter specification.
        primary_store_url: Base URL of the primary store.

    Returns:
        Resolved value as a string if found, None if key is missing.

    Raises:
        ValueError: If the key is present but parse fails, or on connector failures.
    """
    if 'primary-store' not in param_spec:
        return None

    key = param_spec['primary-store']
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{primary_store_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        raise ValueError(
            f"Parameter '{param_name}': primary-store connector failure: {e}"
        )

    if response.status_code == 404:
        try:
            data = response.json()
            if data.get('found') is False:
                return None
            else:
                raise ValueError(
                    f"Parameter '{param_name}': primary-store responded 404 with invalid body"
                )
        except (json.JSONDecodeError, KeyError):
            raise ValueError(
                f"Parameter '{param_name}': primary-store responded 404 with malformed body"
            )

    if response.status_code != 200:
        raise ValueError(
            f"Parameter '{param_name}': primary-store connector failure: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except json.JSONDecodeError:
        raise ValueError(
            f"Parameter '{param_name}': primary-store responded with malformed JSON"
        )

    if not data.get('found', False):
        return None

    value = data.get('value')
    if value is None:
        raise ValueError(
            f"Parameter '{param_name}': primary-store response missing 'value' field"
        )

    if not isinstance(value, str):
        raise ValueError(
            f"Parameter '{param_name}': primary-store 'value' must be a string, got {type(value).__name__}"
        )

    param_type = param_spec['type']
    try:
        return parse_type(value, param_type, param_name)
    except ValueError as e:
        raise ValueError(
            f"Parameter '{param_name}': primary-store parse error: {str(e).removeprefix(f'Parameter \"{param_name}\": ')}"
        )


def resolve_parameter(
    param_name: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str],
    primary_store_url: str | None = None
) -> str | None:
    """Resolve a single parameter from its sources.

    Priority order (lowest to highest): default, env, file, primary-store, arg.
    Returns the resolved value as a string, or None if unresolved.
    Raises ValueError if parsing fails.
    """
    param_type = param_spec['type']
    candidates = []

    # Collect all possible sources in order of priority (lowest first)
    # Default is lowest priority
    if 'default' in param_spec:
        candidates.append(('default', param_spec['default']))

    # Env next
    if 'env' in param_spec:
        env_var = param_spec['env']
        env_value = os.environ.get(env_var)
        if env_value is not None and env_value.strip():
            candidates.append(('env', env_value.strip()))

    # File next
    if 'file' in param_spec:
        file_path = param_spec['file']
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
            trimmed = file_content.strip()
            if trimmed:
                candidates.append(('file', trimmed))
        except (FileNotFoundError, OSError, IsADirectoryError):
            # File doesn't exist or can't be read - skip
            pass

    # Primary-store next - may fail and raise ValueError
    if primary_store_url is not None and 'primary-store' in param_spec:
        pstore_value = fetch_from_primary_store(param_name, param_spec, primary_store_url)
        if pstore_value is not None:
            candidates.append(('primary-store', pstore_value))
        # If missing key, fall through to next source

    # Arg is highest priority
    if 'arg' in param_spec:
        arg_name = param_spec['arg']
        if arg_name in arg_values:
            candidates.append(('arg', arg_values[arg_name]))

    # Parse candidates in order, but only the last successful one wins
    # (higher priority overrides lower)
    for i, (source, value) in enumerate(candidates):
        try:
            parsed_val = parse_type(value, param_type, param_name)
            candidates[i] = (source, parsed_val)
        except ValueError as e:
            raise ValueError(
                f"Parameter '{param_name}': {source} source parse error: {str(e).removeprefix(f'Parameter \"{param_name}\": ')}"
            )

    if candidates:
        return candidates[-1][1]

    # Unresolved
    return None


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    # Scan for --primary-store before parsing
    args = sys.argv[1:]
    primary_store_url = None
    schema_position = -1

    i = 0
    while i < len(args):
        if args[i] == '--primary-store':
            if i + 1 < len(args):
                primary_store_url = args[i + 1]
                i += 2
            else:
                print("Error: --primary-store requires a base URL", file=sys.stderr)
                sys.exit(1)
        else:
            # This could be either schema path or an arg
            if schema_position == -1:
                schema_position = i
            i += 1

    if schema_position == -1:
        print("Error: Schema file not specified", file=sys.stderr)
        sys.exit(1)

    schema_path = args[schema_position]
    cli_args = args[schema_position + 1:]

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
    try:
        arg_values, _ = parse_cli_args(cli_args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Check if any parameters declare primary-store but --primary-store is not configured
    if primary_store_url is None:
        # Check for parameters with primary-store
        params_with_pstore = [
            param_name for param_name, param_spec in schema.items()
            if 'primary-store' in param_spec
        ]
        if params_with_pstore:
            if len(params_with_pstore) == 1:
                print(f"Error: Parameter '{params_with_pstore[0]}' requires --primary-store flag", file=sys.stderr)
            else:
                print(f"Error: Parameters {params_with_pstore} require --primary-store flag", file=sys.stderr)
            sys.exit(1)

    # Resolve each parameter
    results: dict[str, str] = {}
    unresolved: list[str] = []

    for param_name in schema:
        try:
            resolved = resolve_parameter(param_name, schema[param_name], arg_values, primary_store_url)
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
