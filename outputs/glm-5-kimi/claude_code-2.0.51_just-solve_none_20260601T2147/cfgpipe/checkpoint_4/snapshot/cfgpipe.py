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
    """Print error to stderr and exit with code 1."""
    sys.stderr.write(msg.rstrip("\n") + "\n")
    sys.exit(1)


def _parse_integer(s):
    """Parse a strict decimal integer string, returning (int, str)."""
    if "e" in s.lower() or "." in s:
        raise ValueError("not a decimal integer (contains '.' or scientific notation)")
    sign_stripped = s[1:] if s and s[0] in "+-" else s
    if not sign_stripped or not sign_stripped.isdigit():
        raise ValueError("not a decimal integer")
    n = int(s)
    return n, str(n)


def parse_value(raw, type_name):
    """Parse a raw string value according to type_name.

    Returns (parsed_value, string_representation).
    """
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
        # Format with exactly 6 decimal places
        formatted = f"{f:.6f}"
        return f, formatted

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


def normalize_string(s):
    """Return stripped string if non-empty after stripping, else None."""
    return s.strip() or None if s else None


def get_arg_value(arg_name, arg_candidates):
    """Get argument value from arg_candidates for the given arg_name."""
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
    """Read file source, returning stripped content or None."""
    if not path:
        return None
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return None
    return content.strip() or None


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

    if not isinstance(body, dict) or body.get("found") is not True:
        fail("error: primary-store returned malformed response")

    value = body["value"]
    if not isinstance(value, str):
        fail("error: primary-store returned non-string value")

    return value


def lookup_secondary_store(base_url, key):
    """Look up a key in the secondary store.

    Returns (found, value) tuple where found is bool and value is string if found.
    Raises SystemExit on connector failures or malformed responses.
    """
    url = base_url.rstrip("/") + "/v1/secondary/kv?" + urlencode({"key": key})
    try:
        resp = requests.get(url, timeout=30)
    except requests.RequestException as e:
        fail("error: secondary-store request failed: %s" % e)

    if resp.status_code != 200:
        fail("error: secondary-store returned unexpected status %d" % resp.status_code)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: secondary-store returned non-JSON body")

    if not isinstance(body, dict):
        fail("error: secondary-store returned malformed response")

    found = body.get("found")
    if found is True:
        value = body.get("value")
        if not isinstance(value, str):
            fail("error: secondary-store returned non-string value")
        return True, value
    elif found is False:
        return False, None
    else:
        fail("error: secondary-store returned malformed response")


def validate_schema_node(node, path, params_list, ps_keys, ss_keys):
    """Recursively validate schema node (group or parameter declaration).

    Collects parameter declarations into params_list and tracks
    primary-store and secondary-store keys for duplicate detection.
    """
    if isinstance(node, dict) and isinstance(node.get("type"), str):
        type_name = node["type"]
        if type_name not in VALID_TYPES:
            fail("error: parameter '%s' has unrecognized type '%s'" % (path, type_name))

        for field in SOURCE_FIELDS:
            if field in node and not isinstance(node[field], str):
                fail("error: parameter '%s' field '%s' must be a string" % (path, field))

        if "primary-store" in node and not isinstance(node["primary-store"], str):
            fail("error: parameter '%s' field 'primary-store' must be a string" % path)

        if "secondary-store" in node and not isinstance(node["secondary-store"], str):
            fail("error: parameter '%s' field 'secondary-store' must be a string" % path)

        params_list.append((path, node))

        if "primary-store" in node:
            key = node["primary-store"]
            if key in ps_keys:
                fail(
                    "error: duplicate primary-store key '%s' in parameters '%s' and '%s'"
                    % (key, ps_keys[key], path)
                )
            ps_keys[key] = path

        if "secondary-store" in node:
            key = node["secondary-store"]
            if key in ss_keys:
                fail(
                    "error: duplicate secondary-store key '%s' in parameters '%s' and '%s'"
                    % (key, ss_keys[key], path)
                )
            ss_keys[key] = path
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
    """Load and validate schema file."""
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

    params_list = []
    ps_keys = {}
    ss_keys = {}
    validate_schema_node(data, "", params_list, ps_keys, ss_keys)

    return data, params_list, ps_keys, ss_keys


