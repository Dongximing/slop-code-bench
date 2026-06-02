#!/usr/bin/env python3
"""cfgpipe - Core Configuration Resolver

Reads a JSON schema, resolves parameters from local sources
(default, env, file, arg), and writes resolved configuration to stdout as JSON.
"""

import json
import os
import sys


def parse_boolean(value):
    """Parse boolean string representations.

    Accepts common boolean strings, returns lowercase 'true' or 'false'.
    Raises ValueError on unrecognized input.
    """
    lower = value.lower()
    if lower in ("true", "yes", "on", "1"):
        return "true"
    elif lower in ("false", "no", "off", "0"):
        return "false"
    else:
        raise ValueError(
            f"cannot parse '{value}' as boolean: "
            f"expected one of true/false/yes/no/on/off/1/0"
        )


def parse_integer(value):
    """Parse an integer string.

    Accepts decimal integers only (no scientific notation).
    Raises ValueError on invalid input.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty string cannot be parsed as integer")
    # Check for valid decimal integer format
    if stripped[0] in '+-':
        rest = stripped[1:]
    else:
        rest = stripped
    if not rest or not all(c in '0123456789' for c in rest):
        raise ValueError(f"cannot parse '{value}' as integer")
    int(stripped)  # Validate it's actually parseable
    return stripped


def parse_float(value):
    """Parse a float string.

    Accepts decimal inputs (scientific notation not required).
    Returns string representation.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("empty string cannot be parsed as float")
    result = float(stripped)  # Validate it's actually parseable
    return str(result)


def parse_value(raw_value, param_type):
    """Parse a raw string value according to the declared type.

    Returns the formatted string representation.
    Raises ValueError if parsing fails.
    """
    if param_type == "string":
        return raw_value
    elif param_type == "integer":
        return parse_integer(raw_value)
    elif param_type == "float":
        return parse_float(raw_value)
    elif param_type == "boolean":
        return parse_boolean(raw_value)
    else:
        raise ValueError(f"unknown type '{param_type}'")


def build_arg_map(arg_candidates):
    """Build a mapping from arg names to values from CLI candidates.

    Supports --name=value and -name=value formats. Last wins.
    """
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
    """Validate the schema structure.

    Returns None on success, or an error message string on failure.
    """
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
            if field in declaration:
                if not isinstance(declaration[field], str):
                    return (
                        f"parameter '{name}' field '{field}' must be a string"
                    )

    return None


def resolve_parameter(name, declaration, arg_map):
    """Resolve a single parameter from its sources.

    Returns (value_string, None) on success.
    Returns (None, error_message) on parse failure.
    Returns (None, None) if no source provides a value.
    """
    # Priority order (highest first): arg, file, env, default
    sources = [
        ("arg", lambda d: arg_map.get(d["arg"]) if "arg" in d else None),
        (
            "file",
            lambda d: _read_file_source(d["file"]) if "file" in d else None,
        ),
        ("env", lambda d: os.environ.get(d["env"]) if "env" in d else None),
        ("default", lambda d: d.get("default")),
    ]

    param_type = declaration["type"]

    for source_name, getter in sources:
        raw_value = getter(declaration)
        if raw_value is None:
            continue

        # For file source, None means absent (missing/unreadable/dir/empty)
        if source_name == "file" and raw_value is None:
            continue

        # Trim the raw value for emptiness check
        trimmed = raw_value.strip() if isinstance(raw_value, str) else raw_value

        # Empty after trimming counts as absent
        if not trimmed:
            continue

        # Try to parse the trimmed value
        try:
            parsed = parse_value(trimmed, param_type)
            return (parsed, None)
        except ValueError as e:
            return (
                None,
                f"parameter '{name}': failed to parse from source "
                f"'{source_name}': {e}",
            )

    return (None, None)


def _read_file_source(path):
    """Read a file source. Returns content string or None if absent."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r") as f:
            content = f.read()
        return content
    except (OSError, IOError):
        return None


def main():
    args = sys.argv[1:]

    if len(args) < 1:
        print("error: no schema file specified", file=sys.stderr)
        sys.exit(1)

    schema_path = args[0]
    arg_candidates = args[1:]

    # Load schema
    try:
        with open(schema_path, "r") as f:
            schema = json.load(f)
    except FileNotFoundError:
        print(f"error: schema file '{schema_path}' not found", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(
            f"error: invalid JSON in schema file '{schema_path}': {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate schema
    validation_error = validate_schema(schema)
    if validation_error:
        print(f"error: {validation_error}", file=sys.stderr)
        sys.exit(1)

    # Build arg map from CLI candidates
    arg_map = build_arg_map(arg_candidates)

    # Resolve all parameters
    resolved = {}
    unresolved = []

    for name, declaration in schema.items():
        value, error = resolve_parameter(name, declaration, arg_map)

        if error:
            # Parse failure halts immediately
            print(f"error: {error}", file=sys.stderr)
            sys.exit(1)

        if value is None:
            unresolved.append(name)
        else:
            resolved[name] = value

    # Check for unresolved parameters
    if unresolved:
        names = ", ".join(sorted(unresolved))
        print(
            f"error: unresolved parameters: {names}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Success - write JSON to stdout
    print(json.dumps(resolved))
    sys.exit(0)


if __name__ == "__main__":
    main()
