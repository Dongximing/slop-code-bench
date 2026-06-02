#!/usr/bin/env python3
"""cfgpipe - Core Configuration Resolver

Reads a JSON schema, resolves parameters from local sources
(default, env, file, arg), and writes resolved configuration to stdout as JSON.
"""

import json
import os
import sys

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

    return None


def _read_file_source(path):
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            return f.read()
    except (OSError, IOError):
        return None


def resolve_parameter(name, declaration, arg_map):
    """Resolve a single parameter from its sources.

    Returns (value_string, None) on success.
    Returns (None, error_message) on parse failure.
    Returns (None, None) if no source provides a value.
    """
    param_type = declaration["type"]

    # Build ordered candidate list: (source_name, raw_value_or_None)
    candidates = [
        ("arg", arg_map.get(declaration["arg"]) if "arg" in declaration else None),
        ("file", _read_file_source(declaration["file"]) if "file" in declaration else None),
        ("env", os.environ.get(declaration["env"]) if "env" in declaration else None),
        ("default", declaration.get("default")),
    ]

    for source_name, raw_value in candidates:
        if raw_value is None:
            continue
        trimmed = raw_value.strip()
        if not trimmed:
            continue
        try:
            return (parse_value(trimmed, param_type), None)
        except ValueError as e:
            return (None, f"parameter '{name}': failed to parse from source '{source_name}': {e}")

    return (None, None)


def main():
    args = sys.argv[1:]

    if not args:
        print("error: no schema file specified", file=sys.stderr)
        sys.exit(1)

    schema_path = args[0]
    arg_candidates = args[1:]

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

    arg_map = build_arg_map(arg_candidates)

    resolved = {}
    unresolved = []

    for name, declaration in schema.items():
        value, error = resolve_parameter(name, declaration, arg_map)

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
