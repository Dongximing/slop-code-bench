#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver with nested groups and custom types."""

import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from typing import Any


def parse_cli_args(args: list[str]) -> tuple[dict[str, str], str | None]:
    """Parse CLI arguments in --name=value or -name=value format.

    Also extracts --primary-store <base-url> as a special global flag.
    Returns (arg_dict, primary_store_url) where primary_store_url may be None.
    """
    result = {}
    primary_store_url = None
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg == '--primary-store':
            skip_next = True
            continue
        if '=' in arg:
            name, value = arg.split('=', 1)
            result[name.lstrip("-")] = value
    return result, primary_store_url


def get_env_value(env_name: str) -> str | None:
    return os.environ.get(env_name, "").strip() or None


def get_file_value(file_path: str) -> str | None:
    """Return file content if it exists as a regular file with non-empty content."""
    try:
        if not os.path.isfile(file_path):
            return None
        return open(file_path).read().strip() or None
    except (OSError, IOError):
        return None


def validate_schema_recursive(
    schema: dict[str, Any],
    current_path: str,
    errors: list[str],
    primary_store_map: dict[str, list[str]]
) -> None:
    """Recursively validate schema structure.

    Collects all schema errors and tracks primary-store key usage across full hierarchy.
    """
    if not isinstance(schema, dict):
        errors.append(f"{current_path}: schema root must be a JSON object")
        return

    if not schema:
        if current_path == "":
            errors.append("schema root must be non-empty")
        return

    seen_keys = set()

    for key, value in schema.items():
        # Check for duplicate keys within same group
        if key in seen_keys:
            errors.append(f"{compose_path(current_path, key)}: duplicate parameter/group name")
        seen_keys.add(key)

        path = compose_path(current_path, key)

        if isinstance(value, dict):
            # Check if this is a parameter declaration or a group
            has_type = 'type' in value

            if has_type:
                # This is a parameter declaration
                type_val = value['type']

                # Check type is recognized
                if not is_valid_type(type_val):
                    errors.append(f"{path}: unrecognized type '{type_val}'")

                # Check source-annotation keys are strings
                for field in ['default', 'env', 'file', 'arg']:
                    if field in value and not isinstance(value[field], str):
                        errors.append(f"{path}: '{field}' field must be a string")

                # Track primary-store keys across full hierarchy
                if 'primary-store' in value:
                    if not isinstance(value['primary-store'], str):
                        errors.append(f"{path}: 'primary-store' field must be a string")
                    else:
                        pstore_key = value['primary-store']
                        if pstore_key not in primary_store_map:
                            primary_store_map[pstore_key] = []
                        primary_store_map[pstore_key].append(path)
            else:
                # This is a group - must not have source-annotation keys with non-object values
                # Actually, groups must not have ANY source-annotation keys at all (with non-object value)
                # According to spec: "must not contain any source-annotation key with a non-object value"
                # Since group is object-valued itself, any source-annotation key would be invalid
                for field in ['default', 'env', 'file', 'arg', 'primary-store']:
                    if field in value:
                        errors.append(f"{path}: group carries source annotation '{field}'")
                # Recursively validate the group contents
                validate_schema_recursive(value, path, errors, primary_store_map)
        else:
            # Value is not an object - invalid
            errors.append(f"{path}: must be an object (parameter declaration or group)")


def compose_path(parent: str, child: str) -> str:
    """Compose a dotted path from parent and child."""
    if parent == "":
        return child
    return f"{parent}.{child}"


def is_valid_type(type_name: str) -> bool:
    """Check if type is a recognized built-in or custom type."""
    built_in = {'string', 'integer', 'float', 'boolean'}
    custom = {'port'}
    return type_name in built_in or type_name in custom


def validate_schema(schema: dict[str, Any]) -> list[str]:
    """Validate schema structure. Return list of error messages (empty if valid)."""
    errors = []
    primary_store_map: dict[str, list[str]] = {}

    validate_schema_recursive(schema, "", errors, primary_store_map)

    # Check for duplicate primary-store keys across full hierarchy
    for key, params in primary_store_map.items():
        if len(params) > 1:
            errors.append(f"duplicate primary-store key '{key}' used by: {', '.join(params)}")

    return errors


def parse_value(value: str, type_name: str) -> str:
    """Parse a value against a type and return canonical string representation.

    Leaf values remain strings per specification.
    Custom types also return canonical string representation.
    """
    if type_name == 'string':
        return value
    if type_name == 'integer':
        if '.' in value:
            raise ValueError("integer type does not accept decimal values")
        int(value)
        return value
    if type_name == 'float':
        if 'e' in value.lower():
            raise ValueError("scientific notation not allowed")
        float(value)
        return value
    if type_name == 'boolean':
        if value.lower() in {'true', 'yes', 'on', '1', 't', 'y'}:
            return 'true'
        if value.lower() in {'false', 'no', 'off', '0', 'f', 'n'}:
            return 'false'
        raise ValueError(f"not a valid boolean representation: {value}")
    if type_name == 'port':
        # Custom type: port
        if not value.isdigit():
            raise ValueError("port must be a base-10 integer")
        port_num = int(value)
        if port_num < 0 or port_num > 65535:
            raise ValueError(f"port out of range (0-65535): {value}")
        # Render as plain decimal with no leading zeros except '0'
        return str(port_num)
    raise ValueError(f"unknown type: {type_name}")


