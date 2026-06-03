#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver.

Supports:
- Nested parameter groups with dotted paths
- Custom parameter types (port)
- Stricter schema validation
- Source resolution (default, env, file, arg, primary-store)
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote


def parse_args(args: list[str]) -> tuple[str | None, str | None, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--primary-store', dest='primary_store', help='Primary store base URL')
    parser.add_argument('remaining', nargs=argparse.REMAINDER, help='Schema file and remaining args')

    known, unknown = parser.parse_known_args(args)
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
            name_part, value = arg.split("=", 1)
            name = name_part.lstrip("-")
            result[name] = value

    return result


def load_schema(schema_file: str) -> dict[str, Any]:
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


def validate_schema_recursive(
    schema: dict[str, Any],
    path: str,
    primary_store_keys: dict[str, str],
    is_root: bool = False
) -> None:
    """Validate schema recursively, building up the full composed path.

    Args:
        schema: The schema to validate (could be root or nested group)
        path: The current composed path (empty for root)
        primary_store_keys: Dictionary to track primary-store keys across all params
        is_root: Whether this is the root schema object
    """
    if not isinstance(schema, dict):
        print(f"Error: Group '{path or 'root'}' must be a JSON object", file=sys.stderr)
        sys.exit(1)

    if is_root and not schema:
        print(f"Error: Schema root must be a non-empty JSON object", file=sys.stderr)
        sys.exit(1)

    for key, value in schema.items():
        full_path = f"{path}.{key}" if path else key

        if not isinstance(value, dict):
            print(f"Error: Entry '{full_path}' must be an object (parameter or group)", file=sys.stderr)
            sys.exit(1)

        # Check if this is a parameter (has string-valued 'type' field) or a group
        if "type" in value:
            if isinstance(value["type"], str):
                # This is a parameter declaration
                validate_parameter(value, full_path, primary_store_keys)
            else:
                print(f"Error: Entry '{full_path}' has non-string 'type' field", file=sys.stderr)
                sys.exit(1)
        else:
            # This is a group (no type field)
            source_fields = {"default", "env", "file", "arg", "primary-store"}
            # Check if it has any source annotation fields - not allowed on groups
            for field in value:
                if field in source_fields:
                    print(f"Error: Group '{full_path}' carries source annotation '{field}'", file=sys.stderr)
                    sys.exit(1)
            # Recurse into nested group
            validate_schema_recursive(value, full_path, primary_store_keys)


def validate_parameter(
    param_spec: dict[str, Any],
    param_path: str,
    primary_store_keys: dict[str, str]
) -> None:
    """Validate a single parameter declaration."""
    param_type = param_spec.get("type")

    if not isinstance(param_type, str):
        print(f"Error: Parameter '{param_path}' missing or has non-string 'type' field", file=sys.stderr)
        sys.exit(1)

    # Validate type
    valid_builtin_types = {"string", "integer", "float", "boolean"}
    valid_custom_types = {"port"}
    all_valid_types = valid_builtin_types | valid_custom_types

    if param_type not in all_valid_types:
        print(f"Error: Parameter '{param_path}' has unrecognized type '{param_type}'", file=sys.stderr)
        sys.exit(1)

    # Validate source fields are strings
    source_fields = {"default", "env", "file", "arg", "primary-store"}
    for field in source_fields:
        if field in param_spec:
            if not isinstance(param_spec[field], str):
                print(f"Error: Parameter '{param_path}' has non-string '{field}' field", file=sys.stderr)
                sys.exit(1)

    # Track primary-store keys for duplicate detection across all parameters
    if "primary-store" in param_spec:
        ps_key = param_spec["primary-store"]
        if ps_key in primary_store_keys:
            existing_path = primary_store_keys[ps_key]
            print(f"Error: Parameters '{existing_path}' and '{param_path}' share duplicate 'primary-store' key '{ps_key}'", file=sys.stderr)
            sys.exit(1)
        primary_store_keys[ps_key] = param_path


def parse_value(value: str, expected_type: str, param_name: str, source: str) -> str:
    """Parse and validate a string value against the expected type.
    Returns the normalized value as string."""
    stripped = value.strip()
    if not stripped:
        print(f"Error: Parameter '{param_name}' from source '{source}' has empty value", file=sys.stderr)
        sys.exit(1)

    if expected_type == "string":
        return stripped

    elif expected_type == "integer":
        if 'e' in stripped.lower() or 'E' in stripped:
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid integer: scientific notation not supported", file=sys.stderr)
            sys.exit(1)
        try:
            int_val = int(stripped)
            # Check for leading zeros (except for 0 itself)
            if stripped != "0" and stripped.startswith("0"):
                print(f"Error: Parameter '{param_name}' from source '{source}' has integer with leading zeros: '{stripped}'", file=sys.stderr)
                sys.exit(1)
            return str(int_val)
        except ValueError:
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid integer: '{stripped}'", file=sys.stderr)
            sys.exit(1)

    elif expected_type == "float":
        try:
            float_val = float(stripped)
            # Normalize to avoid scientific notation
            return str(float_val)
        except ValueError:
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid float: '{stripped}'", file=sys.stderr)
            sys.exit(1)

    elif expected_type == "boolean":
        lower = stripped.lower()
        if lower not in ("true", "false"):
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid boolean: '{stripped}'", file=sys.stderr)
            sys.exit(1)
        return lower

    elif expected_type == "port":
        if 'e' in stripped.lower() or 'E' in stripped:
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid port: scientific notation not supported", file=sys.stderr)
            sys.exit(1)
        try:
            port = int(stripped)
            if port < 0 or port > 65535:
                print(f"Error: Parameter '{param_name}' from source '{source}' has invalid port: {port} (must be 0-65535)", file=sys.stderr)
                sys.exit(1)
            # Render as plain decimal with no leading zeros except 0
            if stripped != "0" and stripped.startswith("0"):
                print(f"Error: Parameter '{param_name}' from source '{source}' has port with leading zeros: '{stripped}'", file=sys.stderr)
                sys.exit(1)
            return str(port)
        except ValueError:
            print(f"Error: Parameter '{param_name}' from source '{source}' has invalid port: '{stripped}'", file=sys.stderr)
            sys.exit(1)

    # Should not happen with validated schema
    print(f"Error: Parameter '{param_name}' from source '{source}' has unknown type '{expected_type}'", file=sys.stderr)
    sys.exit(1)


def resolve_parameter_recursive(
    schema: dict[str, Any],
    path: str,
    arg_values: dict[str, str],
    primary_store_url: str | None
) -> dict[str, Any]:
    """Resolve configuration recursively, building nested output structure."""
    result: dict[str, Any] = {}

    for key, value in schema.items():
        full_path = f"{path}.{key}" if path else key

        if "type" in value and isinstance(value["type"], str):
            # This is a parameter - resolve it to a leaf value
            resolved_value = resolve_parameter_leaf(value, full_path, arg_values, primary_store_url)
            result[key] = resolved_value
        else:
            # This is a group - recurse into it
            result[key] = resolve_parameter_recursive(value, full_path, arg_values, primary_store_url)

    return result


def resolve_parameter_leaf(
    param_spec: dict[str, Any],
    param_name: str,
    arg_values: dict[str, str],
    primary_store_url: str | None
) -> str | None:
    """Resolve a single parameter to its leaf string value.
    Returns None if unresolved."""
    param_type = param_spec["type"]

    # Priority order: arg (highest), file, env, primary-store, default (lowest)

    # 1. Check command-line argument
    if "arg" in param_spec:
        arg_name = param_spec["arg"]
        if arg_name in arg_values:
            arg_value = arg_values[arg_name]
            if arg_value.strip():
                return parse_value(arg_value, param_type, param_name, "arg")

    # 2. Check file
    if "file" in param_spec:
        file_path = param_spec["file"]
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
        except (FileNotFoundError, OSError, IsADirectoryError):
            pass  # File doesn't exist or can't be read
        else:
            stripped = file_content.strip()
            if stripped:
                return parse_value(stripped, param_type, param_name, "file")

    # 3. Check environment variable
    if "env" in param_spec:
        env_value = os.environ.get(param_spec["env"])
        if env_value is not None and env_value.strip():
            return parse_value(env_value, param_type, param_name, "env")

    # 4. Check primary-store
    if "primary-store" in param_spec:
        if primary_store_url is None:
            print(f"Error: Parameter '{param_name}' declares 'primary-store' but no --primary-store flag provided", file=sys.stderr)
            sys.exit(1)

        ps_key = param_spec["primary-store"]
        ps_value = fetch_primary_store_value(primary_store_url, ps_key, param_name)
        if ps_value is not None:
            return parse_value(ps_value, param_type, param_name, "primary-store")

    # 5. Check default
    if "default" in param_spec:
        return parse_value(param_spec["default"], param_type, param_name, "default")

    # Unresolved
    print(f"Error: Unresolved parameter '{param_name}'", file=sys.stderr)
    sys.exit(1)


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
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: malformed JSON response: {e}", file=sys.stderr)
                    sys.exit(1)

                if not isinstance(data, dict):
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: expected JSON object", file=sys.stderr)
                    sys.exit(1)

                if data.get("found") is True:
                    value = data.get("value")
                    if value is None:
                        print(f"Error: Parameter '{param_name}' primary-store connector failure: missing 'value' field", file=sys.stderr)
                        sys.exit(1)
                    return str(value)
                elif data.get("found") is False:
                    return None
                else:
                    print(f"Error: Parameter '{param_name}' primary-store connector failure: missing or invalid 'found' field", file=sys.stderr)
                    sys.exit(1)
            else:
                print(f"Error: Parameter '{param_name}' primary-store connector failure: HTTP {response.status}", file=sys.stderr)
                sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Error: Parameter '{param_name}' primary-store connector failure: network error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: Parameter '{param_name}' primary-store connector failure: unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Main entry point."""
    primary_store_url, schema_file, arg_candidates = parse_args(sys.argv[1:])
    arg_values = parse_arg_candidates(arg_candidates)

    schema = load_schema(schema_file)

    # Validate schema and track primary-store keys
    primary_store_keys: dict[str, str] = {}
    validate_schema_recursive(schema, "", primary_store_keys, is_root=True)

    # Check if any parameter declares primary-store but no --primary-store flag is provided
    if primary_store_keys and primary_store_url is None:
        params_list = list(primary_store_keys.values())
        if len(params_list) == 1:
            print(f"Error: Parameter '{params_list[0]}' declares 'primary-store' but no --primary-store flag provided", file=sys.stderr)
        else:
            params_str = ', '.join(repr(p) for p in params_list)
            print(f"Error: Parameters {params_str} declare 'primary-store' but no --primary-store flag provided", file=sys.stderr)
        sys.exit(1)

    # Resolve configuration recursively
    resolved = resolve_parameter_recursive(schema, "", arg_values, primary_store_url)

    # Output resolved configuration as JSON
    print(json.dumps(resolved))
    sys.exit(0)


if __name__ == "__main__":
    main()
