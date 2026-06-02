#!/usr/bin/env python3
"""cfgpipe - command-line configuration resolver."""

import json
import os
import sys


VALID_TYPES = {"string", "integer", "float", "boolean"}
SOURCE_FIELDS = ("default", "env", "file", "arg")
# Priority order: highest priority first. arg > file > env > default.
# "The first source that provides a value wins" - we walk highest to lowest.
SOURCE_ORDER = ("arg", "file", "env", "default")


class ParseHalt(Exception):
    """Raised when a parse failure should halt resolution."""

    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


def fail(msg):
    """Print error to stderr and exit non-zero without writing to stdout."""
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(1)


def load_schema(path):
    """Load and structurally validate schema. Returns parsed dict."""
    if not os.path.isfile(path):
        fail("error: schema file not found: " + path)

    # Use object_pairs_hook to detect duplicate keys
    def check_duplicates(pairs):
        seen = set()
        for k, v in pairs:
            if k in seen:
                fail("error: duplicate parameter name: %s" % k)
            seen.add(k)
        return dict(pairs)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=check_duplicates)
    except json.JSONDecodeError as e:
        fail("error: invalid JSON in schema file '%s': %s" % (path, e))
    except OSError as e:
        fail("error: cannot read schema file '%s': %s" % (path, e))

    if not isinstance(data, dict):
        fail("error: schema root must be an object")

    for name, decl in data.items():
        if not isinstance(name, str):
            fail("error: parameter name must be a string")

        if not isinstance(decl, dict):
            fail("error: parameter '%s' declaration must be an object" % name)

        if "type" not in decl:
            fail("error: parameter '%s' missing required 'type' field" % name)
        type_val = decl["type"]
        if not isinstance(type_val, str):
            fail("error: parameter '%s' 'type' must be a string" % name)
        if type_val not in VALID_TYPES:
            fail(
                "error: parameter '%s' has unrecognized type '%s'" % (name, type_val)
            )

        for field in SOURCE_FIELDS:
            if field in decl:
                val = decl[field]
                if not isinstance(val, str):
                    fail(
                        "error: parameter '%s' field '%s' must be a string"
                        % (name, field)
                    )

    return data


def parse_value(raw, type_name):
    """Parse raw string against the declared type.

    Returns (parsed_value, formatted_string) on success.
    Raises ValueError with a reason on failure.
    """
    if type_name == "string":
        return raw, raw

    s = raw.strip()

    if type_name == "integer":
        # Decimal integers only, no scientific notation.
        # Reject leading +, leading zeros allowed for simple match? Spec says
        # "decimal integers" and "plain decimal, no scientific notation".
        # Use int() but verify no scientific notation was used.
        if "e" in s.lower() or "." in s:
            raise ValueError(
                "not a decimal integer (contains '.' or scientific notation)"
            )
        # int() accepts leading +/- and arbitrary whitespace (already stripped).
        # Validate that it's strictly decimal digits with optional sign and
        # no other characters.
        sign_stripped = s[1:] if s and s[0] in "+-" else s
        if not sign_stripped or not sign_stripped.isdigit():
            raise ValueError("not a decimal integer")
        n = int(s)
        return n, str(n)

    if type_name == "float":
        # Decimal inputs; scientific notation not required (i.e. may be rejected).
        if "e" in s.lower():
            raise ValueError("scientific notation not accepted for float")
        if "." not in s:
            # Must have decimal point? Spec says "decimal inputs" - to be safe,
            # accept digit-only input as well, since "decimal" can include
            # whole numbers. Actually let's be strict and require a decimal
            # point OR accept int-like. The spec says "no exact canonical
            # format required" for the output.
            # We'll accept any decimal input that float() can parse, as long
            # as it's not scientific notation.
            pass
        if not s:
            raise ValueError("empty value")
        # Verify it's a valid float literal (digits, optional sign, optional
        # decimal point).
        sign_stripped = s[1:] if s and s[0] in "+-" else s
        # Allow forms like "1.", ".5", "1.5", "1"
        if not all(c.isdigit() or c == "." for c in sign_stripped):
            raise ValueError("not a decimal float")
        if sign_stripped.count(".") > 1:
            raise ValueError("not a decimal float")
        if "." not in sign_stripped and not sign_stripped.isdigit():
            raise ValueError("not a decimal float")
        # Must have at least one digit
        if not any(c.isdigit() for c in sign_stripped):
            raise ValueError("not a decimal float")
        try:
            f = float(s)
        except ValueError:
            raise ValueError("not a decimal float")
        return f, str(f)

    if type_name == "boolean":
        low = s.lower()
        truthy = {"true", "yes", "y", "on", "1", "t"}
        falsy = {"false", "no", "n", "off", "0", "f"}
        if low in truthy:
            return True, "true"
        if low in falsy:
            return False, "false"
        raise ValueError("not a boolean string representation")

    # Should not reach here since schema validation already enforced type.
    raise ValueError("unrecognized type")


