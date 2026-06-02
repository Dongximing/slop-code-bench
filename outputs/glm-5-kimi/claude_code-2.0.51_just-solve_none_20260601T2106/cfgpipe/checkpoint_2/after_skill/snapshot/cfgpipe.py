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

_SOURCE_FIELDS = ("default", "env", "file", "arg")


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


def _parse_float(value):
    return str(float(value.strip()))


_TYPE_PARSERS = {
    "string": lambda v: v,
    "integer": _parse_integer,
    "float": _parse_float,
    "boolean": _parse_boolean,
}


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


def _read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return None


def _parse_primary_store_response(data):
    """Validate and extract value from a primary-store response dict.

    Returns (found: bool, value: str | None, error: str | None).
    """
    if not isinstance(data, dict):
        return (False, None, "primary-store returned non-object response")
    if "found" not in data:
        return (False, None, "primary-store response missing 'found' field")
    if data["found"] is False:
        return (False, None, None)
    if not data.get("found"):
        return (False, None, "primary-store response has invalid 'found' field")
    if "value" not in data:
        return (True, None, "primary-store response missing 'value' field")
    if not isinstance(data["value"], str):
        return (True, None, "primary-store 'value' must be a string")
    return (True, data["value"], None)


def _parse_json_body(body):
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _lookup_primary_store(base_url, key):
    """Look up a key from the primary store.

    Returns:
        (value_string, None) if key is found
        (None, None) if key is not found
        (None, error_message) on failure
    """
    encoded_key = urllib.parse.quote(key, safe="")
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                return (None, f"primary-store returned status {response.status}")
            data = _parse_json_body(response.read().decode("utf-8"))
            if data is None:
                return (None, "primary-store returned malformed JSON response")
            found, value, error = _parse_primary_store_response(data)
            if error:
                return (None, error)
            return (value, None)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = _parse_json_body(e.read().decode("utf-8"))
            if isinstance(data, dict) and data.get("found") is False:
                return (None, None)
            return (None, "primary-store returned 404 with malformed response")
        return (None, f"primary-store HTTP error: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        return (None, f"primary-store network error: {e.reason}")
    except Exception as e:
        return (None, f"primary-store error: {e}")


def _try_parse(raw_value, param_type, name, source_name):
    """Try to parse a raw string value. Returns (parsed, error)."""
    try:
        return (parse_value(raw_value, param_type), None)
    except ValueError as e:
        return (None, f"parameter '{name}': failed to parse from source '{source_name}': {e}")


def resolve_parameter(name, declaration, arg_map, primary_store_base_url):
    """Resolve a single parameter from its sources.

    Returns (value_string, None) on success.
    Returns (None, error_message) on parse failure.
    Returns (None, None) if no source provides a value.
    """
    param_type = declaration["type"]
    primary_store_key = declaration.get("primary-store")

    sources = []

    if "arg" in declaration:
        sources.append(("arg", arg_map.get(declaration["arg"])))
    if primary_store_key and primary_store_base_url:
        value, error = _lookup_primary_store(primary_store_base_url, primary_store_key)
        if error:
            return (None, f"parameter '{name}': {error}")
        sources.append(("primary-store", value))
    if "file" in declaration:
        sources.append(("file", _read_file_source(declaration["file"])))
    if "env" in declaration:
        sources.append(("env", os.environ.get(declaration["env"])))
    if "default" in declaration:
        sources.append(("default", declaration["default"]))

    for source_name, raw_value in sources:
        if raw_value is None:
            continue
        trimmed = raw_value.strip()
        if trimmed:
            parsed, error = _try_parse(trimmed, param_type, name, source_name)
            if error:
                return (None, error)
            return (parsed, None)

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

        remaining.append(arg)
        i += 1

    return (remaining, primary_store_url, None)


def main():
    args = sys.argv[1:]

    if not args:
        print("error: no schema file specified", file=sys.stderr)
        sys.exit(1)

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

    if not isinstance(schema, dict):
        print("error: schema root must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # Single pass: validate declarations and check primary-store requirements
    primary_store_keys = {}
    has_primary_store_param = False

    for name, declaration in schema.items():
        if not isinstance(declaration, dict):
            print(f"error: parameter '{name}' declaration must be an object", file=sys.stderr)
            sys.exit(1)

        if "type" not in declaration:
            print(f"error: parameter '{name}' missing required 'type' field", file=sys.stderr)
            sys.exit(1)

        if not isinstance(declaration["type"], str):
            print(f"error: parameter '{name}' 'type' must be a string", file=sys.stderr)
            sys.exit(1)

        for field in _SOURCE_FIELDS:
            if field in declaration and not isinstance(declaration[field], str):
                print(f"error: parameter '{name}' field '{field}' must be a string", file=sys.stderr)
                sys.exit(1)

        if "primary-store" in declaration:
            if not isinstance(declaration["primary-store"], str):
                print(f"error: parameter '{name}' field 'primary-store' must be a string", file=sys.stderr)
                sys.exit(1)
            key = declaration["primary-store"]
            if key in primary_store_keys:
                print(
                    f"error: duplicate primary-store key '{key}' "
                    f"declared by parameters '{primary_store_keys[key]}' and '{name}'",
                    file=sys.stderr,
                )
                sys.exit(1)
            primary_store_keys[key] = name
            has_primary_store_param = True

    if has_primary_store_param and not primary_store_url:
        first_param = next(
            name for name, decl in schema.items() if "primary-store" in decl
        )
        print(
            f"error: parameter '{first_param}' declares primary-store but --primary-store is not configured",
            file=sys.stderr,
        )
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
