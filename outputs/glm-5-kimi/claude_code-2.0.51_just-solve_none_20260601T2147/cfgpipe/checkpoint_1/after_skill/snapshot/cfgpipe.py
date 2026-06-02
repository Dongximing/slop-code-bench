#!/usr/bin/env python3
"""cfgpipe - command-line configuration resolver."""

import json
import os
import sys

VALID_TYPES = {"string", "integer", "float", "boolean"}
SOURCE_FIELDS = ("default", "env", "file", "arg")
SOURCE_ORDER = ("arg", "file", "env", "default")


def fail(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(1)


def load_schema(path):
    if not os.path.isfile(path):
        fail("error: schema file not found: " + path)

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
        if not isinstance(decl["type"], str):
            fail("error: parameter '%s' 'type' must be a string" % name)
        if decl["type"] not in VALID_TYPES:
            fail("error: parameter '%s' has unrecognized type '%s'" % (name, decl["type"]))

        for field in SOURCE_FIELDS:
            if field in decl and not isinstance(decl[field], str):
                fail("error: parameter '%s' field '%s' must be a string" % (name, field))

    return data


def parse_value(raw, type_name):
    if type_name == "string":
        return raw, raw

    s = raw.strip()

    if type_name == "integer":
        if "e" in s.lower() or "." in s:
            raise ValueError("not a decimal integer (contains '.' or scientific notation)")
        sign_stripped = s[1:] if s and s[0] in "+-" else s
        if not sign_stripped or not sign_stripped.isdigit():
            raise ValueError("not a decimal integer")
        n = int(s)
        return n, str(n)

    if type_name == "float":
        if "e" in s.lower():
            raise ValueError("scientific notation not accepted for float")
        sign_stripped = s[1:] if s and s[0] in "+-" else s
        if not sign_stripped:
            raise ValueError("empty value")
        if sign_stripped.count(".") > 1:
            raise ValueError("not a decimal float")
        if not any(c.isdigit() for c in sign_stripped):
            raise ValueError("not a decimal float")
        if not all(c.isdigit() or c == "." for c in sign_stripped):
            raise ValueError("not a decimal float")
        try:
            f = float(s)
        except ValueError:
            raise ValueError("not a decimal float")
        return f, str(f)

    if type_name == "boolean":
        low = s.lower()
        if low in {"true", "yes", "y", "on", "1", "t"}:
            return True, "true"
        if low in {"false", "no", "n", "off", "0", "f"}:
            return False, "false"
        raise ValueError("not a boolean string representation")

    raise ValueError("unrecognized type")


def get_arg_value(arg_name, arg_candidates):
    long_prefix = "--" + arg_name + "="
    short_prefix = "-" + arg_name + "="
    found = None
    for cand in arg_candidates:
        if cand.startswith(long_prefix):
            found = cand[len(long_prefix):]
        elif cand.startswith(short_prefix) and not cand.startswith("--"):
            found = cand[len(short_prefix):]
    return found


def read_file_source(path):
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
    return trimmed if trimmed else None


def resolve_parameter(name, decl, arg_candidates):
    type_name = decl["type"]

    for source in SOURCE_ORDER:
        if source not in decl:
            continue
        src_val = decl[source]

        if source == "arg":
            raw = get_arg_value(src_val, arg_candidates)
            if raw is not None and not raw.strip():
                raw = None
        elif source == "file":
            raw = read_file_source(src_val)
        elif source == "env":
            val = os.environ.get(src_val)
            raw = val.strip() if val and val.strip() else None
        else:
            raw = src_val.strip() if src_val and src_val.strip() else None

        if raw is None:
            continue

        try:
            _, formatted = parse_value(raw, type_name)
        except ValueError as e:
            fail("error: parameter '%s' could not be parsed from source '%s': %s" % (name, source, e))

        return formatted

    return None


def main():
    argv = sys.argv[1:]
    if not argv:
        fail("error: usage: cfgpipe.py <schema-file> [arg-candidates...]")

    schema = load_schema(argv[0])
    arg_candidates = argv[1:]

    resolved = {}
    unresolved = []

    for name, decl in schema.items():
        value = resolve_parameter(name, decl, arg_candidates)
        if value is not None:
            resolved[name] = value
        else:
            unresolved.append(name)

    if unresolved:
        fail("error: unresolved parameters: %s" % ", ".join(unresolved))

    json.dump(resolved, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
