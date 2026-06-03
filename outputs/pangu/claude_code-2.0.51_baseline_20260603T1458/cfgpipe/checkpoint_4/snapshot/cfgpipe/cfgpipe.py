#!/usr/bin/env python3
"""cfgpipe - Command-line configuration resolver with watch mode and change events.

Reads a JSON schema document from disk, resolves each declared parameter
from configured sources, and emits configuration changes as JSON lines.
Every resolved leaf value is a string.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Iterator
import threading


class ConfigError(Exception):
    """Exception raised for configuration resolution errors."""
    pass


def parse_cli_args(args: list[str]) -> dict[str, str]:
    """Parse CLI arguments in --name=value or -name=value format.

    Last wins for duplicate names. Only arguments after the schema-file path
    participate.
    """
    result: dict[str, str] = {}
    for arg in args:
        if '=' not in arg:
            continue
        name_part, _, value = arg.partition('=')
        # Strip leading dashes
        if name_part.startswith('--'):
            name = name_part[2:]
        elif name_part.startswith('-'):
            name = name_part[1:]
        else:
            continue
        result[name] = value
    return result


def read_schema(schema_path: str) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Read and validate the schema file.

    Returns the parsed schema dict and a map of primary-store keys to parameter paths.

    Raises ConfigError on file missing, invalid JSON, or validation failure.
    """
    try:
        with open(schema_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise ConfigError(f"Schema file not found: {schema_path}")
    except OSError as e:
        raise ConfigError(f"Cannot read schema file {schema_path}: {e}")

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in schema file {schema_path}: {e}")

    if not isinstance(schema, dict):
        raise ConfigError(f"Schema root must be an object, got {type(schema).__name__}")

    # Validate each parameter declaration
    primary_store_keys: dict[str, str] = {}
    secondary_store_keys: dict[str, str] = {}

    for param_name, param_decl in schema.items():
        if not isinstance(param_decl, dict):
            raise ConfigError(
                f"Parameter '{param_name}' must be an object declaration"
            )
        if 'type' not in param_decl:
            raise ConfigError(
                f"Parameter '{param_name}' is missing required 'type' field"
            )

        # Validate source fields are strings if present
        for field in ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'):
            if field in param_decl and not isinstance(param_decl[field], str):
                raise ConfigError(
                    f"Parameter '{param_name}' has non-string '{field}' field"
                )

        # Track primary-store keys for duplicate detection
        if 'primary-store' in param_decl:
            ps_key = param_decl['primary-store']
            if not isinstance(ps_key, str):
                raise ConfigError(
                    f"Parameter '{param_name}' has non-string 'primary-store' field"
                )
            if ps_key in primary_store_keys:
                raise ConfigError(
                    f"Parameters '{primary_store_keys[ps_key]}' and '{param_name}' "
                    f"share the same primary-store key '{ps_key}'"
                )
            primary_store_keys[ps_key] = param_name

        # Track secondary-store keys for duplicate detection
        if 'secondary-store' in param_decl:
            ss_key = param_decl['secondary-store']
            if not isinstance(ss_key, str):
                raise ConfigError(
                    f"Parameter '{param_name}' has non-string 'secondary-store' field"
                )
            if ss_key in secondary_store_keys:
                raise ConfigError(
                    f"Parameters '{secondary_store_keys[ss_key]}' and '{param_name}' "
                    f"share the same secondary-store key '{ss_key}'"
                )
            secondary_store_keys[ss_key] = param_name

    return schema, primary_store_keys, secondary_store_keys


def parse_value(value: str, value_type: str, param_name: str, source: str) -> str:
    """Parse a raw string value against the declared type.

    Returns the value as a string (possibly formatted per type rules).
    Raises ConfigError on parse failure.

    All resolved values are returned as strings.
    """
    value = value.strip()

    if value_type == 'string':
        return value

    elif value_type == 'integer':
        try:
            # Must be a decimal integer, no scientific notation
            parsed = int(value)
            # Verify no scientific notation was accepted (int() would handle it)
            # Check for scientific notation indicators
            if 'e' in value.lower() or 'E' in value:
                raise ConfigError(
                    f"Parameter '{param_name}' cannot parse source '{source}': "
                    f"scientific notation not allowed for integer"
                )
            return value
        except ValueError:
            raise ConfigError(
                f"Parameter '{param_name}': cannot parse source '{source}' "
                f"'{value}' as integer"
            )

    elif value_type == 'float':
        try:
            # Must be a decimal input, no scientific notation required
            if 'e' in value.lower() or 'E' in value:
                raise ConfigError(
                    f"Parameter '{param_name}' cannot parse source '{source}': "
                    f"scientific notation not allowed for float"
                )
            parsed = float(value)
            # Render with exactly 6 decimal digits
            return f"{parsed:.6f}"
        except ValueError:
            raise ConfigError(
                f"Parameter '{param_name}': cannot parse source '{source}' "
                f"'{value}' as float"
            )

    elif value_type == 'boolean':
        lower_value = value.lower()
        if lower_value in ('true', 'false'):
            return lower_value
        raise ConfigError(
            f"Parameter '{param_name}': cannot parse source '{source}' "
            f"'{value}' as boolean (expected 'true' or 'false')"
        )

    elif value_type == 'port':
        try:
            port = int(value)
        except ValueError:
            raise ConfigError(
                f"Parameter '{param_name}': cannot parse source '{source}' "
                f"'{value}' as port (not an integer)"
            )
        if port < 0 or port > 65535:
            raise ConfigError(
                f"Parameter '{param_name}': port out of range (0-65535): {port}"
            )
        # Check for leading zeros (except '0' is valid)
        if value != "0" and value.startswith("0"):
            raise ConfigError(
                f"Parameter '{param_name}': port has leading zeros: {value!r}"
            )
        return value

    else:
        raise ConfigError(
            f"Parameter '{param_name}': unknown type '{value_type}'"
        )


def fetch_primary_store_value(
    base_url: str,
    key: str,
    param_name: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Fetch a value from the primary store.

    Returns tuple of (value, response_body) where value is the string if found, None if not found.
    Raises ConfigError on connector failures or parse errors.
    """
    url = f"{base_url}/v1/primary/kv?key={urllib.parse.quote(key, safe='')}"

    try:
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    body = response.read().decode('utf-8')
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as e:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response is not valid JSON: {e}"
                        )

                    if not isinstance(data, dict):
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response must be a JSON object"
                        )

                    if 'found' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response missing 'found' field"
                        )

                    if not data['found']:
                        return None, data

                    if 'value' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store response for present key missing 'value' field"
                        )

                    value = data['value']
                    if not isinstance(value, str):
                        raise ConfigError(
                            f"Parameter '{param_name}': primary-store 'value' field must be a string"
                        )

                    return value, data
                else:
                    raise ConfigError(
                        f"Parameter '{param_name}': primary-store returned unexpected status {response.status}"
                    )
        except urllib.error.HTTPError as e:
            # Handle 404 as missing key (per spec: 404 Not Found with body {"found": false} means key missing)
            if e.code == 404:
                return None, None
            else:
                raise ConfigError(
                    f"Parameter '{param_name}': primary-store HTTP error {e.code}: {e.reason}"
                )
    except urllib.error.URLError as e:
        raise ConfigError(
            f"Parameter '{param_name}': primary-store network error: {e}"
        )


def fetch_primary_store_watch(
    base_url: str,
    cursor: int,
    keys: list[str]
) -> tuple[int, list[dict[str, Any]]]:
    """Watch for changes in the primary store.

    Returns tuple of (next_cursor, events_list).
    Raises ConfigError on connector failures or malformed responses.
    """
    params = [("cursor", str(cursor))] + [("key", k) for k in keys]
    query_string = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in params)
    url = f"{base_url}/v1/primary/watch?{query_string}"

    try:
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=300) as response:  # Long timeout for watch
                if response.status == 200:
                    body = response.read().decode('utf-8')
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as e:
                        raise ConfigError(f"primary-store watch response is not valid JSON: {e}")

                    if not isinstance(data, dict):
                        raise ConfigError("primary-store watch response must be a JSON object")

                    if 'cursor' not in data:
                        raise ConfigError("primary-store watch response missing 'cursor' field")

                    if 'events' not in data:
                        raise ConfigError("primary-store watch response missing 'events' field")

                    cursor_val = data['cursor']
                    events = data['events']

                    if not isinstance(cursor_val, int):
                        raise ConfigError("primary-store watch 'cursor' must be an integer")

                    if not isinstance(events, list):
                        raise ConfigError("primary-store watch 'events' must be an array")

                    return cursor_val, events
                else:
                    raise ConfigError(f"primary-store watch returned unexpected status {response.status}")
        except urllib.error.HTTPError as e:
            raise ConfigError(f"primary-store watch HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ConfigError(f"primary-store watch network error: {e}")


def fetch_secondary_store_value(
    base_url: str,
    key: str,
    param_name: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Fetch a value from the secondary store.

    Returns tuple of (value, response_body) where value is the string if found, None if not found.
    Raises ConfigError on connector failures or parse errors.
    """
    url = f"{base_url}/v1/secondary/kv?key={urllib.parse.quote(key, safe='')}"

    try:
        req = urllib.request.Request(url, method='GET')
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    body = response.read().decode('utf-8')
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError as e:
                        raise ConfigError(
                            f"Parameter '{param_name}': secondary-store response is not valid JSON: {e}"
                        )

                    if not isinstance(data, dict):
                        raise ConfigError(
                            f"Parameter '{param_name}': secondary-store response must be a JSON object"
                        )

                    if 'found' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': secondary-store response missing 'found' field"
                        )

                    if not data['found']:
                        return None, data

                    if 'value' not in data:
                        raise ConfigError(
                            f"Parameter '{param_name}': secondary-store response for present key missing 'value' field"
                        )

                    value = data['value']
                    if not isinstance(value, str):
                        raise ConfigError(
                            f"Parameter '{param_name}': secondary-store 'value' field must be a string"
                        )

                    return value, data
                else:
                    raise ConfigError(
                        f"Parameter '{param_name}': secondary-store returned unexpected status {response.status}"
                    )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, None
            else:
                raise ConfigError(
                    f"Parameter '{param_name}': secondary-store HTTP error {e.code}: {e.reason}"
                )
    except urllib.error.URLError as e:
        raise ConfigError(
            f"Parameter '{param_name}': secondary-store network error: {e}"
        )


def fetch_secondary_store_batch(
    base_url: str,
    keys: list[str]
) -> list[dict[str, Any]]:
    """Batch read from secondary store.

    Returns list of items with key, status, and conditionally value or error.
    Raises ConfigError on connector failures or malformed responses.
    """
    url = f"{base_url}/v1/secondary/batch-read"
    body = json.dumps({"keys": keys})

    try:
        req = urllib.request.Request(
            url,
            data=body.encode('utf-8'),
            method='POST',
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                if response.status == 200:
                    resp_body = response.read().decode('utf-8')
                    try:
                        data = json.loads(resp_body)
                    except json.JSONDecodeError as e:
                        raise ConfigError(f"secondary-store batch-read response is not valid JSON: {e}")

                    if not isinstance(data, dict):
                        raise ConfigError("secondary-store batch-read response must be a JSON object")

                    if 'items' not in data:
                        raise ConfigError("secondary-store batch-read response missing 'items' field")

                    items = data['items']
                    if not isinstance(items, list):
                        raise ConfigError("secondary-store batch-read 'items' must be an array")

                    return items
                else:
                    raise ConfigError(f"secondary-store batch-read returned unexpected status {response.status}")
        except urllib.error.HTTPError as e:
            raise ConfigError(f"secondary-store batch-read HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ConfigError(f"secondary-store batch-read network error: {e}")


def resolve_parameter_sources(
    param_name: str,
    param_decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
    secondary_store_cache: dict[str, str]
) -> tuple[str | None, str]:
    """Resolve a single parameter from its declared sources.

    Priority order (highest to lowest): arg > secondary-store > primary-store > file > env > default.
    Returns the resolved value as a string, or None if unresolved.
    Raises ConfigError on parse failure.
    """
    value_type = param_decl['type']

    # 1. Check CLI argument (highest priority)
    if 'arg' in param_decl:
        arg_name = param_decl['arg']
        if arg_name in cli_args:
            raw_value = cli_args[arg_name]
            return parse_value(raw_value, value_type, param_name, 'arg'), 'arg'

    # 2. Check secondary store (second highest priority)
    if secondary_store_url is not None and 'secondary-store' in param_decl:
        ss_key = param_decl['secondary-store']
        # Check cached first
        if ss_key in secondary_store_cache:
            raw_value = secondary_store_cache[ss_key]
            try:
                return parse_value(raw_value, value_type, param_name, 'secondary-store'), 'secondary-store'
            except ConfigError as e:
                raise ConfigError(f"Parameter '{param_name}': secondary-store value parse error: {e}")

    # 3. Check primary store (third highest priority)
    if primary_store_url is not None and 'primary-store' in param_decl:
        ps_key = param_decl['primary-store']
        raw_value = fetch_primary_store_value(primary_store_url, ps_key, param_name)
        if raw_value is not None:
            return parse_value(raw_value, value_type, param_name, 'primary-store'), 'primary-store'

    # 4. Check file
    if 'file' in param_decl:
        file_path = param_decl['file']
        try:
            with open(file_path, 'r') as f:
                raw_value = f.read()
        except (FileNotFoundError, OSError):
            raw_value = None

        if raw_value is not None:
            raw_value = raw_value.strip()
            if raw_value != '':
                return parse_value(raw_value, value_type, param_name, 'file'), 'file'

    # 5. Check environment variable
    if 'env' in param_decl:
        env_name = param_decl['env']
        try:
            raw_value = os.environ[env_name]
        except KeyError:
            raw_value = None

        if raw_value is not None and raw_value.strip() != '':
            return parse_value(raw_value, value_type, param_name, 'env'), 'env'

    # 6. Check default (lowest priority)
    if 'default' in param_decl:
        raw_value = param_decl['default']
        return parse_value(raw_value, value_type, param_name, 'default'), 'default'

    # No source provided a value
    return None, ''


def emit_change_event(path: str, type_name: str, previous: str, current: str) -> None:
    """Emit a change event as JSON line to stdout."""
    event = {
        'path': path,
        'type': type_name,
        'previous': previous,
        'current': current
    }
    print(json.dumps(event))
    sys.stdout.flush()


def resolve_with_events(
    schema: dict[str, dict[str, Any]],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
    emit_event: bool = True
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve all parameters and emit change events.

    Returns tuple of (resolved_values, sources_used).
    """
    resolved: dict[str, str] = {}
    sources: dict[str, str] = {}
    unresolved: list[str] = []
    secondary_store_cache: dict[str, str] = {}

    # Build map of secondary-store keys
    secondary_store_keys_map: dict[str, str] = {}  # key -> param_path
    for param_name, param_decl in schema.items():
        if 'secondary-store' in param_decl:
            ss_key = param_decl['secondary-store']
            secondary_store_keys_map[ss_key] = param_name

    # First pass: fetch all secondary store values (batch)
    if secondary_store_url is not None and secondary_store_keys_map:
        keys = list(secondary_store_keys_map.keys())
        try:
            items = fetch_secondary_store_batch(secondary_store_url, keys)
            for item in items:
                key = item.get('key')
                status = item.get('status')
                if status == 'ok' and key in secondary_store_keys_map:
                    value = item.get('value')
                    if isinstance(value, str):
                        secondary_store_cache[key] = value
        except ConfigError as e:
            raise ConfigError(f"secondary-store batch-read failed: {e}")

    for param_name in sorted(schema.keys()):  # Process in consistent order
        param_decl = schema[param_name]
        value_type = param_decl['type']

        try:
            value, source = resolve_parameter_sources(
                param_name, param_decl, cli_args,
                primary_store_url, secondary_store_url, secondary_store_cache
            )

            if value is None:
                unresolved.append(param_name)
            else:
                old_value = resolved.get(param_name, '')
                resolved[param_name] = value
                sources[param_name] = source
                if emit_event:
                    emit_change_event(param_name, value_type, old_value, value)
        except ConfigError as e:
            raise ConfigError(f"Parameter '{param_name}': {e}")

    if unresolved:
        if len(unresolved) == 1:
            raise ConfigError(f"Parameter '{unresolved[0]}' is unresolved")
        else:
            params_str = "', '".join(unresolved)
            raise ConfigError(f"Parameters '{params_str}' are unresolved")

    return resolved, sources


def build_output(schema: dict[str, dict[str, Any]], resolved: dict[str, str]) -> dict[str, Any]:
    """Build nested output dict from resolved parameters, preserving group structure."""
    def copy_structure(schema_node: dict[str, Any]) -> dict[str, Any]:
        """Copy the schema structure without values."""
        node = {}
        for key, value in schema_node.items():
            if 'type' in value:
                # This is a leaf parameter - skip
                continue
            else:
                # This is a group
                node[key] = copy_structure(value)
        return node

    result = copy_structure(schema)

    # Fill in resolved values
    for path, value in resolved.items():
        parts = path.split('.')
        current = result
        for part in parts[:-1]:
            if part not in current:
                # Create intermediate structure
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    return result


def run_watch_mode(
    schema: dict[str, dict[str, Any]],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
    secondary_store_keys_map: dict[str, str],
    poll_interval: float
) -> None:
    """Run in watch mode with continuous monitoring."""
    # Track last known values for each parameter
    last_values: dict[str, str] = {}
    last_primary_versions: dict[str, int] = {}  # key -> version

    # Track baseline values for secondary store keys
    secondary_store_baseline: dict[str, str] = {}
    secondary_store_monitoring: bool = bool(secondary_store_keys_map)

    # Initial resolution - emit seed events
    resolved, _ = resolve_with_events(
        schema, cli_args, primary_store_url, secondary_store_url, emit_event=True
    )

    # Track initial values from seed events
    for path, value in resolved.items():
        last_values[path] = value

    # Emit the full resolved configuration
    output = build_output(schema, resolved)
    print(json.dumps(output))
    sys.stdout.flush()

    # If no monitorable sources, exit normally
    if primary_store_url is None and not secondary_store_monitoring:
        return

    # Start monitoring threads
    stop_event = threading.Event()

    def monitor_primary_store() -> None:
        """Monitor primary store for changes."""
        nonlocal stop_event

        # Build list of monitored keys
        monitored_keys: list[str] = []
        key_to_path: dict[str, str] = {}

        for param_name, param_decl in schema.items():
            if 'primary-store' in param_decl:
                key = param_decl['primary-store']
                monitored_keys.append(key)
                key_to_path[key] = param_name

        if not monitored_keys:
            return

        cursor = 0
        while not stop_event.is_set():
            try:
                new_cursor, events = fetch_primary_store_watch(
                    primary_store_url, cursor, monitored_keys
                )
                cursor = new_cursor

                for event in events:
                    key = event.get('key')
                    new_version = event.get('version')
                    value = event.get('value')

                    if key is None or key not in key_to_path:
                        continue

                    param_path = key_to_path[key]
                    old_version = last_primary_versions.get(key, 0)

                    # Only apply if newer than last applied
                    if new_version <= old_version:
                        continue

                    # Update the value
                    param_decl = schema.get(param_path)
                    if param_decl is None:
                        continue

                    value_type = param_decl['type']
                    old_string = last_values.get(param_path, '')

                    try:
                        new_string = parse_value(
                            value, value_type, param_path, 'primary-store'
                        )
                    except ConfigError as e:
                        print(f"Error: {e}", file=sys.stderr)
                        stop_event.set()
                        return

                    # Emit event if value changed
                    if new_string != old_string:
                        last_values[param_path] = new_string
                        emit_change_event(param_path, value_type, old_string, new_string)

                    last_primary_versions[key] = new_version

            except ConfigError as e:
                print(f"Error: {e}", file=sys.stderr)
                stop_event.set()
                return

    def monitor_secondary_store() -> None:
        """Poll secondary store for changes."""
        nonlocal stop_event

        if not secondary_store_monitoring:
            return

        keys = list(secondary_store_keys_map.keys())
        poll_interval_sec = poll_interval

        while not stop_event.is_set():
            try:
                time.sleep(poll_interval_sec)

                items = fetch_secondary_store_batch(secondary_store_url, keys)

                for item in items:
                    key = item.get('key')
                    status = item.get('status')

                    if key is None or key not in secondary_store_keys_map:
                        continue

                    param_path = secondary_store_keys_map[key]

                    if status == 'ok':
                        value = item.get('value', '')
                        if not isinstance(value, str):
                            continue

                        old_baseline = secondary_store_baseline.get(key, '')

                        # Parse to validate
                        param_decl = schema.get(param_path)
                        if param_decl is None:
                            continue

                        value_type = param_decl['type']

                        try:
                            parsed_string = parse_value(
                                value, value_type, param_path, 'secondary-store'
                            )
                        except ConfigError:
                            # Silently skip parse-failing observations
                            continue

                        # Emit event if different from baseline
                        if not secondary_store_baseline:
                            # First observation - emit baseline event
                            old_string = last_values.get(param_path, '')
                            last_values[param_path] = parsed_string
                            secondary_store_baseline[key] = parsed_string
                            emit_change_event(param_path, value_type, old_string, parsed_string)
                        elif parsed_string != old_baseline:
                            old_baseline_str = secondary_store_baseline.get(key, '')
                            if parsed_string != old_baseline_str:
                                secondary_store_baseline[key] = parsed_string
                                last_values[param_path] = parsed_string
                                emit_change_event(
                                    param_path, value_type, old_baseline_str, parsed_string
                                )

                    elif status == 'missing':
                        # Key missing - silently skip
                        pass

                    elif status == 'error':
                        # Error - silently skip, monitoring continues
                        pass

            except ConfigError as e:
                print(f"Error: {e}", file=sys.stderr)
                stop_event.set()
                return

    # Start monitoring threads
    primary_thread = threading.Thread(target=monitor_primary_store, daemon=True)
    secondary_thread = threading.Thread(target=monitor_secondary_store, daemon=True)

    if primary_store_url is not None:
        primary_thread.start()
    if secondary_store_monitoring:
        secondary_thread.start()

    # Wait for termination
    try:
        while not stop_event.is_set():
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_event.set()

    # Wait for threads to finish (should be quick after stop_event)
    if primary_store_url is not None:
        primary_thread.join(timeout=1)
    if secondary_store_monitoring:
        secondary_thread.join(timeout=1)


def main() -> int:
    """Main entry point.

    Returns exit code (0 for success, non-zero for failure).
    """
    parser = argparse.ArgumentParser(
        prog='cfgpipe',
        description='Configuration resolver',
        add_help=False
    )
    parser.add_argument('--primary-store', help='Primary store base URL')
    parser.add_argument('--secondary-store', help='Secondary store base URL')
    parser.add_argument('--secondary-store-poll-interval', help='Secondary store poll interval (e.g., 2s, 1000ms)')
    parser.add_argument('--watch', action='store_true', help='Enable watch mode')
    parser.add_argument('schema_file', help='Path to JSON schema file')
    parser.add_argument('arg_candidates', nargs='*', help='CLI argument candidates')

    # Parse known args to get schema file and options
    args, remaining = parser.parse_known_args()

    # Parse poll interval
    poll_interval: float | None = None
    if args.secondary_store_poll_interval:
        interval_str = args.secondary_store_poll_interval
        if interval_str.endswith('ms'):
            try:
                poll_interval = float(interval_str[:-2]) / 1000.0
            except ValueError:
                print(f"Error: Invalid poll interval '{interval_str}'", file=sys.stderr)
                return 1
        elif interval_str.endswith('s'):
            try:
                poll_interval = float(interval_str[:-1])
            except ValueError:
                print(f"Error: Invalid poll interval '{interval_str}'", file=sys.stderr)
                return 1
        else:
            try:
                poll_interval = float(interval_str)
            except ValueError:
                print(f"Error: Invalid poll interval '{interval_str}'", file=sys.stderr)
                return 1

    # Read and validate schema
    try:
        schema, primary_store_keys, secondary_store_keys = read_schema(args.schema_file)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Validate primary-store configuration
    if primary_store_keys:
        if args.primary_store is None:
            params_with_ps = list(primary_store_keys.values())
            if len(params_with_ps) == 1:
                print(f"Error: Parameter '{params_with_ps[0]}' declares 'primary-store' but no --primary-store flag provided", file=sys.stderr)
            else:
                params_str = "', '".join(params_with_ps)
                print(f"Error: Parameters '{params_str}' declare 'primary-store' but no --primary-store flag provided", file=sys.stderr)
            return 1

    # Validate secondary-store configuration for watch mode
    if args.watch:
        if poll_interval is not None and poll_interval <= 0:
            print("Error: --secondary-store-poll-interval must be strictly positive in watch mode", file=sys.stderr)
            return 1

        # Check if any parameters declare secondary-store
        if secondary_store_keys:
            if args.secondary_store is None:
                params_with_ss = list(secondary_store_keys.values())
                if len(params_with_ss) == 1:
                    print(f"Error: Parameter '{params_with_ss[0]}' declares 'secondary-store' but no --secondary-store flag provided", file=sys.stderr)
                else:
                    params_str = "', '".join(params_with_ss)
                    print(f"Error: Parameters '{params_str}' declare 'secondary-store' but no --secondary-store flag provided", file=sys.stderr)
                return 1
        else:
            # No parameters declare secondary-store, but flag was provided - check if that's an issue
            # According to spec, if --watch and --secondary-store are present but no parameters declare secondary-store,
            # that counts as the declared-key failure
            if args.secondary_store is not None:
                print("Error: --secondary-store provided but no parameters declare 'secondary-store'", file=sys.stderr)
                return 1

    # Validate poll interval requirement when secondary-store is used in watch mode
    if args.watch and args.secondary_store is not None and secondary_store_keys:
        if poll_interval is None:
            print("Error: --secondary-store-poll-interval is required when --secondary-store is used with --watch", file=sys.stderr)
            return 1
        if poll_interval <= 0:
            print("Error: --secondary-store-poll-interval must be strictly positive in watch mode", file=sys.stderr)
            return 1

    # Parse CLI arguments from remaining (including arg_candidates)
    all_cli_args = args.arg_candidates + remaining
    cli_args = parse_cli_args(all_cli_args)

    if args.watch:
        # Run in watch mode
        try:
            run_watch_mode(
                schema, cli_args,
                args.primary_store,
                args.secondary_store,
                secondary_store_keys,
                poll_interval if poll_interval is not None else 1.0
            )
            return 0
        except ConfigError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    # Non-watch mode: simple resolution
    try:
        resolved, _ = resolve_with_events(
            schema, cli_args, args.primary_store, args.secondary_store, emit_event=False
        )
        output = build_output(schema, resolved)
        print(json.dumps(output))
        return 0
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
