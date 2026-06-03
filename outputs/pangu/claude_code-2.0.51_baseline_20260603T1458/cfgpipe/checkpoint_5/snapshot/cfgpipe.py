#!/usr/bin/env python3
"""cfgpipe - Command-line configuration resolver.

Reads a JSON schema document from disk, resolves each declared parameter
from local sources, and writes the resolved configuration to stdout as JSON.
Every resolved leaf value is a string.
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from typing import Any


class ConfigError(Exception):
    """Exception raised for configuration resolution errors."""
    pass


def parse_cli_args(args: list[str]) -> dict[str, str]:
    """Parse CLI arguments in --name=value or -name=value format.

    Last wins for duplicate names. Only arguments after the schema-file path
    participate.
    """
    result: dict[str, str] = {}
    for arg in args:
        if '=' not in arg:
            continue
        name_part, _, value = arg.partition('=')
        # Strip leading dashes
        if name_part.startswith('--'):
            name = name_part[2:]
        elif name_part.startswith('-'):
            name = name_part[1:]
        else:
            continue
        result[name] = value
    return result


def read_schema(schema_path: str) -> dict[str, dict[str, Any]]:
    """Read and validate the schema file.

    Returns the parsed schema dict.

    Raises ConfigError on file missing, invalid JSON, or validation failure.
    """
    try:
        with open(schema_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise ConfigError(f"Schema file not found: {schema_path}")
    except OSError as e:
        raise ConfigError(f"Cannot read schema file {schema_path}: {e}")

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in schema file {schema_path}: {e}")

    if not isinstance(schema, dict):
        raise ConfigError(f"Schema root must be an object, got {type(schema).__name__}")

    # Validate each parameter declaration
    primary_store_keys = {}
    for param_name, param_decl in schema.items():
        if not isinstance(param_decl, dict):
            raise ConfigError(
                f"Parameter '{param_name}' must be an object declaration"
            )
        if 'type' not in param_decl:
            raise ConfigError(
                f"Parameter '{param_name}' is missing required 'type' field"
            )

        # Validate source fields are strings if present
        for field in ('default', 'env', 'file', 'arg'):
            if field in param_decl and not isinstance(param_decl[field], str):
                raise ConfigError(
                    f"Parameter '{param_name}' has non-string '{field}' field"
                )

        # Track primary-store keys for duplicate detection
        if 'primary-store' in param_decl:
            ps_key = param_decl['primary-store']
            if not isinstance(ps_key, str):
                raise ConfigError(
                    f"Parameter '{param_name}' has non-string 'primary-store' field"
                )
            if ps_key in primary_store_keys:
                raise ConfigError(
                    f"Parameters '{primary_store_keys[ps_key]}' and '{param_name}' "
                    f"share the same primary-store key '{ps_key}'"
                )
            primary_store_keys[ps_key] = param_name

    return schema, primary_store_keys


def fetch_primary_store_value(
    base_url: str,
    key: str,
    param_name: str
) -> str | None:
    """Fetch a value from the primary store.

    Returns the value as a string if found, None if not found.
    Raises ConfigError on connector failures or parse errors.
    """
    import urllib.parse
    import urllib.request
    import urllib.error
    import json as json_module

    url = f"{base_url}/v1/primary/kv?key={urllib.parse.quote(key, safe='')}"

    try:
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    body = response.read().decode('utf-8')
                    try:
                        data = json_module.loads(body)
                    except json_module.JSONDecodeError as e:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response is not valid JSON: {e}"
                        )

                    if not isinstance(data, dict):
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response must be a JSON object"
                        )

                    if 'found' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response missing 'found' field"
                        )

                    if not data['found']:
                        return None

                    if 'value' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response for present key missing 'value' field"
                        )

                    value = data['value']
                    if not isinstance(value, str):
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store 'value' field must be a string"
                        )

                    return value
                else:
                    raise ConfigError(
                        f"Parameter '{param_name}': primary-store returned unexpected status {response.status}"
                    )
        except urllib.error.HTTPError as e:
            # Handle 404 as missing key (per spec: 404 Not Found with body {"found": false} means key missing)
            if e.code == 404:
                # Try to read the response body
                try:
                    body = e.read().decode('utf-8')
                except:
                    body = '""'
                # Check if it's a proper {"found": false} response
                try:
                    data = json_module.loads(body)
                    if isinstance(data, dict) and data.get('found') is False:
                        return None
                except json_module.JSONDecodeError:
                    pass
                return None
            else:
                raise ConfigError(
                    f"Parameter '{param_name}': primary-store HTTP error {e.code}: {e.reason}"
                )
    except urllib.error.URLError as e:
        raise ConfigError(
            f"Parameter '{param_name}': primary-store network error: {e}"
        )


def parse_value(value: str, value_type: str, param_name: str, source: str) -> str:
    """Parse a raw string value against the declared type.

    Returns the value as a string (possibly formatted per type rules).
    Raises ConfigError on parse failure.

    All resolved values are returned as strings.
    """
    value = value.strip()

    if value_type == 'string':
        return value

    elif value_type == 'integer':
        try:
            # Must be a decimal integer, no scientific notation
            int(value)
            # Verify no scientific notation was accepted (int() would handle it)
            # Check for scientific notation indicators
            if 'e' in value.lower() or 'E' in value:
                raise ConfigError(
                    f"Parameter '{param_name}' cannot parse source '{source}': "
                    f"scientific notation not allowed for integer"
                )
            return value
        except ValueError:
            raise ConfigError(
                f"Parameter '{param_name}': cannot parse source '{source}' "
                f"'{value}' as integer"
            )

    elif value_type == 'float':
        try:
            # Must be a decimal input, no scientific notation required
            if 'e' in value.lower() or 'E' in value:
                raise ConfigError(
                    f"Parameter '{param_name}' cannot parse source '{source}': "
                    f"scientific notation not allowed for float"
                )
            float(value)
            return value
        except ValueError:
            raise ConfigError(
                f"Parameter '{param_name}': cannot parse source '{source}' "
                f"'{value}' as float"
            )

    elif value_type == 'boolean':
        lower_value = value.lower()
        if lower_value in ('true', 'false'):
            return lower_value
        raise ConfigError(
            f"Parameter '{param_name}': cannot parse source '{source}' "
            f"'{value}' as boolean (expected 'true' or 'false')"
        )

    else:
        raise ConfigError(
            f"Parameter '{param_name}': unknown type '{value_type}'"
        )


def resolve_parameter(
    param_name: str,
    param_decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None = None
) -> str | None:
    """Resolve a single parameter from its declared sources.

    Priority order (highest to lowest): arg > primary-store > file > env > default.
    Returns the resolved value as a string, or None if unresolved.
    Raises ConfigError on parse failure.
    """
    value_type = param_decl['type']

    # 1. Check CLI argument (highest priority)
    if 'arg' in param_decl:
        arg_name = param_decl['arg']
        if arg_name in cli_args:
            raw_value = cli_args[arg_name]
            return parse_value(raw_value, value_type, param_name, 'arg')

    # 2. Check primary store (second highest priority)
    if primary_store_url is not None and 'primary-store' in param_decl:
        ps_key = param_decl['primary-store']
        raw_value = fetch_primary_store_value(primary_store_url, ps_key, param_name)
        if raw_value is not None:
            return parse_value(raw_value, value_type, param_name, 'primary-store')

    # 3. Check file
    if 'file' in param_decl:
        file_path = param_decl['file']
        try:
            with open(file_path, 'r') as f:
                raw_value = f.read()
        except (FileNotFoundError, OSError):
            raw_value = None

        if raw_value is not None:
            raw_value = raw_value.strip()
            if raw_value != '':
                return parse_value(raw_value, value_type, param_name, 'file')

    # 4. Check environment variable
    if 'env' in param_decl:
        env_name = param_decl['env']
        try:
            raw_value = os.environ[env_name]
        except KeyError:
            raw_value = None

        if raw_value is not None and raw_value.strip() != '':
            return parse_value(raw_value, value_type, param_name, 'env')

    # 5. Check default (lowest priority)
    if 'default' in param_decl:
        raw_value = param_decl['default']
        return parse_value(raw_value, value_type, param_name, 'default')

    # No source provided a value
    return None


def main() -> int:
    """Main entry point.

    Returns exit code (0 for success, non-zero for failure).
    """
    parser = argparse.ArgumentParser(
        prog='cfgpipe',
        description='Configuration resolver',
        add_help=False
    )
    parser.add_argument('--primary-store', help='Primary store base URL')
    parser.add_argument('schema_file', help='Path to JSON schema file')
    parser.add_argument('arg_candidates', nargs='*', help='CLI argument candidates')

    # Parse known args to get schema file and primary-store, keep rest for CLI arg parsing
    args, remaining = parser.parse_known_args()

    # Read and validate schema
    try:
        schema, primary_store_keys = read_schema(args.schema_file)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Validate primary-store configuration
    if primary_store_keys:
        if args.primary_store is None:
            # At least one parameter declares primary-store but flag is absent
            params_with_ps = list(primary_store_keys.values())
            if len(params_with_ps) == 1:
                print(f"Error: Parameter '{params_with_ps[0]}' declares 'primary-store' but no --primary-store flag provided", file=sys.stderr)
            else:
                params_str = "', '".join(params_with_ps)
                print(f"Error: Parameters '{params_str}' declare 'primary-store' but no --primary-store flag provided", file=sys.stderr)
            return 1

    # Parse CLI arguments from remaining (including arg_candidates)
    all_cli_args = args.arg_candidates + remaining
    cli_args = parse_cli_args(all_cli_args)

    # Resolve each parameter
    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    for param_name in sorted(schema.keys()):  # Process in consistent order
        param_decl = schema[param_name]
        try:
            value = resolve_parameter(param_name, param_decl, cli_args, args.primary_store)
            if value is None:
                unresolved.append(param_name)
            else:
                resolved[param_name] = value
        except ConfigError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Check for unresolved parameters
    if unresolved:
        if len(unresolved) == 1:
            print(f"Error: Parameter '{unresolved[0]}' is unresolved", file=sys.stderr)
        else:
            params_str = "', '".join(unresolved)
            print(f"Error: Parameters '{params_str}' are unresolved", file=sys.stderr)
        return 1

    # Write resolved configuration to stdout
    output = json.dumps(resolved, sort_keys=False)
    print(output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
