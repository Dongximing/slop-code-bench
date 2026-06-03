#!/usr/bin/env python3
"""
cfgpipe - Command-line configuration resolver.

Reads a JSON schema document, resolves each declared parameter from local sources,
and writes the resolved configuration to stdout as JSON.

Watch mode monitors primary-store and secondary-store for runtime changes
and emits structured change events as newline-delimited JSON.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Type parsing / formatting helpers
# ---------------------------------------------------------------------------

def parse_boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ('true', 'yes', '1', 'on', 'y'):
        return True
    if normalized in ('false', 'no', '0', 'off', 'n'):
        return False
    raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for integer: '{value}'")
    try:
        result = int(stripped)
        if '.' in stripped:
            raise ValueError(f"Float value '{value}' cannot be parsed as integer")
        return result
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as integer")


def parse_float(value: str) -> float:
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not supported: '{value}'")
    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def parse_port(value: str) -> int:
    stripped = value.strip()
    if 'e' in stripped.lower():
        raise ValueError(f"Scientific notation not allowed for port: '{value}'")
    if '.' in stripped:
        raise ValueError(f"Float value '{value}' cannot be parsed as port")
    try:
        result = int(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as port")
    if stripped != '0' and stripped != '-0':
        sign_stripped = stripped[1:] if stripped.startswith('-') else stripped
        if len(sign_stripped) > 1 and sign_stripped.startswith('0'):
            raise ValueError(f"Port '{value}' has leading zeros")
    if result < 0 or result > 65535:
        raise ValueError(f"Port '{value}' out of range (must be 0-65535)")
    return result


def format_boolean(value: bool) -> str:
    return 'true' if value else 'false'


def format_integer(value: int) -> str:
    return str(value)


def format_float(value: float) -> str:
    return f"{value:.6f}"


def format_port(value: int) -> str:
    return str(value)


def parse_value(value: str, type_name: str) -> Tuple[Any, str]:
    """Parse a string value according to the declared type.
    Returns (parsed_value, formatted_string).
    """
    if type_name == 'string':
        return value, value
    elif type_name == 'integer':
        parsed = parse_integer(value)
        return parsed, format_integer(parsed)
    elif type_name == 'float':
        parsed = parse_float(value)
        return parsed, format_float(parsed)
    elif type_name == 'boolean':
        parsed = parse_boolean(value)
        return parsed, format_boolean(parsed)
    elif type_name == 'port':
        parsed = parse_port(value)
        return parsed, format_port(parsed)
    else:
        raise ValueError(f"Unknown type: '{type_name}'")


def format_change_value(type_name: str, formatted_str: str) -> str:
    """Return the string representation for a change event's current/previous."""
    return formatted_str


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

def validate_schema(schema: Dict[str, Any]) -> None:
    """Validate the schema structure including groups and nested parameters."""
    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")
    if not schema:
        raise ValueError("Schema root must be a non-empty object")

    primary_store_keys: Dict[str, str] = {}
    secondary_store_keys: Dict[str, str] = {}

    def validate_node(node: Dict[str, Any], path: str) -> None:
        if 'type' in node and isinstance(node['type'], str):
            type_name = node['type']
            valid_types = ('string', 'integer', 'float', 'boolean', 'port')
            if type_name not in valid_types:
                raise ValueError(f"Parameter '{path}' has unrecognized type: '{type_name}'")

            for field in ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'):
                if field in node:
                    if not isinstance(node[field], str):
                        raise ValueError(f"Parameter '{path}' has non-string '{field}' field")

            if 'primary-store' in node:
                ps_key = node['primary-store']
                if ps_key in primary_store_keys:
                    other_param = primary_store_keys[ps_key]
                    raise ValueError(
                        f"Schema error: parameters '{other_param}' and '{path}' share the same primary-store key '{ps_key}'"
                    )
                primary_store_keys[ps_key] = path

            if 'secondary-store' in node:
                ss_key = node['secondary-store']
                if ss_key in secondary_store_keys:
                    other_param = secondary_store_keys[ss_key]
                    raise ValueError(
                        f"Schema error: parameters '{other_param}' and '{path}' share the same secondary-store key '{ss_key}'"
                    )
                secondary_store_keys[ss_key] = path
        else:
            source_annotations = ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store')
            for field in source_annotations:
                if field in node and not isinstance(node[field], dict):
                    raise ValueError(f"Group '{path}' has non-object '{field}' field")

            if 'type' in node and isinstance(node['type'], str):
                raise ValueError(f"Group '{path}' has a 'type' field")

            for name, child in node.items():
                if name in source_annotations and isinstance(child, dict):
                    continue
                if not isinstance(child, dict):
                    raise ValueError(f"Entry '{path}.{name}' must be an object")
                child_path = f"{path}.{name}" if path else name
                validate_node(child, child_path)

    for name, child in schema.items():
        if not isinstance(child, dict):
            raise ValueError(f"Entry '{name}' must be an object")
        validate_node(child, name)


