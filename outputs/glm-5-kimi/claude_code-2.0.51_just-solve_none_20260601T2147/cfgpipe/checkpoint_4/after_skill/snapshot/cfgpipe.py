#!/usr/bin/env python3
"""cfgpipe - command-line configuration resolver with watch mode."""

import json
import os
import re
import sys
import time
from urllib.parse import urlencode

import requests

VALID_TYPES = {"string", "integer", "float", "boolean", "port"}
SOURCE_ANNOTATIONS = ("default", "env", "file", "arg", "primary-store", "secondary-store")
SOURCE_FIELDS = ("default", "env", "file", "arg")
# Priority: default (lowest), env, file, primary-store, secondary-store, arg (highest)
SOURCE_ORDER = ("arg", "secondary-store", "primary-store", "file", "env", "default")


def fail(msg):
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(1)


def _parse_integer(s):
    if "e" in s.lower() or "." in s:
        raise ValueError("not a decimal integer (contains '.' or scientific notation)")
    sign_stripped = s[1:] if s and s[0] in "+-" else s
    if not sign_stripped or not sign_stripped.isdigit():
        raise ValueError("not a decimal integer")
    n = int(s)
    return n, str(n)


def parse_value(raw, type_name):
    if type_name == "string":
        return raw, raw

    s = raw.strip()

    if type_name == "integer":
        return _parse_integer(s)

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
        return f, f"{f:.6f}"

    if type_name == "boolean":
        low = s.lower()
        if low in {"true", "yes", "y", "on", "1", "t"}:
            return True, "true"
        if low in {"false", "no", "n", "off", "0", "f"}:
            return False, "false"
        raise ValueError("not a boolean string representation")

    if type_name == "port":
        if s and s[0] in "+-":
            raise ValueError("port must be in range 0-65535")
        n, _ = _parse_integer(s)
        if n < 0 or n > 65535:
            raise ValueError("port must be in range 0-65535")
        return n, str(n)

    raise ValueError("unrecognized type")


def _json_response(resp, label):
    """Parse JSON from a response, failing on non-200 or malformed body."""
    if resp.status_code != 200:
        fail("error: %s returned unexpected status %d" % (label, resp.status_code))
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: %s returned non-JSON body" % label)
    if not isinstance(body, dict):
        fail("error: %s returned malformed response" % label)
    return body


def _request_get(url, label, params=None):
    try:
        return requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        fail("error: %s request failed: %s" % (label, e))


def _strip_or_none(s):
    """Return stripped string if non-empty after stripping, else None."""
    if not s:
        return None
    return s.strip() or None


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
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    return content.strip() or None


def lookup_primary_store(base_url, key):
    url = base_url.rstrip("/") + "/v1/primary/kv"
    resp = _request_get(url, "primary-store", params={"key": key})

    if resp.status_code == 404:
        body = _json_response_check(resp, "primary-store")
        if body.get("found") is False:
            return None
        fail("error: primary-store returned 404 with unexpected body")

    body = _json_response(resp, "primary-store")
    if body.get("found") is not True:
        fail("error: primary-store returned malformed response")

    value = body["value"]
    if not isinstance(value, str):
        fail("error: primary-store returned non-string value")
    return value


def _json_response_check(resp, label):
    """Parse JSON from a response without status-code checking."""
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: %s returned non-JSON body" % label)


def lookup_secondary_store(base_url, key):
    url = base_url.rstrip("/") + "/v1/secondary/kv"
    resp = _request_get(url, "secondary-store", params={"key": key})
    body = _json_response(resp, "secondary-store")

    found = body.get("found")
    if found is True:
        value = body.get("value")
        if not isinstance(value, str):
            fail("error: secondary-store returned non-string value")
        return True, value
    if found is False:
        return False, None
    fail("error: secondary-store returned malformed response")


def _check_store_key_dup(keys_map, key, path, store_name):
    if key in keys_map:
        fail("error: duplicate %s key '%s' in parameters '%s' and '%s'"
             % (store_name, key, keys_map[key], path))
    keys_map[key] = path


