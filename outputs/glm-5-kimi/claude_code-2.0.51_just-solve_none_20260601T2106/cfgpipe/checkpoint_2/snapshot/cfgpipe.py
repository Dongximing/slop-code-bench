#!/usr/bin/env python3
"""cfgpipe - Core Configuration Resolver

Reads a JSON schema, resolves parameters from local sources
(default, env, file, primary-store, arg), and writes resolved configuration to stdout as JSON.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

_TRUTHY = frozenset(("true", "yes", "on", "1"))
_FALSY = frozenset(("false", "no", "off", "0"))

_TYPE_PARSERS = {
    "string": lambda v: v,
    "integer": lambda v: _parse_integer(v),
    "float": lambda v: str(float(v.strip())),
    "boolean": lambda v: _parse_boolean(v),
}


def _parse_boolean(value):
    lower = value.lower()
    if lower in _TRUTHY:
        return "true"
    if lower in _FALSY:
        return "false"
    raise ValueError(
        f"cannot parse '{value}' as boolean: "
        f"expected one of true/false/yes/no/on/off/1/0"
    )


def _parse_integer(value):
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty string cannot be parsed as integer")
    rest = stripped[1:] if stripped[0] in "+-" else stripped
    if not rest or not rest.isdigit():
        raise ValueError(f"cannot parse '{value}' as integer")
    int(stripped)
    return stripped


def parse_value(raw_value, param_type):
    parser = _TYPE_PARSERS.get(param_type)
    if parser is None:
        raise ValueError(f"unknown type '{param_type}'")
    return parser(raw_value)


def build_arg_map(arg_candidates):
    arg_map = {}
    for candidate in arg_candidates:
        for prefix in ("--", "-"):
            if candidate.startswith(prefix):
                rest = candidate[len(prefix):]
                if "=" in rest:
                    name, value = rest.split("=", 1)
                    arg_map[name] = value
                break
    return arg_map


def validate_schema(schema):
    if not isinstance(schema, dict):
        return "schema root must be a JSON object"

    source_fields = ("default", "env", "file", "arg")

    # Track primary-store keys for duplicate detection
    primary_store_keys = {}

    for name, declaration in schema.items():
        if not isinstance(declaration, dict):
            return f"parameter '{name}' declaration must be an object"

        if "type" not in declaration:
            return f"parameter '{name}' missing required 'type' field"

        if not isinstance(declaration["type"], str):
            return f"parameter '{name}' 'type' must be a string"

        for field in source_fields:
            if field in declaration and not isinstance(declaration[field], str):
                return f"parameter '{name}' field '{field}' must be a string"

        # Validate primary-store field
        if "primary-store" in declaration:
            if not isinstance(declaration["primary-store"], str):
                return f"parameter '{name}' field 'primary-store' must be a string"
            key = declaration["primary-store"]
            if key in primary_store_keys:
                return (f"duplicate primary-store key '{key}' "
                        f"declared by parameters '{primary_store_keys[key]}' and '{name}'")
            primary_store_keys[key] = name

    return None


def _read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return None


def _lookup_primary_store(base_url, key):
    """Look up a key from the primary store.

    Returns:
        (value_string, None) if key is found
        (None, None) if key is not found (404 with found:false)
        (None, error_message) on connector failure
    """
    encoded_key = urllib.parse.quote(key, safe="")
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                return (None, f"primary-store returned status {response.status}")

            body = response.read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return (None, "primary-store returned malformed JSON response")

            if not isinstance(data, dict):
                return (None, "primary-store returned non-object response")

            if "found" not in data:
                return (None, "primary-store response missing 'found' field")

            if data["found"] is False:
                return (None, None)  # Key not found, fall through

            if data["found"] is True:
                if "value" not in data:
                    return (None, "primary-store response missing 'value' field")
                if not isinstance(data["value"], str):
                    return (None, "primary-store 'value' must be a string")
                return (data["value"], None)

            return (None, "primary-store response has invalid 'found' field")

    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                body = e.read().decode("utf-8")
                data = json.loads(body)
                if isinstance(data, dict) and data.get("found") is False:
                    return (None, None)  # Key not found, fall through
                return (None, f"primary-store 404 response has invalid body")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return (None, f"primary-store returned 404 with malformed response")
        return (None, f"primary-store HTTP error: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        return (None, f"primary-store network error: {e.reason}")
    except Exception as e:
        return (None, f"primary-store error: {e}")


def resolve_parameter(name, declaration, arg_map, primary_store_base_url):
    """Resolve a single parameter from its sources.

    Returns (value_string, None) on success.
    Returns (None, error_message) on parse failure.
    Returns (None, None) if no source provides a value.
    """
    param_type = declaration["type"]

    # Check arg first (highest priority)
    if "arg" in declaration:
        arg_value = arg_map.get(declaration["arg"])
        if arg_value is not None:
            trimmed = arg_value.strip()
            if trimmed:
                try:
                    return (parse_value(trimmed, param_type), None)
                except ValueError as e:
                    return (None, f"parameter '{name}': failed to parse from source 'arg': {e}")

    # Check primary-store
    if "primary-store" in declaration and primary_store_base_url:
        key = declaration["primary-store"]
        value, error = _lookup_primary_store(primary_store_base_url, key)
        if error:
            return (None, f"parameter '{name}': {error}")
        if value is not None:
            try:
                return (parse_value(value, param_type), None)
            except ValueError as e:
                return (None, f"parameter '{name}': failed to parse from source 'primary-store': {e}")

    # Check file
    if "file" in declaration:
        file_value = _read_file_source(declaration["file"])
        if file_value is not None:
            trimmed = file_value.strip()
            if trimmed:
                try:
                    return (parse_value(trimmed, param_type), None)
                except ValueError as e:
                    return (None, f"parameter '{name}': failed to parse from source 'file': {e}")

    # Check env
    if "env" in declaration:
        env_value = os.environ.get(declaration["env"])
        if env_value is not None:
            trimmed = env_value.strip()
            if trimmed:
                try:
                    return (parse_value(trimmed, param_type), None)
                except ValueError as e:
                    return (None, f"parameter '{name}': failed to parse from source 'env': {e}")

    # Check default (lowest priority)
    if "default" in declaration:
        default_value = declaration["default"]
        if default_value is not None:
            trimmed = default_value.strip()
            if trimmed:
                try:
                    return (parse_value(trimmed, param_type), None)
                except ValueError as e:
                    return (None, f"parameter '{name}': failed to parse from source 'default': {e}")

    return (None, None)


def parse_global_flags(args):
    """Parse global flags from command line arguments.

    Returns:
        (remaining_args, primary_store_url, error_message)
    """
    primary_store_url = None
    remaining = []

    i = 0
    while i < len(args):
        arg = args[i]

        if arg == "--primary-store":
            if i + 1 >= len(args):
                return (None, None, "error: --primary-store requires a value")
            primary_store_url = args[i + 1]
            i += 2
            continue

        if arg.startswith("--primary-store="):
            primary_store_url = arg.split("=", 1)[1]
            i += 1
            continue

        # First non-flag argument is the schema file
        remaining.append(arg)
        i += 1

    return (remaining, primary_store_url, None)


def main():
    args = sys.argv[1:]

    if not args:
        print("error: no schema file specified", file=sys.stderr)
        sys.exit(1)

    # Parse global flags
    remaining_args, primary_store_url, flag_error = parse_global_flags(args)
    if flag_error:
        print(flag_error, file=sys.stderr)
        sys.exit(1)

    if not remaining_args:
        print("error: no schema file specified", file=sys.stderr)
        sys.exit(1)

    schema_path = remaining_args[0]
    arg_candidates = remaining_args[1:]

    try:
        with open(schema_path, "r") as f:
            schema = json.load(f)
    except FileNotFoundError:
        print(f"error: schema file '{schema_path}' not found", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in schema file '{schema_path}': {e}", file=sys.stderr)
        sys.exit(1)

    validation_error = validate_schema(schema)
    if validation_error:
        print(f"error: {validation_error}", file=sys.stderr)
        sys.exit(1)

    # Check if any parameter declares primary-store but --primary-store is not configured
    params_with_primary_store = [
        name for name, decl in schema.items()
        if "primary-store" in decl
    ]

    if params_with_primary_store and not primary_store_url:
        param_name = params_with_primary_store[0]
        print(f"error: parameter '{param_name}' declares primary-store but --primary-store is not configured",
              file=sys.stderr)
        sys.exit(1)

    arg_map = build_arg_map(arg_candidates)

    resolved = {}
    unresolved = []

    for name, declaration in schema.items():
        value, error = resolve_parameter(name, declaration, arg_map, primary_store_url)

        if error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)

        if value is None:
            unresolved.append(name)
        else:
            resolved[name] = value

    if unresolved:
        names = ", ".join(sorted(unresolved))
        print(f"error: unresolved parameters: {names}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(resolved))


if __name__ == "__main__":
    main()