def resolve_parameter(composed_path, decl, arg_candidates, primary_store_url, secondary_store_url):
    """Resolve a single parameter value.

    Returns (formatted_value, source_name) tuple or (None, None) if unresolved.
    For primary-store, also returns version info when applicable.
    """
    type_name = decl["type"]

    for source in SOURCE_ORDER:
        raw = None
        version = None

        if source == "primary-store":
            if "primary-store" not in decl:
                continue
            if primary_store_url is None:
                fail("error: parameter '%s' declares primary-store but --primary-store is not configured" % composed_path)
            raw = lookup_primary_store(primary_store_url, decl["primary-store"])
        elif source == "secondary-store":
            if "secondary-store" not in decl:
                continue
            if secondary_store_url is None:
                fail("error: parameter '%s' declares secondary-store but --secondary-store is not configured" % composed_path)
            found, raw = lookup_secondary_store(secondary_store_url, decl["secondary-store"])
            if not found:
                raw = None
        elif source not in decl:
            continue
        elif source == "arg":
            raw = normalize_string(get_arg_value(decl[source], arg_candidates))
        elif source == "file":
            raw = read_file_source(decl[source])
        elif source == "env":
            raw = normalize_string(os.environ.get(decl[source]))
        else:
            raw = normalize_string(decl[source])

        if raw is None:
            continue

        try:
            _, formatted = parse_value(raw, type_name)
        except ValueError as e:
            fail("error: parameter '%s' could not be parsed from source '%s': %s" % (composed_path, source, e))

        return formatted, source

    return None, None


def build_output(params_list, resolved_values):
    """Build nested output structure from resolved values."""
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


