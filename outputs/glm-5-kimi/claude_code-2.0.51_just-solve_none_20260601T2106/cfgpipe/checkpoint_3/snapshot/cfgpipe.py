#!/usr/bin/env python3
"""cfgpipe - Core Configuration Resolver

Reads a JSON schema, resolves parameters from local sources
(default, env, file, primary-store, arg), and writes resolved configuration to stdout as JSON.

Supports parameter groups (nested objects), custom parameter types (port),
and strict schema validation with composed dotted paths.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse

_TRUTHY = frozenset(("true", "yes", "on", "1"))
_FALSY = frozenset(("false", "no", "off", "0"))

_SOURCE_FIELDS = ("default", "env", "file", "arg", "primary-store")

_BUILTIN_TYPES = frozenset(("string", "integer", "float", "boolean"))
_CUSTOM_TYPES = frozenset(("port",))
_ALL_TYPES = _BUILTIN_TYPES | _CUSTOM_TYPES


# ---------------------------------------------------------------------------
# Type parsers
# ---------------------------------------------------------------------------

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


def _parse_port(value):
    stripped = value.strip()
    try:
        num = int(stripped, 10)
    except ValueError:
        raise ValueError(f"cannot parse '{value}' as port: not a valid integer")
    if num < 0 or num > 65535:
        raise ValueError(
            f"port value {num} out of range: must be 0-65535"
        )
    # Render as plain decimal with no leading zeros except "0"
    return str(num)


_TYPE_PARSERS = {
    "string": lambda v: v,
    "integer": _parse_integer,
    "float": _parse_float,
    "boolean": _parse_boolean,
    "port": _parse_port,
}


def parse_value(raw_value, param_type):
    parser = _TYPE_PARSERS.get(param_type)
    if parser is None:
        raise ValueError(f"unknown type '{param_type}'")
    return parser(raw_value)


# ---------------------------------------------------------------------------
# Arg parsing helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------

def _read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return None


def _parse_primary_store_response(data):
    """Validate and extract value from a primary-store response dict."""
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
    """Look up a key from the primary store."""
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


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def _is_parameter_declaration(obj):
    """Check if an object looks like a parameter declaration (has 'type' key).

    Note: This checks if the object HAS a type field, but caller must verify
    it doesn't have nested children (which would make it an invalid group).
    """
    return isinstance(obj, dict) and "type" in obj and isinstance(obj["type"], str)


def _has_nested_children(obj):
    """Check if an object has nested child objects (not just source annotations)."""
    for key, val in obj.items():
        if key not in _SOURCE_FIELDS and key != "type":
            if isinstance(val, dict):
                return True
    return False


def _validate_schema(node, path, primary_store_map, errors):
    """Recursively validate the schema, collecting parameter declarations.

    Args:
        node: The current schema node (dict).
        path: The composed dotted path to this node (empty string for root).
        primary_store_map: dict mapping primary-store key -> list of composed paths.
        errors: list to collect error strings.

    Returns:
        list of (composed_path, declaration_dict) for all parameter declarations found.
    """
    if not isinstance(node, dict):
        errors.append(f"schema node '{path}' must be an object" if path else "schema root must be a JSON object")
        return []

    # Determine if this is a parameter declaration or a group
    # If it has a string-valued type AND no nested children, it's a parameter
    # If it has a string-valued type AND nested children, it's a group with an illegal type
    # If no string-valued type, it's a group
    if _is_parameter_declaration(node) and not _has_nested_children(node):
        # Validate the type is recognized
        ptype = node["type"]
        if ptype not in _ALL_TYPES:
            display_path = path if path else "(root)"
            errors.append(f"parameter '{display_path}': unrecognized type '{ptype}'")
            return []

        # Validate source fields are strings
        for field in _SOURCE_FIELDS:
            if field in node and not isinstance(node[field], str):
                errors.append(f"parameter '{path}': field '{field}' must be a string")

        # Track primary-store key
        if "primary-store" in node and isinstance(node["primary-store"], str):
            ps_key = node["primary-store"]
            if ps_key not in primary_store_map:
                primary_store_map[ps_key] = []
            primary_store_map[ps_key].append(path)

        return [(path, node)]

    # This node is a group
    # A group must not define a string-valued 'type' field for itself
    if "type" in node and isinstance(node["type"], str):
        display_path = path if path else "(root)"
        errors.append(f"group '{display_path}': group must not define a 'type' field")
        return []

    # A group must not contain source-annotation keys with non-object values
    for key in _SOURCE_FIELDS:
        if key in node:
            val = node[key]
            if not isinstance(val, dict):
                display_path = path if path else "(root)"
                errors.append(
                    f"group '{display_path}': source annotation '{key}' not allowed in group"
                )
                return []

    # Every entry inside a group must be object-valued
    parameters = []
    for name, child in node.items():
        child_path = f"{path}.{name}" if path else name

        # Skip source annotation keys that are objects (sub-groups or params)
        if name in _SOURCE_FIELDS and isinstance(child, dict):
            # If it looks like it could be a source annotation that's an object,
            # we need to check if it's actually a parameter or group
            pass

        if not isinstance(child, dict):
            errors.append(
                f"entry '{child_path}' must be an object"
            )
            return []

        parameters.extend(
            _validate_schema(child, child_path, primary_store_map, errors)
        )

    return parameters


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Build nested output
# ---------------------------------------------------------------------------

def _set_nested(output, composed_path, value):
    """Set a value in a nested dict using a dotted path."""
    parts = composed_path.split(".")
    node = output
    for part in parts[:-1]:
        if part not in node:
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_global_flags(args):
    """Parse global flags from command line arguments."""
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

    # Root must be a non-empty JSON object
    if not isinstance(schema, dict):
        print("error: schema root must be a JSON object", file=sys.stderr)
        sys.exit(1)

    if not schema:
        print("error: schema root must be a non-empty JSON object", file=sys.stderr)
        sys.exit(1)

    # Validate schema and collect parameter declarations
    primary_store_map = {}
    errors = []
    parameters = _validate_schema(schema, "", primary_store_map, errors)

    if errors:
        print(f"error: {errors[0]}", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate primary-store keys
    for ps_key, paths in primary_store_map.items():
        if len(paths) > 1:
            param_list = ", ".join(f"'{p}'" for p in paths)
            print(
                f"error: duplicate primary-store key '{ps_key}' "
                f"declared by parameters {param_list}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Check primary-store configuration requirement
    has_primary_store_param = bool(primary_store_map)
    if has_primary_store_param and not primary_store_url:
        first_param = next(
            path for path, decl in parameters if "primary-store" in decl
        )
        print(
            f"error: parameter '{first_param}' declares primary-store but --primary-store is not configured",
            file=sys.stderr,
        )
        sys.exit(1)

    arg_map = build_arg_map(arg_candidates)

    # Resolve all parameters
    resolved = {}
    unresolved = []

    for composed_path, declaration in parameters:
        value, error = resolve_parameter(
            composed_path, declaration, arg_map, primary_store_url
        )

        if error:
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)

        if value is None:
            unresolved.append(composed_path)
        else:
            resolved[composed_path] = value

    if unresolved:
        names = ", ".join(sorted(unresolved))
        print(f"error: unresolved parameters: {names}", file=sys.stderr)
        sys.exit(1)

    # Build nested output structure
    output = {}
    for composed_path, value in resolved.items():
        _set_nested(output, composed_path, value)

    print(json.dumps(output))


if __name__ == "__main__":
    main()
