#!/usr/bin/env python3
"""cfgpipe - command-line configuration resolver."""

import json
import os
import sys
from urllib.parse import urlencode

import requests

BUILTIN_TYPES = {"string", "integer", "float", "boolean"}
CUSTOM_TYPES = {"port"}
VALID_TYPES = BUILTIN_TYPES | CUSTOM_TYPES
SOURCE_ANNOTATIONS = ("default", "env", "file", "arg", "primary-store")
SOURCE_FIELDS = ("default", "env", "file", "arg")
SOURCE_ORDER = ("arg", "primary-store", "file", "env", "default")


def fail(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(1)


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

    if type_name == "port":
        if "e" in s.lower() or "." in s:
            raise ValueError("not a decimal integer")
        sign_stripped = s[1:] if s and s[0] in "+-" else s
        if not sign_stripped or not sign_stripped.isdigit():
            raise ValueError("not a decimal integer")
        if s and s[0] in "+-":
            raise ValueError("port must be in range 0-65535")
        n = int(s)
        if n < 0 or n > 65535:
            raise ValueError("port must be in range 0-65535")
        return n, str(n)

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


def is_parameter_declaration(obj):
    """Return True if obj looks like a parameter declaration (has a string 'type' field)."""
    return isinstance(obj, dict) and isinstance(obj.get("type"), str)


def validate_schema_node(node, path, params_list, ps_keys):
    """Recursively validate a schema node.

    A node is either:
    - A parameter declaration: has a string-valued 'type' field
    - A group: an object that does NOT have a string-valued 'type' field

    For groups:
    - Must not have source annotation keys with non-object values
    - Every entry must be an object
    - Recurse into each entry

    For parameter declarations:
    - Validate type is recognized
    - Validate source fields are strings
    - Track primary-store keys for duplicate detection

    params_list: list of (composed_path, declaration_dict) for all parameters found
    ps_keys: dict mapping primary-store key -> composed_path for duplicate detection
    """
    if is_parameter_declaration(node):
        # This is a parameter declaration
        type_name = node["type"]
        if type_name not in VALID_TYPES:
            fail("error: parameter '%s' has unrecognized type '%s'" % (path, type_name))

        for field in SOURCE_FIELDS:
            if field in node and not isinstance(node[field], str):
                fail("error: parameter '%s' field '%s' must be a string" % (path, field))

        if "primary-store" in node and not isinstance(node["primary-store"], str):
            fail("error: parameter '%s' field 'primary-store' must be a string" % path)

        params_list.append((path, node))

        if "primary-store" in node:
            key = node["primary-store"]
            if key in ps_keys:
                fail(
                    "error: duplicate primary-store key '%s' in parameters '%s' and '%s'"
                    % (key, ps_keys[key], path)
                )
            ps_keys[key] = path
    else:
        # This is a group
        # Check that group doesn't have a string-valued type field
        # (already handled by is_parameter_declaration)

        # Check that group doesn't have source annotation keys with non-object values
        for ann in SOURCE_ANNOTATIONS:
            if ann in node and not isinstance(node[ann], object):
                fail(
                    "error: group '%s' contains source annotation '%s' with non-object value"
                    % (path, ann)
                )
            # Source annotations in a group context should not be strings
            # A group has source annotation keys only if their value is a non-object (e.g., string)
            if ann in node and not isinstance(node[ann], dict):
                fail(
                    "error: group '%s' contains source annotation '%s' with non-object value"
                    % (path, ann)
                )

        # Every entry in a group must be an object
        for key, value in node.items():
            # Skip annotation keys we allow on groups
            # Actually, re-reading the spec: groups must not contain source-annotation keys
            # with non-object value. So source-annotation keys with object values are fine?
            # Wait, the spec says "must not contain any source-annotation key with a non-object value"
            # So source annotations ARE allowed if their value IS an object.
            # But what would that mean? We don't process those.
            # Let's just skip those keys for the "every entry must be object-valued" check.
            if key in SOURCE_ANNOTATIONS and isinstance(node[key], dict):
                continue

            if not isinstance(value, dict):
                fail(
                    "error: group '%s' entry '%s' must be an object" % (path, key)
                )

            child_path = path + "." + key if path else key
            validate_schema_node(value, child_path, params_list, ps_keys)


def load_schema(path):
    if not os.path.isfile(path):
        fail("error: schema file not found: " + path)

    def check_duplicates(pairs):
        seen = set()
        for k, v in pairs:
            if k in seen:
                fail("error: duplicate key: %s" % k)
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

    if not data:
        fail("error: schema root must be a non-empty object")

    # Check root-level: a group must not have source annotations with non-object values
    # But the root is an implicit group. Check source annotations at root level.
    for ann in SOURCE_ANNOTATIONS:
        if ann in data and not isinstance(data[ann], dict):
            fail(
                "error: group '' contains source annotation '%s' with non-object value"
                % ann
            )

    # Check all entries at root are objects (except dict-valued annotations)
    params_list = []
    ps_keys = {}

    for key, value in data.items():
        # Skip annotation keys with dict values
        if key in SOURCE_ANNOTATIONS and isinstance(value, dict):
            continue

        if not isinstance(value, dict):
            fail("error: group '' entry '%s' must be an object" % key)

        validate_schema_node(value, key, params_list, ps_keys)

    return data, params_list


def resolve_parameter(composed_path, decl, arg_candidates, primary_store_url):
    type_name = decl["type"]

    for source in SOURCE_ORDER:
        if source == "primary-store":
            if "primary-store" not in decl:
                continue
            if primary_store_url is None:
                fail(
                    "error: parameter '%s' declares primary-store but --primary-store is not configured"
                    % composed_path
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
            fail("error: parameter '%s' could not be parsed from source '%s': %s" % (composed_path, source, e))

        return formatted

    return None


def build_output(params_list, resolved_values):
    """Build nested output structure from resolved values.

    params_list: list of (composed_path, decl)
    resolved_values: dict mapping composed_path -> resolved string value
    Returns: nested dict matching the group hierarchy
    """
    result = {}
    for composed_path, _ in params_list:
        value = resolved_values[composed_path]
        parts = composed_path.split(".")
        # Navigate/create nested dicts
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


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

    schema, params_list = load_schema(remaining[0])
    arg_candidates = remaining[1:]

    resolved_values = {}
    unresolved = []

    for composed_path, decl in params_list:
        value = resolve_parameter(composed_path, decl, arg_candidates, primary_store_url)
        if value is not None:
            resolved_values[composed_path] = value
        else:
            unresolved.append(composed_path)

    if unresolved:
        fail("error: unresolved parameters: %s" % ", ".join(unresolved))

    output = build_output(params_list, resolved_values)
    json.dump(output, sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
