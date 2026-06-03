#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver."""

import argparse
import json
import os
import sys
from typing import Any
from urllib.parse import quote
import urllib.request
import urllib.error


def parse_args(args: list[str]) -> tuple[str | None, list[str], str | None]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--primary-store', dest='primary_store', help='Primary store base URL')
    parser.add_argument('remaining', nargs=argparse.REMAINDER, help='Schema file and remaining args')

    # Parse known args first to extract global flags
    known, unknown = parser.parse_known_args(args)

    # The remaining args should contain schema file followed by arg candidates
    all_remaining = unknown if unknown else known.remaining

    if not all_remaining:
        print("Error: No schema file specified", file=sys.stderr)
        sys.exit(1)

    schema_file = all_remaining[0]
    arg_candidates = all_remaining[1:]
    return known.primary_store, schema_file, arg_candidates


def parse_arg_candidates(arg_candidates: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}

    for arg in arg_candidates:
        if "=" in arg:
            # Handle --name=value or -name=value
            name_part, value = arg.split("=", 1)
            # Strip leading dashes to get the name
            name = name_part.lstrip("-")
            result[name] = value

    return result


def load_schema(schema_file: str) -> dict[str, dict[str, Any]]:
    try:
        with open(schema_file, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: Schema file missing or invalid: {schema_file}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"Error: Cannot read schema file: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(schema, dict):
        print("Error: Schema must be a JSON object", file=sys.stderr)
        sys.exit(1)

    return schema


def validate_schema(schema: dict[str, dict[str, Any]]) -> dict[str, str]:
    valid_types = {"string", "integer", "float", "boolean"}
    source_fields = {"default", "env", "file", "arg"}

    # Track primary-store keys to check for duplicates
    primary_store_keys: dict[str, str] = {}

    for param_name, param_spec in schema.items():
        if not isinstance(param_spec, dict):
            print(f"Error: Parameter '{param_name}' must be an object", file=sys.stderr)
            sys.exit(1)

        # Check type field
        if "type" not in param_spec:
            print(f"Error: Parameter '{param_name}' missing required 'type' field", file=sys.stderr)
            sys.exit(1)

        param_type = param_spec["type"]
        if param_type not in valid_types:
            print(f"Error: Parameter '{param_name}' has invalid type '{param_type}'", file=sys.stderr)
            sys.exit(1)

        # Check source fields are strings
        for field in source_fields:
            if field in param_spec and not isinstance(param_spec[field], str):
                print(f"Error: Parameter '{param_name}' field '{field}' must be a string", file=sys.stderr)
                sys.exit(1)

        # Check primary-store field
        if "primary-store" in param_spec:
            if not isinstance(param_spec["primary-store"], str):
                print(f"Error: Parameter '{param_name}' field 'primary-store' must be a string", file=sys.stderr)
                sys.exit(1)

            ps_key = param_spec["primary-store"]
            # Check for duplicates
            if ps_key in primary_store_keys:
                print(f"Error: Schema error: parameters '{primary_store_keys[ps_key]}' and '{param_name}' "
                      f"share the same 'primary-store' key '{ps_key}'", file=sys.stderr)
                sys.exit(1)
            primary_store_keys[ps_key] = param_name

    return primary_store_keys


def fetch_primary_store_value(base_url: str, key: str, param_name: str) -> str | None:
    encoded_key = quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                body = response.read().decode('utf-8')
                try:
                    data = json.loads(body)
                except json.JSONDecodeError as e:
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: "
                          f"malformed JSON response from '{url}': {e}", file=sys.stderr)
                    sys.exit(1)

                if not isinstance(data, dict):
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: "
                          f"expected JSON object from '{url}'", file=sys.stderr)
                    sys.exit(1)

                if data.get("found") is True:
                    value = data.get("value")
                    if value is None:
                        print(f"Error: Parameter '{param_name}' primary-store connector failure: "
                              f"missing 'value' field in response from '{url}'", file=sys.stderr)
                        sys.exit(1)
                    return str(value)
                elif data.get("found") is False:
                    return None
                else:
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: "
                          f"missing or invalid 'found' field in response from '{url}'", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Error: Parameter '{param_name}' primary-store connector failure: "
                      f"HTTP {response.status} from '{url}'", file=sys.stderr)
                sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Parameter '{param_name}' primary-store connector failure: network error "
              f"connecting to '{url}': {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Parameter '{param_name}' primary-store connector failure: unexpected error "
              f"connecting to '{url}': {e}", file=sys.stderr)
        sys.exit(1)