def validate_schema_node(node, path, params_list, ps_keys, ss_keys):
    if isinstance(node, dict) and isinstance(node.get("type"), str):
        type_name = node["type"]
        if type_name not in VALID_TYPES:
            fail("error: parameter '%s' has unrecognized type '%s'" % (path, type_name))

        for field in SOURCE_FIELDS:
            if field in node and not isinstance(node[field], str):
                fail("error: parameter '%s' field '%s' must be a string" % (path, field))

        if "primary-store" in node:
            if not isinstance(node["primary-store"], str):
                fail("error: parameter '%s' field 'primary-store' must be a string" % path)
            _check_store_key_dup(ps_keys, node["primary-store"], path, "primary-store")

        if "secondary-store" in node:
            if not isinstance(node["secondary-store"], str):
                fail("error: parameter '%s' field 'secondary-store' must be a string" % path)
            _check_store_key_dup(ss_keys, node["secondary-store"], path, "secondary-store")

        params_list.append((path, node))
    else:
        for ann in SOURCE_ANNOTATIONS:
            if ann in node and not isinstance(node[ann], dict):
                fail("error: group '%s' contains source annotation '%s' with non-object value" % (path, ann))

        for key, value in node.items():
            if key in SOURCE_ANNOTATIONS and isinstance(node[key], dict):
                continue
            if not isinstance(value, dict):
                fail("error: group '%s' entry '%s' must be an object" % (path, key))
            child_path = path + "." + key if path else key
            validate_schema_node(value, child_path, params_list, ps_keys, ss_keys)


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

    if not isinstance(data, dict) or not data:
        fail("error: schema root must be a non-empty object")

    params_list, ps_keys, ss_keys = [], {}, {}
    validate_schema_node(data, "", params_list, ps_keys, ss_keys)
    return data, params_list, ps_keys, ss_keys


def _fetch_source(source, decl, arg_candidates, primary_store_url, secondary_store_url, composed_path):
    """Fetch raw value for a single source. Returns string or None."""
    if source in ("primary-store", "secondary-store"):
        if source not in decl:
            return None
        url = primary_store_url if source == "primary-store" else secondary_store_url
        if url is None:
            fail("error: parameter '%s' declares %s but --%s is not configured"
                 % (composed_path, source, source))
        if source == "primary-store":
            return lookup_primary_store(url, decl["primary-store"])
        found, raw = lookup_secondary_store(url, decl["secondary-store"])
        return raw if found else None

    if source not in decl:
        return None

    if source == "arg":
        return _strip_or_none(get_arg_value(decl[source], arg_candidates))
    if source == "file":
        return read_file_source(decl[source])
    if source == "env":
        return _strip_or_none(os.environ.get(decl[source]))
    # source == "default"
    return _strip_or_none(decl[source])


def resolve_parameter(composed_path, decl, arg_candidates, primary_store_url, secondary_store_url):
    type_name = decl["type"]
    for source in SOURCE_ORDER:
        raw = _fetch_source(source, decl, arg_candidates, primary_store_url, secondary_store_url, composed_path)
        if raw is None:
            continue
        try:
            _, formatted = parse_value(raw, type_name)
        except ValueError as e:
            fail("error: parameter '%s' could not be parsed from source '%s': %s" % (composed_path, source, e))
        return formatted, source
    return None, None


def build_output(params_list, resolved_values):
    result = {}
    for composed_path, _ in params_list:
        value = resolved_values[composed_path]
        parts = composed_path.split(".")
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