def primary_store_lookup(base_url: str, key: str) -> str | None:
    """Look up a key in the primary store.

    Returns value string if found, None if not found (404 with found:false).
    Raises RuntimeError for connector failures.
    """
    url = f"{base_url}/v1/primary/kv?key={urllib.parse.quote(key, safe='')}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                data = json.loads(e.read().decode('utf-8'))
                if not data.get('found', False):
                    return None
            except (json.JSONDecodeError, ValueError):
                pass
        raise RuntimeError(f"primary store returned status {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"primary store connection failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"primary store response malformed: {e}")

    if not data.get('found', False):
        return None
    return data.get('value')


def resolve_parameter_with_primary(
    path: str,
    decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
) -> tuple[str | None, str | None]:
    """Resolve parameter with primary-store support.

    Priority: arg -> primary-store -> file -> env -> default
    """
    # Check arg sources
    key = decl.get('arg', path)
    val = cli_args.get(key)
    if val is not None:
        return val, 'arg'

    # Check primary-store if configured
    if primary_store_url is not None and 'primary-store' in decl:
        try:
            val = primary_store_lookup(primary_store_url, decl['primary-store'])
            if val is not None:
                return val, 'primary-store'
        except RuntimeError:
            raise

    # Check fallback sources
    if 'file' in decl:
        val = get_file_value(decl['file'])
        if val is not None:
            return val, 'file'
    if 'env' in decl:
        val = get_env_value(decl['env'])
        if val is not None:
            return val, 'env'
    if 'default' in decl:
        return decl['default'], 'default'
    return None, None


def resolve_schema(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve schema into configuration value.

    Returns (result, errors) where result is None if resolution failed.
    Errors contain path, source, and detail information.
    """
    result = {}
    errors = []

    def resolve_node(node: dict[str, Any], current_path: str) -> dict[str, Any] | None:
        """Recursively resolve a schema node.

        Returns a dict for the resolved subtree, or None if errors occurred.
        """
        output = {}

        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key

            if isinstance(value, dict) and 'type' in value:
                # This is a parameter declaration
                try:
                    value_str, source = resolve_parameter_with_primary(
                        path, value, cli_args, primary_store_url
                    )
                except RuntimeError as e:
                    errors.append({
                        'path': path,
                        'source': 'primary-store',
                        'detail': str(e)
                    })
                    return None

                if value_str is None:
                    errors.append({
                        'path': path,
                        'source': None,
                        'detail': 'unresolved parameter: no source provided value'
                    })
                    return None

                try:
                    resolved_val = parse_value(value_str, value['type'])
                    output[key] = resolved_val
                except ValueError as e:
                    errors.append({
                        'path': path,
                        'source': source,
                        'detail': str(e)
                    })
                    return None

            elif isinstance(value, dict):
                # This is a group - recursively resolve
                resolved_group = resolve_node(value, path)
                if resolved_group is None:
                    return None
                output[key] = resolved_group

        return output

    result = resolve_node(schema, "")
    return result, errors


def check_primary_store_config(schema: dict[str, Any], primary_store_url: str | None) -> str | None:
    """Validate primary-store configuration.

    If any parameter declares primary-store but --primary-store is absent,
    return an error naming the affected parameter.
    """
    if primary_store_url is None:
        params_with_primary = []

        def find_primary_params(node: dict[str, Any], current_path: str) -> None:
            for key, value in node.items():
                path = compose_path(current_path, key) if current_path else key
                if isinstance(value, dict) and 'type' in value:
                    if 'primary-store' in value:
                        params_with_primary.append(path)
                elif isinstance(value, dict):
                    find_primary_params(value, path)

        find_primary_params(schema, "")
        if params_with_primary:
            return f"schema error: parameters with 'primary-store' require --primary-store flag: {', '.join(params_with_primary)}"
    return None


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    # First, find the schema file position (first arg not starting with -,
    # skipping over known flag pairs like --primary-store <url>)
    schema_path = None
    schema_idx = None
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == '--primary-store' and i + 1 < len(sys.argv):
            # Skip the URL argument too
            i += 2
        elif not arg.startswith('-'):
            schema_path = arg
            schema_idx = i
            break
        else:
            i += 1

    if schema_path is None:
        print("Error: no schema file specified", file=sys.stderr)
        return 1

    # Separate global flags (before schema) from arg candidates (after schema)
    global_args = sys.argv[1:schema_idx]
    arg_candidates = sys.argv[schema_idx + 1:]

    # Parse global flags for --primary-store
    primary_store_url = None
    g = iter(global_args)
    for arg in g:
        if arg == '--primary-store':
            try:
                primary_store_url = next(g)
            except StopIteration:
                print("Error: --primary-store requires a URL argument", file=sys.stderr)
                return 1

    # Parse arg candidates for CLI args (--name=value style)
    cli_args, _ = parse_cli_args(arg_candidates)

    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except OSError as e:
        print(f"Error reading schema file: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON schema: {e}", file=sys.stderr)
        return 1

    # Schema validation (before resolution)
    validation_errors = validate_schema(schema)
    if validation_errors:
        for err in validation_errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    # Check primary-store config
    err = check_primary_store_config(schema, primary_store_url)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    # Resolve the schema
    resolved, errors = resolve_schema(schema, cli_args, primary_store_url)

    if errors:
        for err in errors:
            path = err['path']
            source = err.get('source')
            detail = err['detail']
            if source:
                print(f"Error: '{path}' from source '{source}': {detail}", file=sys.stderr)
            else:
                print(f"Error: {path}: {detail}", file=sys.stderr)
        return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