def parse_value(value: str, expected_type: str, param_name: str, source: str) -> str:
    try:
        if expected_type == "string":
            return value

        elif expected_type == "integer":
            stripped = value.strip()
            if not stripped:
                raise ValueError("empty value")
            if 'e' in stripped.lower() or 'E' in stripped:
                raise ValueError("scientific notation not supported")
            if not stripped.lstrip('-').isdigit():
                raise ValueError("not a decimal integer")
            return stripped

        elif expected_type == "float":
            stripped = value.strip()
            if not stripped:
                raise ValueError("empty value")
            float(stripped)
            return stripped

        elif expected_type == "boolean":
            lower = value.strip().lower()
            if lower not in ("true", "false"):
                raise ValueError(f"must be 'true' or 'false', got '{value}'")
            return lower

    except ValueError as e:
        print(f"Error: Parameter '{param_name}' parse failure from source '{source}': {e}", file=sys.stderr)
        sys.exit(1)
    return value


def resolve_parameter(
    param_name: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str],
    primary_store_url: str | None
) -> str | None:
    """Resolve a parameter from its sources.

    Priority order: default (lowest), env, file, primary-store, arg (highest).
    Check from highest to lowest priority; first one with a value wins.

    Args:
        param_name: Name of the parameter
        param_spec: Parameter specification dictionary
        arg_values: Parsed command-line argument values
        primary_store_url: Primary store base URL or None if unconfigured

    Returns:
        Resolved value as string, or None if unresolved

    Raises:
        SystemExit: If parsing fails or primary-store is required but not configured
    """
    param_type = param_spec["type"]

    # 1. Check command-line argument (highest priority)
    if "arg" in param_spec:
        arg_name = param_spec["arg"]
        if arg_name in arg_values:
            arg_value = arg_values[arg_name]
            if arg_value:  # Non-empty
                value = parse_value(arg_value, param_type, param_name, "arg")
                return value

    # 2. Check file
    if "file" in param_spec:
        file_path = param_spec["file"]
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
        except (FileNotFoundError, OSError, IsADirectoryError):
            # File doesn't exist or can't be read, continue to next source
            pass
        else:
            stripped = file_content.strip()
            if stripped:
                value = parse_value(stripped, param_type, param_name, "file")
                return value

    # 3. Check environment variable
    if "env" in param_spec:
        env_value = os.environ.get(param_spec["env"])
        if env_value is not None and env_value.strip():
            value = parse_value(env_value, param_type, param_name, "env")
            return value

    # 4. Check primary-store
    if "primary-store" in param_spec:
        if primary_store_url is None:
            print(f"Error: Parameter '{param_name}' declares 'primary-store' but "
                  f"no --primary-store flag provided", file=sys.stderr)
            sys.exit(1)

        ps_key = param_spec["primary-store"]
        ps_value = fetch_primary_store_value(primary_store_url, ps_key, param_name)
        if ps_value is not None:
            value = parse_value(ps_value, param_type, param_name, "primary-store")
            return value
        # If key not found (ps_value is None), fall through to next source

    # 5. Check default (lowest priority)
    if "default" in param_spec:
        value = parse_value(param_spec["default"], param_type, param_name, "default")
        return value

    # Unresolved
    return None


def main() -> None:
    """Main entry point."""
    primary_store_url, schema_file, arg_candidates = parse_args(sys.argv[1:])
    arg_values = parse_arg_candidates(arg_candidates)

    schema = load_schema(schema_file)
    primary_store_keys = validate_schema(schema)

    # Check if any parameter declares primary-store but no --primary-store flag is provided
    if primary_store_keys and primary_store_url is None:
        affected_params = list(primary_store_keys.values())
        if len(affected_params) == 1:
            print(f"Error: Parameter '{affected_params[0]}' declares 'primary-store' but "
                  f"no --primary-store flag provided", file=sys.stderr)
        else:
            print(f"Error: Parameters {', '.join(f"'{p}'" for p in affected_params)} declare "
                  f"'primary-store' but no --primary-store flag provided", file=sys.stderr)
        sys.exit(1)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []

    for param_name in schema:
        param_spec = schema[param_name]
        result = resolve_parameter(param_name, param_spec, arg_values, primary_store_url)

        if result is not None:
            resolved[param_name] = result
        else:
            unresolved.append(param_name)

    if unresolved:
        print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Output resolved configuration as JSON
    print(json.dumps(resolved))
    sys.exit(0)


if __name__ == "__main__":
    main()