def emit_change_event(path, type_name, previous, current):
    """Emit a change event as a single-line JSON object."""
    event = {
        "path": path,
        "type": type_name,
        "previous": previous,
        "current": current
    }
    json.dump(event, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_config(config):
    """Emit the full resolved configuration as a single-line JSON."""
    json.dump(config, sys.stdout)
    sys.stdout.write("\n")
    sys.stdout.flush()


def parse_duration(s):
    """Parse a duration string like '2s', '100ms', '1m' into seconds (float)."""
    if not s:
        return None

    match = re.match(r'^(\d+(?:\.\d+)?)(ms|s|m)?$', s.strip())
    if not match:
        fail("error: invalid duration format: %s" % s)

    value = float(match.group(1))
    unit = match.group(2) or "s"

    if unit == "ms":
        return value / 1000.0
    elif unit == "s":
        return value
    elif unit == "m":
        return value * 60
    else:
        return value


def primary_store_watch(base_url, keys, cursor):
    """Watch primary store for changes.

    Returns (new_cursor, events) where events is a list of (key, value, version).
    """
    url = base_url.rstrip("/") + "/v1/primary/watch"
    params = [("cursor", cursor)]
    for key in keys:
        params.append(("key", key))

    try:
        resp = requests.get(url + "?" + urlencode(params), timeout=30)
    except requests.RequestException as e:
        fail("error: primary-store watch request failed: %s" % e)

    if resp.status_code != 200:
        fail("error: primary-store watch returned unexpected status %d" % resp.status_code)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: primary-store watch returned non-JSON body")

    if not isinstance(body, dict):
        fail("error: primary-store watch returned malformed response")

    new_cursor = body.get("cursor")
    if not isinstance(new_cursor, int):
        fail("error: primary-store watch returned missing or invalid cursor")

    events = []
    for evt in body.get("events", []):
        if not isinstance(evt, dict):
            fail("error: primary-store watch returned malformed event")
        key = evt.get("key")
        value = evt.get("value")
        version = evt.get("version")
        if not isinstance(key, str) or not isinstance(value, str) or not isinstance(version, int):
            fail("error: primary-store watch returned malformed event")
        events.append((key, value, version))

    return new_cursor, events


def secondary_store_batch_read(base_url, keys):
    """Batch read from secondary store.

    Returns dict mapping key to (status, value, error) where:
    - status is one of 'ok', 'missing', 'error'
    - value is present only for 'ok'
    - error is present only for 'error'
    """
    url = base_url.rstrip("/") + "/v1/secondary/batch-read"
    try:
        resp = requests.post(url, json={"keys": keys}, timeout=30)
    except requests.RequestException as e:
        fail("error: secondary-store batch-read request failed: %s" % e)

    if resp.status_code != 200:
        fail("error: secondary-store batch-read returned unexpected status %d" % resp.status_code)

    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        fail("error: secondary-store batch-read returned non-JSON body")

    if not isinstance(body, dict):
        fail("error: secondary-store batch-read returned malformed response")

    items = body.get("items", [])
    if not isinstance(items, list):
        fail("error: secondary-store batch-read returned malformed response")

    result = {}
    for item in items:
        if not isinstance(item, dict):
            fail("error: secondary-store batch-read returned malformed item")
        key = item.get("key")
        status = item.get("status")
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
            error = item.get("error")
            result[key] = ("error", None, error)
        else:
            fail("error: secondary-store batch-read returned unknown status: %s" % status)

    return result


def parse_global_flags(argv):
    """Parse global flags from argv.

    Returns dict with:
    - primary_store_url
    - secondary_store_url
    - secondary_store_poll_interval
    - watch
    - remaining (remaining argv)
    """
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


def resolve_with_events(params_list, arg_candidates, primary_store_url, secondary_store_url):
    """Resolve all parameters and collect event data.

    Returns (resolved_values, seed_events, ps_monitored, ss_monitored, ps_versions, ss_baselines).

    - resolved_values: dict of path -> formatted value
    - seed_events: list of (path, type_name, current_value) for seed-time events
    - ps_monitored: dict of primary-store key -> (path, decl) for parameters to monitor
    - ss_monitored: dict of secondary-store key -> (path, decl) for parameters to monitor
    - ps_versions: dict of path -> version for primary-store tracked values
    - ss_baselines: dict of path -> string representation for secondary-store tracked values
    """
    resolved_values = {}
    seed_events = []
    ps_monitored = {}
    ss_monitored = {}
    ps_versions = {}

    for composed_path, decl in params_list:
        value, source = resolve_parameter(
            composed_path, decl, arg_candidates, primary_store_url, secondary_store_url
        )

        if value is None:
            fail("error: unresolved parameter: %s" % composed_path)

        resolved_values[composed_path] = value
        seed_events.append((composed_path, decl["type"], value))

        # Track primary-store parameters for monitoring
        if "primary-store" in decl:
            ps_monitored[decl["primary-store"]] = (composed_path, decl)
            # Initialize version tracking (we don't know the version from initial lookup,
            # so we'll set it to 0 and update on first watch)

        # Track secondary-store parameters for monitoring
        if "secondary-store" in decl:
            ss_monitored[decl["secondary-store"]] = (composed_path, decl)

    return resolved_values, seed_events, ps_monitored, ss_monitored, ps_versions


def run_watch_mode(
    params_list,
    arg_candidates,
    primary_store_url,
    secondary_store_url,
    secondary_store_poll_interval,
    ps_monitored,
    ss_monitored,
    resolved_values,
    ps_versions
):
    """Run watch mode with monitoring."""
    # Initialize primary-store state
    ps_cursor = 0
    # path -> (string_value, version). Version initialized to -1 since seed
    # lookup doesn't return version info; first watch response establishes
    # the baseline version.
    ps_state = {}
    # Track which paths have had their first successful observation from watch
    ps_initialized = set()

    # Initialize secondary-store state
    # path -> string_value (last successfully observed)
    ss_state = {}
    # Track which paths have had their first successful batch-read observation
    ss_initialized = set()
    ss_keys_to_path = {k: v[0] for k, v in ss_monitored.items()}  # key -> path
    ss_path_to_decl = {v[0]: v[1] for v in ss_monitored.values()}  # path -> decl

    # Set initial state from resolved values (seed-time values)
    for key, (path, decl) in ps_monitored.items():
        ps_state[path] = (resolved_values[path], -1)

    for key, (path, decl) in ss_monitored.items():
        ss_state[path] = resolved_values[path]

    # Start monitoring loop
    while True:
        # Primary-store monitoring
        if ps_monitored and primary_store_url:
            try:
                new_cursor, events = primary_store_watch(
                    primary_store_url,
                    list(ps_monitored.keys()),
                    ps_cursor
                )
                ps_cursor = new_cursor

                for key, value, version in events:
                    if key not in ps_monitored:
                        continue
                    path, decl = ps_monitored[key]
                    type_name = decl["type"]

                    current_value, current_version = ps_state.get(path, ("", -1))
                    if path in ps_initialized and version <= current_version:
                        # Stale or duplicate update, discard silently
                        continue

                    # Parse the new value
                    try:
                        _, formatted = parse_value(value, type_name)
                    except ValueError:
                        # Parse failure during monitoring is fatal for primary-store
                        fail("error: parameter '%s' could not be parsed from source 'primary-store': parse error" % path)

                    # First observation from watch establishes baseline;
                    # only emit event if value changed from seed-time value
                    if path not in ps_initialized:
                        ps_initialized.add(path)
                        if formatted != current_value:
                            emit_change_event(path, type_name, current_value, formatted)
                    elif formatted != current_value:
                        emit_change_event(path, type_name, current_value, formatted)

                    ps_state[path] = (formatted, version)
            except SystemExit:
                raise
            except Exception as e:
                fail("error: primary-store monitoring failed: %s" % e)

        # Secondary-store monitoring
        if ss_monitored and secondary_store_url and secondary_store_poll_interval:
            try:
                results = secondary_store_batch_read(
                    secondary_store_url,
                    list(ss_monitored.keys())
                )

                for key, (status, value, error) in results.items():
                    if key not in ss_keys_to_path:
                        continue
                    path = ss_keys_to_path[key]
                    decl = ss_path_to_decl[path]
                    type_name = decl["type"]

                    if status == "ok":
                        # Try to parse the value
                        try:
                            _, formatted = parse_value(value, type_name)
                        except ValueError:
                            # Parse-failing observations during monitoring are silently skipped
                            continue

                        current = ss_state.get(path, "")
                        if path not in ss_initialized:
                            # First successful observation establishes baseline
                            # The spec allows emitting an event for the initial baseline
                            # Using previous="" to match spec example
                            ss_initialized.add(path)
                            emit_change_event(path, type_name, "", formatted)
                            ss_state[path] = formatted
                        elif formatted != current:
                            previous = current
                            emit_change_event(path, type_name, previous, formatted)
                            ss_state[path] = formatted
                    # 'missing' and 'error' statuses are silently skipped
            except SystemExit:
                raise
            except Exception as e:
                fail("error: secondary-store monitoring failed: %s" % e)

        # Sleep for poll interval if secondary-store monitoring is active
        if ss_monitored and secondary_store_url and secondary_store_poll_interval:
            time.sleep(secondary_store_poll_interval)
        elif ps_monitored and primary_store_url:
            # For primary-store, we add a small sleep to avoid busy-waiting
            # The watch endpoint should be long-polling but we add a small
            # delay to be safe and avoid hammering the server
            time.sleep(0.1)
        else:
            # No monitorable sources, exit after initial output
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

    # Validate watch mode setup requirements
    if watch_mode and secondary_store_url:
        # Poll interval must be strictly positive
        if poll_interval is None:
            fail("error: --secondary-store-poll-interval is required when --watch and --secondary-store are both present")
        if poll_interval <= 0:
            fail("error: --secondary-store-poll-interval must be strictly positive")

        # At least one declared secondary-store key must exist
        if not ss_keys:
            fail("error: --watch and --secondary-store are present but no parameters declare secondary-store")

    # Validate secondary-store configuration
    if not secondary_store_url and ss_keys:
        fail("error: parameter '%s' declares secondary-store but --secondary-store is not configured" % list(ss_keys.values())[0])

    # Resolve all parameters
    resolved_values, seed_events, ps_monitored, ss_monitored, ps_versions = resolve_with_events(
        params_list, arg_candidates, primary_store_url, secondary_store_url
    )

    if watch_mode:
        # Emit seed-time change events
        for path, type_name, current in seed_events:
            emit_change_event(path, type_name, "", current)

        # Emit full resolved configuration
        output = build_output(params_list, resolved_values)
        emit_config(output)

        # Check if there are monitorable sources
        has_monitors = (ps_monitored and primary_store_url) or (ss_monitored and secondary_store_url and poll_interval and poll_interval > 0)

        if not has_monitors:
            # No monitorable sources, exit normally
            sys.exit(0)

        # Run watch mode
        run_watch_mode(
            params_list,
            arg_candidates,
            primary_store_url,
            secondary_store_url,
            poll_interval,
            ps_monitored,
            ss_monitored,
            resolved_values,
            ps_versions
        )
    else:
        # Non-watch mode: just output the resolved configuration
        output = build_output(params_list, resolved_values)
        json.dump(output, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
