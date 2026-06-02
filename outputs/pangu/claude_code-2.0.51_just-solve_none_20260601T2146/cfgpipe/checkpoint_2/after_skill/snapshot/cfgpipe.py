#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver."""

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


def validate_schema(schema: dict[str, Any]) -> str | None:
    """Validate schema structure. Return error message or None if valid.

    Also checks for conflicting primary-store keys.
    """
    seen = set()
    primary_store_keys = {}  # key -> list of parameter names
    valid_types = {'string', 'integer', 'float', 'boolean'}

    for name, decl in schema.items():
        if name in seen:
            return f"duplicate parameter name: {name}"
        seen.add(name)

        if not isinstance(decl, dict):
            return f"parameter '{name}' must be an object"

        if 'type' not in decl:
            return f"parameter '{name}' is missing required 'type' field"

        if decl['type'] not in valid_types:
            return f"unknown type: {decl['type']}"

        for field in ['default', 'env', 'file', 'arg']:
            if field in decl and not isinstance(decl[field], str):
                return f"parameter '{name}' has non-string '{field}' field"

        if 'primary-store' in decl:
            if not isinstance(decl['primary-store'], str):
                return f"parameter '{name}' has non-string 'primary-store' field"
            key = decl['primary-store']
            if key not in primary_store_keys:
                primary_store_keys[key] = []
            primary_store_keys[key].append(name)

    # Check for conflicting primary-store keys
    for key, params in primary_store_keys.items():
        if len(params) > 1:
            return f"schema error: primary-store key '{key}' used by multiple parameters: {', '.join(params)}"

    return None


def parse_value(value: str, type_name: str) -> str:
    """Parse a value against a type and return canonical string representation."""
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


def _resolve_arg(name: str, decl: dict[str, Any], cli_args: dict[str, str]) -> tuple[str | None, str | None] | None:
    """Resolve arg source. Returns (value, source) or None."""
    key = decl.get('arg', name)
    val = cli_args.get(key)
    if val is not None:
        return val, 'arg'
    return None


def _resolve_fallback(name: str, decl: dict[str, Any]) -> tuple[str | None, str | None] | None:
    """Resolve fallback sources. Returns (value, source) or None."""
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
    return None


def resolve_parameter_with_primary(
    name: str,
    decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
) -> tuple[str | None, str | None]:
    """Resolve parameter with primary-store support.

    Priority: arg -> primary-store -> file -> env -> default
    """
    # Check arg sources
    result = _resolve_arg(name, decl, cli_args)
    if result:
        return result

    # Check primary-store if configured
    if primary_store_url is not None and 'primary-store' in decl:
        try:
            val = primary_store_lookup(primary_store_url, decl['primary-store'])
            if val is not None:
                return val, 'primary-store'
        except RuntimeError:
            raise

    # Check fallback sources
    result = _resolve_fallback(name, decl)
    if result:
        return result

    return None, None


def check_primary_store_config(schema: dict[str, Any], primary_store_url: str | None) -> str | None:
    """Validate primary-store configuration.

    If any parameter declares primary-store but --primary-store is absent,
    return an error naming the affected parameter.
    """
    if primary_store_url is None:
        params_with_primary = [
            name for name, decl in schema.items() if 'primary-store' in decl
        ]
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

    if not isinstance(schema, dict):
        print("Error: schema must be a JSON object", file=sys.stderr)
        return 1

    err = validate_schema(schema)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    # Check primary-store config: if any parameter declares primary-store
    # but --primary-store flag is absent, that's a fatal error.
    err = check_primary_store_config(schema, primary_store_url)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    resolved = {}

    for name, decl in schema.items():
        try:
            value, source = resolve_parameter_with_primary(name, decl, cli_args, primary_store_url)
        except RuntimeError as e:
            # Fatal connector error from primary store
            print(f"Error: primary store error for parameter '{name}': {e}", file=sys.stderr)
            return 1

        if value is None:
            print(f"Error: unresolved parameter: {name}", file=sys.stderr)
            return 1

        try:
            resolved[name] = parse_value(value, decl['type'])
        except ValueError as e:
            print(f"Error: parameter '{name}' from source '{source}': {e}", file=sys.stderr)
            return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
