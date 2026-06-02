#!/usr/bin/env python3
"""cfgpipe - command-line configuration resolver."""

import json
import os
import sys
from urllib.parse import urlencode

import requests

VALID_TYPES = {"string", "integer", "float", "boolean"}
SOURCE_FIELDS = ("default", "env", "file", "arg")
SOURCE_ORDER = ("arg", "primary-store", "file", "env", "default")


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

        if "primary-store" in decl and not isinstance(decl["primary-store"], str):
            fail("error: parameter '%s' field 'primary-store' must be a string" % name)

    ps_keys = {}
    for name, decl in data.items():
        if "primary-store" in decl:
            key = decl["primary-store"]
            if key in ps_keys:
                fail(
                    "error: duplicate primary-store key '%s' in parameters '%s' and '%s'"
                    % (key, ps_keys[key], name)
                )
            ps_keys[key] = name

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


def normalize_string(s):
    """Return stripped string if non-empty after stripping, else None."""
    if not s:
        return None
    stripped = s.strip()
    return stripped if stripped else None


def get_arg_value(arg_name, arg_candidates):
    long_prefix = "--" + arg_name + "="
    short_prefix = "-" + arg_name + "="
    result = None
    for cand in arg_candidates:
        if cand.startswith(long_prefix):
            result = cand[len(long_prefix):]
        elif cand.startswith(short_prefix) and not cand.startswith("--"):
            result = cand[len(short_prefix):]
    return result


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


def lookup_primary_store(base_url, key):
    """Look up a key in the primary store.

    Returns the string value if found, None if the key is missing.
    Raises SystemExit on connector failures or malformed responses.
    """
    url = base_url.rstrip("/") + "/v1/primary/kv?" + urlencode({"key": key})
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as e:
        fail("error: primary-store request failed: %s" % e)

    if resp.status_code == 404:
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError):
            fail("error: primary-store returned 404 with non-JSON body")
        if body.get("found") is False:
            return None
        fail("error: primary-store returned 404 with unexpected body")

    if resp.status_code != 200:
        fail("error: primary-store returned unexpected status %d" % resp.status_code)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: primary-store returned non-JSON body")

    if not isinstance(body, dict):
        fail("error: primary-store returned malformed response")

    if body.get("found") is not True:
        fail("error: primary-store returned unexpected response body")

    if "value" not in body:
        fail("error: primary-store returned response missing 'value' field")

    value = body["value"]
    if not isinstance(value, str):
        fail("error: primary-store returned non-string value")

    return value


def resolve_parameter(name, decl, arg_candidates, primary_store_url):
    type_name = decl["type"]

    for source in SOURCE_ORDER:
        if source == "primary-store":
            if "primary-store" not in decl:
                continue
            if primary_store_url is None:
                fail(
                    "error: parameter '%s' declares primary-store but --primary-store is not configured"
                    % name
                )
            raw = lookup_primary_store(primary_store_url, decl["primary-store"])
        elif source in decl:
            src_val = decl[source]
            if source == "arg":
                raw = normalize_string(get_arg_value(src_val, arg_candidates))
            elif source == "file":
                raw = read_file_source(src_val)
            elif source == "env":
                raw = normalize_string(os.environ.get(src_val))
            else:
                raw = normalize_string(src_val)
        else:
            continue

        if raw is None:
            continue

        try:
            _, formatted = parse_value(raw, type_name)
        except ValueError as e:
            fail("error: parameter '%s' could not be parsed from source '%s': %s" % (name, source, e))

        return formatted

    return None


def parse_global_flags(argv):
    """Parse global flags from argv, returning (primary_store_url, remaining_argv)."""
    primary_store_url = None
    remaining = []
    i = 0
    while i < len(argv):
        if argv[i] == "--primary-store":
            if i + 1 >= len(argv):
                fail("error: --primary-store requires a value")
            primary_store_url = argv[i + 1]
            i += 2
        else:
            remaining.append(argv[i])
            i += 1
    return primary_store_url, remaining


def main():
    argv = sys.argv[1:]
    if not argv:
        fail("error: usage: cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]")

    primary_store_url, remaining = parse_global_flags(argv)
    if not remaining:
        fail("error: usage: cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]")

    schema = load_schema(remaining[0])
    arg_candidates = remaining[1:]

    resolved = {}
    unresolved = []

    for name, decl in schema.items():
        value = resolve_parameter(name, decl, arg_candidates, primary_store_url)
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