# ---------------------------------------------------------------------------
# CLI flag parsing
# ---------------------------------------------------------------------------

def parse_global_flags(args: List[str]) -> Tuple[Dict[str, str], List[str]]:
    """Parse global flags from command-line arguments."""
    flags: Dict[str, str] = {}
    remaining: List[str] = []
    i = 0
    watch = False

    while i < len(args):
        arg = args[i]
        if arg == '--primary-store':
            if i + 1 >= len(args):
                print("Error: --primary-store requires a value", file=sys.stderr)
                sys.exit(1)
            flags['primary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--primary-store='):
            flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store':
            if i + 1 >= len(args):
                print("Error: --secondary-store requires a value", file=sys.stderr)
                sys.exit(1)
            flags['secondary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store='):
            flags['secondary-store'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store-poll-interval':
            if i + 1 >= len(args):
                print("Error: --secondary-store-poll-interval requires a value", file=sys.stderr)
                sys.exit(1)
            flags['secondary-store-poll-interval'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store-poll-interval='):
            flags['secondary-store-poll-interval'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--watch':
            watch = True
            i += 1
        else:
            remaining.append(arg)
            i += 1

    if watch:
        flags['__watch__'] = 'true'
    return flags, remaining


def parse_duration(duration_str: str) -> float:
    """Parse a duration string like '2s', '500ms', '1m' into seconds."""
    duration_str = duration_str.strip()
    match = re.match(r'^(\d+(?:\.\d+)?)(ms|s|m)?$', duration_str)
    if not match:
        raise ValueError(f"Invalid duration: '{duration_str}'")
    value = float(match.group(1))
    unit = match.group(2) or 's'
    if unit == 'ms':
        return value / 1000.0
    elif unit == 's':
        return value
    elif unit == 'm':
        return value * 60.0
    return value


# ---------------------------------------------------------------------------
# Source lookups
# ---------------------------------------------------------------------------

def get_arg_value(arg_candidates: List[str], arg_name: str) -> Optional[str]:
    result = None
    for arg in arg_candidates:
        if arg.startswith(f"--{arg_name}="):
            result = arg.split('=', 1)[1]
        elif arg.startswith(f"-{arg_name}="):
            result = arg.split('=', 1)[1]
    return result


def get_env_value(env_var: str) -> Optional[str]:
    return os.environ.get(env_var)


def get_file_value(file_path: str) -> Optional[str]:
    if not os.path.exists(file_path):
        return None
    if not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, 'r') as f:
            content = f.read()
        trimmed = content.strip()
        if not trimmed:
            return None
        return trimmed
    except (IOError, OSError):
        return None


def query_primary_store(base_url: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """Query the primary store for a key.
    Returns: (value, None) if key found, (None, None) if not found, (None, error) on failure.
    """
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"
    try:
        request = urllib.request.Request(url, method='GET')
        request.add_header('Accept', 'application/json')
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                return None, f"Primary store returned non-200 status: {response.status}"
            body = response.read().decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None, f"Primary store returned malformed JSON: {body}"
            if not isinstance(data, dict):
                return None, f"Primary store returned non-object response: {body}"
            if 'found' not in data:
                return None, f"Primary store response missing 'found' field: {body}"
            if data['found'] is False:
                return None, None
            if data['found'] is True:
                if 'value' not in data:
                    return None, f"Primary store response missing 'value' field: {body}"
                if not isinstance(data['value'], str):
                    return None, f"Primary store 'value' is not a string: {body}"
                return data['value'], None
            return None, f"Primary store returned invalid 'found' value: {body}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                body = e.read().decode('utf-8')
                data = json.loads(body)
                if isinstance(data, dict) and data.get('found') is False:
                    return None, None
                return None, f"Primary store 404 response malformed: {body}"
            except Exception:
                return None, f"Primary store 404 response malformed: {body}"
        else:
            return None, f"Primary store returned HTTP error {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"Primary store network error: {e.reason}"
    except Exception as e:
        return None, f"Primary store connector failure: {str(e)}"


def query_secondary_store(base_url: str, key: str) -> Tuple[Optional[str], Optional[str]]:
    """Query the secondary store for a key.
    Returns: (value, None) if found, (None, None) if missing, (None, error) on failure.
    """
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/secondary/kv?key={encoded_key}"
    try:
        request = urllib.request.Request(url, method='GET')
        request.add_header('Accept', 'application/json')
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status != 200:
                return None, f"Secondary store returned non-200 status: {response.status}"
            body = response.read().decode('utf-8')
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return None, f"Secondary store returned malformed JSON: {body}"
            if not isinstance(data, dict):
                return None, f"Secondary store returned non-object response: {body}"
            if 'found' not in data:
                return None, f"Secondary store response missing 'found' field: {body}"
            if data['found'] is False:
                return None, None
            if data['found'] is True:
                if 'value' not in data:
                    return None, f"Secondary store response missing 'value' field: {body}"
                if not isinstance(data['value'], str):
                    return None, f"Secondary store 'value' is not a string: {body}"
                return data['value'], None
            return None, f"Secondary store returned invalid 'found' value: {body}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                body = e.read().decode('utf-8')
                data = json.loads(body)
                if isinstance(data, dict) and data.get('found') is False:
                    return None, None
                return None, f"Secondary store 404 response malformed: {body}"
            except Exception:
                return None, f"Secondary store 404 response malformed: {body}"
        else:
            return None, f"Secondary store returned HTTP error {e.code}: {e.reason}"
    except urllib.error.URLError as e:
        return None, f"Secondary store network error: {e.reason}"
    except Exception as e:
        return None, f"Secondary store connector failure: {str(e)}"


# ---------------------------------------------------------------------------
# Parameter collection & validation
# ---------------------------------------------------------------------------

def collect_parameters(schema: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """Collect all parameters from the schema with their composed paths."""
    result = []

    def collect_node(node: Dict[str, Any], path: str) -> None:
        if 'type' in node and isinstance(node['type'], str):
            result.append((path, node))
        else:
            source_annotations = ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store')
            for name, child in node.items():
                if name in source_annotations and isinstance(child, dict):
                    continue
                if isinstance(child, dict):
                    child_path = f"{path}.{name}" if path else name
                    collect_node(child, child_path)

    for name, child in schema.items():
        collect_node(child, name)
    return result


def validate_store_config(
    schema: Dict[str, Any],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    watch_mode: bool,
    poll_interval: Optional[float],
) -> None:
    """Validate store configuration requirements."""
    parameters = collect_parameters(schema)

    primary_params = [(p, d) for p, d in parameters if 'primary-store' in d]
    secondary_params = [(p, d) for p, d in parameters if 'secondary-store' in d]

    if primary_params and primary_store_url is None:
        param_name = primary_params[0][0]
        print(
            f"Error: Parameter '{param_name}' declares primary-store but --primary-store flag is not set",
            file=sys.stderr
        )
        sys.exit(1)

    if secondary_params and secondary_store_url is None:
        param_name = secondary_params[0][0]
        print(
            f"Error: Parameter '{param_name}' declares secondary-store but --secondary-store flag is not set",
            file=sys.stderr
        )
        sys.exit(1)

    if secondary_store_url is not None and not secondary_params:
        print(
            "Error: --secondary-store flag is set but no parameters declare secondary-store",
            file=sys.stderr
        )
        sys.exit(1)

    if watch_mode and secondary_store_url is not None:
        if poll_interval is None or poll_interval <= 0:
            print(
                "Error: --secondary-store-poll-interval must be strictly positive when --watch and --secondary-store are both present",
                file=sys.stderr
            )
            sys.exit(1)

    if watch_mode and secondary_store_url is not None and not secondary_params:
        print(
            "Error: --watch and --secondary-store are present but no parameters declare secondary-store keys",
            file=sys.stderr
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------

def resolve_parameter(
    path: str,
    param: Dict[str, Any],
    arg_candidates: List[str],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
) -> Tuple[Optional[str], Optional[Tuple[str, str, str]]]:
    """Resolve a single parameter from its declared sources.
    Priority: default, env, file, primary-store, secondary-store, arg
    Returns: (formatted_value, None) on success,
             (None, (param_path, source, reason)) on parse failure,
             (None, None) if unresolved.
    """
    type_name = param['type']
    sources: List[Tuple[str, str]] = []

    # Highest priority: arg
    if 'arg' in param:
        arg_value = get_arg_value(arg_candidates, param['arg'])
        if arg_value is not None:
            sources.append(('arg', arg_value))

    # secondary-store overrides primary-store
    if 'secondary-store' in param and secondary_store_url is not None:
        ss_key = param['secondary-store']
        ss_value, ss_error = query_secondary_store(secondary_store_url, ss_key)
        if ss_error is not None:
            return None, (path, 'secondary-store', ss_error)
        if ss_value is not None:
            sources.append(('secondary-store', ss_value))

    if 'primary-store' in param and primary_store_url is not None:
        ps_key = param['primary-store']
        ps_value, ps_error = query_primary_store(primary_store_url, ps_key)
        if ps_error is not None:
            return None, (path, 'primary-store', ps_error)
        if ps_value is not None:
            sources.append(('primary-store', ps_value))

    if 'file' in param:
        file_value = get_file_value(param['file'])
        if file_value is not None:
            sources.append(('file', file_value))

    if 'env' in param:
        env_value = get_env_value(param['env'])
        if env_value is not None:
            sources.append(('env', env_value))

    if 'default' in param:
        sources.append(('default', param['default']))

    for source_name, source_value in sources:
        try:
            _, formatted = parse_value(source_value, type_name)
            return formatted, None
        except ValueError as e:
            return None, (path, source_name, str(e))

    return None, None


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def build_nested_output(resolved_values: Dict[str, str]) -> Dict[str, Any]:
    """Build a nested output structure from composed paths and resolved values."""
    result: Dict[str, Any] = {}
    for path, value in resolved_values.items():
        parts = path.split('.')
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return result


def emit_change_event(path: str, type_name: str, previous: str, current: str) -> None:
    """Emit a change event as a single-line JSON object."""
    event = {
        "path": path,
        "type": type_name,
        "previous": previous,
        "current": current,
    }
    sys.stdout.write(json.dumps(event) + '\n')
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Watch mode: primary-store monitoring
# ---------------------------------------------------------------------------

def primary_store_watch_request(
    base_url: str, cursor: int, keys: List[str]
) -> Tuple[int, List[Dict[str, Any]]]:
    """Make a primary-store watch request.
    Returns (new_cursor, events_list).
    Raises on failure.
    """
    params = [f"cursor={cursor}"]
    for key in keys:
        params.append(f"key={urllib.parse.quote(key, safe='')}")
    url = f"{base_url.rstrip('/')}/v1/primary/watch?{'&'.join(params)}"

    request = urllib.request.Request(url, method='GET')
    request.add_header('Accept', 'application/json')
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode('utf-8')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError(f"Primary store watch returned non-object: {body}")
        if 'cursor' not in data or 'events' not in data:
            raise ValueError(f"Primary store watch response missing cursor/events: {body}")
        return data['cursor'], data['events']


def secondary_store_batch_read(
    base_url: str, keys: List[str]
) -> List[Dict[str, Any]]:
    """Make a secondary-store batch-read request.
    Returns items list. Raises on failure.
    """
    url = f"{base_url.rstrip('/')}/v1/secondary/batch-read"
    body_data = json.dumps({"keys": keys}).encode('utf-8')
    request = urllib.request.Request(url, data=body_data, method='POST')
    request.add_header('Content-Type', 'application/json')
    request.add_header('Accept', 'application/json')
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode('utf-8')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError(f"Secondary store batch-read returned non-object: {body}")
        if 'items' not in data:
            raise ValueError(f"Secondary store batch-read response missing items: {body}")
        return data['items']


# ---------------------------------------------------------------------------
# Watch mode orchestration
# ---------------------------------------------------------------------------

class WatchState:
    """Shared mutable state for watch mode."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_values: Dict[str, str] = {}   # path -> formatted string value
        self.param_types: Dict[str, str] = {}       # path -> type name
        self.primary_versions: Dict[str, int] = {}  # primary-store key -> last applied version
        self.secondary_baselines: Dict[str, Optional[str]] = {}  # secondary-store key -> last successful observation string (None = no baseline yet)
        self.error: Optional[str] = None
        self.stop_event = threading.Event()


def run_watch_mode(
    parameters: List[Tuple[str, Dict[str, Any]]],
    resolved: Dict[str, str],
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    poll_interval: Optional[float],
) -> int:
    """Run watch mode: emit seed events, config line, then monitor for changes."""

    state = WatchState()
    state.current_values = dict(resolved)

    # Classify parameters by store
    primary_monitored: List[Tuple[str, Dict[str, Any]]] = []
    secondary_monitored: List[Tuple[str, Dict[str, Any]]] = []

    for path, param in parameters:
        state.param_types[path] = param['type']
        if 'primary-store' in param:
            primary_monitored.append((path, param))
        if 'secondary-store' in param:
            secondary_monitored.append((path, param))

    # --- Seed-time change events ---
    for path, param in parameters:
        type_name = param['type']
        current = resolved[path]
        emit_change_event(path, type_name, "", current)

    # --- Full resolved configuration ---
    output = build_nested_output(resolved)
    sys.stdout.write(json.dumps(output) + '\n')
    sys.stdout.flush()

    # If nothing to monitor, exit normally
    if not primary_monitored and not secondary_monitored:
        return 0

    # --- Start monitoring threads ---
    threads: List[threading.Thread] = []

    if primary_monitored:
        t = threading.Thread(
            target=_primary_monitor,
            args=(state, primary_monitored, primary_store_url),
            daemon=True,
        )
        threads.append(t)

    if secondary_monitored and secondary_store_url is not None and poll_interval is not None:
        t = threading.Thread(
            target=_secondary_monitor,
            args=(state, secondary_monitored, secondary_store_url, poll_interval),
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()

    # Wait for error or interruption
    try:
        while True:
            with state.lock:
                if state.error is not None:
                    print(f"Error: {state.error}", file=sys.stderr)
                    return 1
            # Check if all threads are still alive
            all_dead = all(not t.is_alive() for t in threads)
            if all_dead:
                with state.lock:
                    if state.error is not None:
                        print(f"Error: {state.error}", file=sys.stderr)
                        return 1
                # All threads exited without error
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        state.stop_event.set()

    # Wait for threads to finish
    for t in threads:
        t.join(timeout=2.0)

    with state.lock:
        if state.error is not None:
            print(f"Error: {state.error}", file=sys.stderr)
            return 1

    return 0


def _primary_monitor(
    state: WatchState,
    monitored: List[Tuple[str, Dict[str, Any]]],
    base_url: str,
) -> None:
    """Monitor primary-store for changes using the watch endpoint."""
    # Build key -> (path, param) mapping
    key_to_param: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for path, param in monitored:
        key = param['primary-store']
        key_to_param[key] = (path, param)
    keys = list(key_to_param.keys())

    cursor = 0

    while not state.stop_event.is_set():
        try:
            new_cursor, events = primary_store_watch_request(base_url, cursor, keys)
        except Exception as e:
            with state.lock:
                state.error = f"Primary store watch failed: {str(e)}"
            return

        for event in events:
            key = event.get('key')
            value = event.get('value')
            version = event.get('version')

            if key not in key_to_param:
                continue

            path, param = key_to_param[key]

            # Version enforcement: only apply if newer
            with state.lock:
                last_version = state.primary_versions.get(key)
                if last_version is not None and version <= last_version:
                    continue  # stale/duplicate
                state.primary_versions[key] = version

            # Parse and format
            type_name = param['type']
            try:
                _, formatted = parse_value(value, type_name)
            except ValueError:
                # Skip parse failures silently during runtime
                continue

            with state.lock:
                previous = state.current_values.get(path, "")
                if previous == formatted:
                    continue  # no visible value change
                state.current_values[path] = formatted
                emit_change_event(path, type_name, previous, formatted)

        cursor = new_cursor


def _secondary_monitor(
    state: WatchState,
    monitored: List[Tuple[str, Dict[str, Any]]],
    base_url: str,
    poll_interval: float,
) -> None:
    """Monitor secondary-store for changes using batch-read polling."""
    # Build key -> (path, param) mapping
    key_to_param: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for path, param in monitored:
        key = param['secondary-store']
        key_to_param[key] = (path, param)
    keys = list(key_to_param.keys())

    # Track baselines
    baselines: Dict[str, Optional[str]] = {key: None for key in keys}
    first_observation_done: Dict[str, bool] = {key: False for key in keys}

    while not state.stop_event.is_set():
        state.stop_event.wait(timeout=poll_interval)
        if state.stop_event.is_set():
            return

        try:
            items = secondary_store_batch_read(base_url, keys)
        except Exception as e:
            with state.lock:
                state.error = f"Secondary store poll failed: {str(e)}"
            return

        for item in items:
            key = item.get('key')
            status = item.get('status')

            if key not in key_to_param:
                continue

            path, param = key_to_param[key]
            type_name = param['type']

            if status == 'ok':
                value = item.get('value')
                if value is None:
                    continue
                try:
                    _, formatted = parse_value(value, type_name)
                except ValueError:
                    # Parse-failing observations during monitoring are silently skipped
                    continue

                if not first_observation_done.get(key, False):
                    # First successful observation establishes baseline
                    first_observation_done[key] = True
                    baselines[key] = formatted
                    # May emit an event for the initial baseline
                    with state.lock:
                        previous = state.current_values.get(path, "")
                        if previous != formatted:
                            state.current_values[path] = formatted
                            emit_change_event(path, type_name, previous, formatted)
                else:
                    with state.lock:
                        last_baseline = baselines.get(key)
                        if last_baseline is not None and formatted == last_baseline:
                            continue  # No change
                        baselines[key] = formatted
                        previous = state.current_values.get(path, "")
                        if previous == formatted:
                            continue
                        state.current_values[path] = formatted
                        emit_change_event(path, type_name, previous, formatted)
            elif status == 'missing':
                # Key not present in store, skip
                continue
            elif status == 'error':
                # Error status, skip
                continue


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    flags, remaining = parse_global_flags(sys.argv[1:])

    if not remaining:
        print("Error: Missing schema file argument", file=sys.stderr)
        return 1

    schema_file = remaining[0]
    arg_candidates = remaining[1:]

    primary_store_url = flags.get('primary-store')
    secondary_store_url = flags.get('secondary-store')
    watch_mode = '__watch__' in flags

    poll_interval_raw = flags.get('secondary-store-poll-interval')
    poll_interval: Optional[float] = None
    if poll_interval_raw is not None:
        try:
            poll_interval = parse_duration(poll_interval_raw)
        except ValueError as e:
            print(f"Error: Invalid --secondary-store-poll-interval: {e}", file=sys.stderr)
            return 1

    if not os.path.exists(schema_file):
        print(f"Error: Schema file not found: '{schema_file}'", file=sys.stderr)
        return 1

    try:
        with open(schema_file, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON in schema file: {e}", file=sys.stderr)
        return 1
    except (IOError, OSError) as e:
        print(f"Error: Cannot read schema file: {e}", file=sys.stderr)
        return 1

    try:
        validate_schema(schema)
    except ValueError as e:
        print(f"Error: Schema validation failed: {e}", file=sys.stderr)
        return 1

    validate_store_config(
        schema, primary_store_url, secondary_store_url, watch_mode, poll_interval
    )

    parameters = collect_parameters(schema)

    resolved: Dict[str, str] = {}
    unresolved: List[str] = []

    for path, param in parameters:
        formatted_value, parse_error = resolve_parameter(
            path, param, arg_candidates, primary_store_url, secondary_store_url
        )

        if parse_error is not None:
            param_path, source, reason = parse_error
            print(f"Error: Failed to parse parameter '{param_path}' from source '{source}': {reason}", file=sys.stderr)
            return 1

        if formatted_value is not None:
            resolved[path] = formatted_value
        else:
            unresolved.append(path)

    if unresolved:
        unresolved_str = ", ".join(f"'{path}'" for path in unresolved)
        print(f"Error: Unresolved parameters: {unresolved_str}", file=sys.stderr)
        return 1

    if watch_mode:
        return run_watch_mode(
            parameters, resolved, primary_store_url, secondary_store_url, poll_interval
        )
    else:
        output = build_nested_output(resolved)
        print(json.dumps(output))
        return 0


if __name__ == '__main__':
    sys.exit(main())