def get_arg_value(arg_name, arg_candidates):
    """Find a value for arg_name in arg_candidates. Last wins.

    Supports --name=value and -name=value. Returns None if not found.
    """
    long_prefix = "--" + arg_name + "="
    short_prefix = "-" + arg_name + "="
    found = None
    for cand in arg_candidates:
        if cand.startswith(long_prefix):
            found = cand[len(long_prefix):]
        elif cand.startswith(short_prefix):
            # Don't match --name= with -name= pattern (long form already
            # handled above). But also avoid matching when cand starts with
            # "--" and short prefix is "-".
            # The short prefix only matches if cand does NOT start with "--".
            # Since the long check above handled "--name=", we are safe to
            # check short here only when not "--".
            if not cand.startswith("--"):
                found = cand[len(short_prefix):]
    return found


def read_file_source(path):
    """Read a file source. Returns trimmed string or None if absent.

    Absent means: file missing, path is not a regular file, read error,
    or content is empty after trimming whitespace.
    """
    if not path:
        return None
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    trimmed = content.strip()
    if not trimmed:
        return None
    return trimmed


def get_env_value(env_name):
    """Get env var value. Returns None if unset or empty after trim."""
    if not env_name:
        return None
    val = os.environ.get(env_name)
    if val is None:
        return None
    trimmed = val.strip()
    if not trimmed:
        return None
    return trimmed


def get_default_value(default_str):
    """Get default source value. Returns None if empty/missing."""
    if default_str is None:
        return None
    trimmed = default_str.strip()
    if not trimmed:
        return None
    return trimmed


def resolve_parameter(name, decl, arg_candidates):
    """Resolve a single parameter.

    Returns ("ok", formatted_string) on success.
    Returns ("unresolved", None) if no source provides a value.
    Raises a string error message on parse failure (caller halts).
    """
    type_name = decl["type"]

    for source in SOURCE_ORDER:
        if source not in decl:
            continue
        src_val = decl[source]

        if source == "default":
            raw = get_default_value(src_val)
        elif source == "env":
            raw = get_env_value(src_val)
        elif source == "file":
            raw = read_file_source(src_val)
        elif source == "arg":
            v = get_arg_value(src_val, arg_candidates)
            raw = v  # arg is never "empty after trim" per spec? The spec
            # says env/file treat empty-trimmed as absent. For arg, the spec
            # doesn't explicitly say. We treat absent as "not provided" and
            # any provided value (even empty after trim of `--name=`) as
            # a value. But to be safe with `-name=` (empty value), let's
            # treat empty as absent too, consistent with other sources.
            if raw is not None and not raw.strip():
                raw = None
        else:
            raw = None

        if raw is None:
            continue

        try:
            _parsed, formatted = parse_value(raw, type_name)
        except ValueError as e:
            raise ParseHalt(
                "error: parameter '%s' could not be parsed from source '%s': %s"
                % (name, source, str(e))
            ) from None

        return ("ok", formatted)

    return ("unresolved", None)


def main():
    argv = sys.argv[1:]
    if len(argv) < 1:
        fail("error: usage: cfgpipe.py <schema-file> [arg-candidates...]")

    schema_path = argv[0]
    arg_candidates = argv[1:]

    schema = load_schema(schema_path)

    resolved = {}
    unresolved = []
    try:
        for name, decl in schema.items():
            status, value = resolve_parameter(name, decl, arg_candidates)
            if status == "ok":
                resolved[name] = value
            else:
                unresolved.append(name)
    except ParseHalt as e:
        fail(e.msg)

    if unresolved:
        fail(
            "error: unresolved parameters: %s" % ", ".join(unresolved)
        )

    json.dump(resolved, sys.stdout)
    sys.stdout.write("\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