def _emit_json(obj):
    json.dump(obj, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_change_event(path, type_name, previous, current):
    _emit_json({"path": path, "type": type_name, "previous": previous, "current": current})


def parse_duration(s):
    if not s:
        return None
    match = re.match(r'^(\d+(?:\.\d+)?)(ms|s|m)?$', s.strip())
    if not match:
        fail("error: invalid duration format: %s" % s)
    value = float(match.group(1))
    unit = match.group(2) or "s"
    if unit == "ms":
        return value / 1000.0
    if unit == "m":
        return value * 60
    return value


def primary_store_watch(base_url, keys, cursor):
    url = base_url.rstrip("/") + "/v1/primary/watch"
    params = [("cursor", cursor)] + [("key", k) for k in keys]
    resp = _request_get(url, "primary-store watch", params=params)
    body = _json_response(resp, "primary-store watch")

    new_cursor = body.get("cursor")
    if not isinstance(new_cursor, int):
        fail("error: primary-store watch returned missing or invalid cursor")

    events = []
    for evt in body.get("events", []):
        if not isinstance(evt, dict):
            fail("error: primary-store watch returned malformed event")
        key, value, version = evt.get("key"), evt.get("value"), evt.get("version")
        if not isinstance(key, str) or not isinstance(value, str) or not isinstance(version, int):
            fail("error: primary-store watch returned malformed event")
        events.append((key, value, version))
    return new_cursor, events


def secondary_store_batch_read(base_url, keys):
    url = base_url.rstrip("/") + "/v1/secondary/batch-read"
    try:
        resp = requests.post(url, json={"keys": keys}, timeout=30)
    except requests.RequestException as e:
        fail("error: secondary-store batch-read request failed: %s" % e)
    body = _json_response(resp, "secondary-store batch-read")

    items = body.get("items", [])
    if not isinstance(items, list):
        fail("error: secondary-store batch-read returned malformed response")

    result = {}
    for item in items:
        if not isinstance(item, dict):
            fail("error: secondary-store batch-read returned malformed item")
        key, status = item.get("key"), item.get("status")
        if not isinstance(key, str) or not isinstance(status, str):
            fail("error: secondary-store batch-read returned malformed item")
        if status == "ok":
            value = item.get("value")
            if not isinstance(value, str):
                fail("error: secondary-store batch-read returned malformed item")
            result[key] = ("ok", value, None)
        elif status == "missing":
            result[key] = ("missing", None, None)
        elif status == "error":
            result[key] = ("error", None, item.get("error"))
        else:
            fail("error: secondary-store batch-read returned unknown status: %s" % status)
    return result


def parse_global_flags(argv):
    result = {
        "primary_store_url": None,
        "secondary_store_url": None,
        "secondary_store_poll_interval": None,
        "watch": False,
        "remaining": []
    }

    i = 0
    while i < len(argv):
        if argv[i] == "--primary-store":
            if i + 1 >= len(argv):
                fail("error: --primary-store requires a value")
            result["primary_store_url"] = argv[i + 1]
            i += 2
        elif argv[i] == "--secondary-store":
            if i + 1 >= len(argv):
                fail("error: --secondary-store requires a value")
            result["secondary_store_url"] = argv[i + 1]
            i += 2
        elif argv[i] == "--secondary-store-poll-interval":
            if i + 1 >= len(argv):
                fail("error: --secondary-store-poll-interval requires a value")
            result["secondary_store_poll_interval"] = parse_duration(argv[i + 1])
            i += 2
        elif argv[i] == "--watch":
            result["watch"] = True
            i += 1
        else:
            result["remaining"].append(argv[i])
            i += 1

    return result


def resolve_all(params_list, arg_candidates, primary_store_url, secondary_store_url):
    """Resolve all parameters, returning (values, seed_events, ps_monitored, ss_monitored)."""
    resolved_values = {}
    seed_events = []
    ps_monitored = {}
    ss_monitored = {}

    for composed_path, decl in params_list:
        value, source = resolve_parameter(
            composed_path, decl, arg_candidates, primary_store_url, secondary_store_url
        )
        if value is None:
            fail("error: unresolved parameter: %s" % composed_path)

        resolved_values[composed_path] = value
        seed_events.append((composed_path, decl["type"], value))

        if "primary-store" in decl:
            ps_monitored[decl["primary-store"]] = (composed_path, decl)
        if "secondary-store" in decl:
            ss_monitored[decl["secondary-store"]] = (composed_path, decl)

    return resolved_values, seed_events, ps_monitored, ss_monitored


def _monitor_primary(primary_store_url, ps_monitored, ps_cursor, ps_state, ps_initialized):
    """Poll primary-store watch endpoint. Returns updated cursor."""
    new_cursor, events = primary_store_watch(
        primary_store_url, list(ps_monitored.keys()), ps_cursor
    )

    for key, value, version in events:
        if key not in ps_monitored:
            continue
        path, decl = ps_monitored[key]
        type_name = decl["type"]

        current_value, current_version = ps_state.get(path, ("", -1))
        if path in ps_initialized and version <= current_version:
            continue

        try:
            _, formatted = parse_value(value, type_name)
        except ValueError:
            fail("error: parameter '%s' could not be parsed from source 'primary-store': parse error" % path)

        if path not in ps_initialized:
            ps_initialized.add(path)

        if formatted != current_value:
            emit_change_event(path, type_name, current_value, formatted)

        ps_state[path] = (formatted, version)

    return new_cursor


def _monitor_secondary(secondary_store_url, ss_monitored, ss_state, ss_initialized):
    """Poll secondary-store batch-read endpoint."""
    ss_keys_to_path = {k: v[0] for k, v in ss_monitored.items()}
    ss_path_to_decl = {v[0]: v[1] for v in ss_monitored.values()}

    results = secondary_store_batch_read(secondary_store_url, list(ss_monitored.keys()))

    for key, (status, value, _error) in results.items():
        if key not in ss_keys_to_path:
            continue
        path = ss_keys_to_path[key]
        decl = ss_path_to_decl[path]
        type_name = decl["type"]

        if status != "ok":
            continue
        try:
            _, formatted = parse_value(value, type_name)
        except ValueError:
            continue

        current = ss_state.get(path, "")
        if path not in ss_initialized:
            ss_initialized.add(path)
            emit_change_event(path, type_name, "", formatted)
            ss_state[path] = formatted
        elif formatted != current:
            emit_change_event(path, type_name, current, formatted)
            ss_state[path] = formatted


def run_watch_mode(primary_store_url, secondary_store_url, poll_interval,
                   ps_monitored, ss_monitored, resolved_values):
    ps_cursor = 0
    ps_state = {}
    ps_initialized = set()

    ss_state = {}
    ss_initialized = set()

    for _key, (path, _decl) in ps_monitored.items():
        ps_state[path] = (resolved_values[path], -1)

    for _key, (path, _decl) in ss_monitored.items():
        ss_state[path] = resolved_values[path]

    while True:
        if ps_monitored and primary_store_url:
            try:
                ps_cursor = _monitor_primary(
                    primary_store_url, ps_monitored, ps_cursor, ps_state, ps_initialized
                )
            except SystemExit:
                raise
            except Exception as e:
                fail("error: primary-store monitoring failed: %s" % e)

        if ss_monitored and secondary_store_url and poll_interval:
            try:
                _monitor_secondary(secondary_store_url, ss_monitored, ss_state, ss_initialized)
            except SystemExit:
                raise
            except Exception as e:
                fail("error: secondary-store monitoring failed: %s" % e)

        if ss_monitored and secondary_store_url and poll_interval:
            time.sleep(poll_interval)
        elif ps_monitored and primary_store_url:
            time.sleep(0.1)
        else:
            break


def main():
    argv = sys.argv[1:]
    if not argv:
        fail("error: usage: cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]")

    flags = parse_global_flags(argv)

    if not flags["remaining"]:
        fail("error: usage: cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]")

    schema_path = flags["remaining"][0]
    arg_candidates = flags["remaining"][1:]

    schema, params_list, ps_keys, ss_keys = load_schema(schema_path)

    primary_store_url = flags["primary_store_url"]
    secondary_store_url = flags["secondary_store_url"]
    watch_mode = flags["watch"]
    poll_interval = flags["secondary_store_poll_interval"]

    if watch_mode and secondary_store_url:
        if poll_interval is None:
            fail("error: --secondary-store-poll-interval is required when --watch and --secondary-store are both present")
        if poll_interval <= 0:
            fail("error: --secondary-store-poll-interval must be strictly positive")
        if not ss_keys:
            fail("error: --watch and --secondary-store are present but no parameters declare secondary-store")

    if not secondary_store_url and ss_keys:
        fail("error: parameter '%s' declares secondary-store but --secondary-store is not configured"
             % list(ss_keys.values())[0])

    resolved_values, seed_events, ps_monitored, ss_monitored = resolve_all(
        params_list, arg_candidates, primary_store_url, secondary_store_url
    )

    if watch_mode:
        for path, type_name, current in seed_events:
            emit_change_event(path, type_name, "", current)

        _emit_json(build_output(params_list, resolved_values))

        has_monitors = ((ps_monitored and primary_store_url)
                        or (ss_monitored and secondary_store_url and poll_interval and poll_interval > 0))
        if not has_monitors:
            sys.exit(0)

        run_watch_mode(
            primary_store_url, secondary_store_url, poll_interval,
            ps_monitored, ss_monitored, resolved_values
        )
    else:
        json.dump(build_output(params_list, resolved_values), sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
