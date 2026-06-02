#!/usr/bin/env python3
"""cfgpipe - A command-line configuration resolver with watch mode and change events."""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Callable


# ============================================================================
# Extended Type System
# ============================================================================

class TypedValue:
    """Base for type-wrapped values with JSON serialization."""
    __slots__ = ('_value',)

    def __init__(self, value):
        self._value = value

    def to_json(self):
        return self._value

    def __str__(self):
        return str(self._value)

    def __eq__(self, other):
        if isinstance(other, TypedValue):
            return self._value == other._value
        return self._value == other


class StringValue(TypedValue):
    """String type wrapper for JSON serialization."""
    pass


class IntegerValue(TypedValue):
    """Integer type wrapper for JSON serialization."""
    pass


class FloatValue(TypedValue):
    """Float type wrapper with fixed 6-decimal JSON serialization."""
    __slots__ = ('_value',)

    def to_json(self):
        return round(self._value, 6)

    def __str__(self):
        return f"{self._value:.6f}"


class BooleanValue(TypedValue):
    """Boolean type wrapper for JSON serialization."""
    def __str__(self):
        return 'true' if self._value else 'false'


class DurationValue(TypedValue):
    """Duration type wrapper for normalized time span representation."""
    __slots__ = ('_value',)

    def to_json(self):
        return str(self)

    def __str__(self):
        ts = self._value
        if ts == 0:
            return "0s"
        hours = ts // 3600
        remaining = ts % 3600
        minutes = remaining // 60
        seconds = remaining % 60
        parts = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if seconds:
            parts.append(f"{seconds}s")
        return "".join(parts)

    @classmethod
    def parse(cls, s: str) -> 'DurationValue':
        s = s.strip()
        if not s:
            raise ValueError("empty duration")
        if s == '0s':
            return cls(0)

        total_seconds = 0
        i = 0
        while i < len(s):
            start = i
            while i < len(s) and s[i].isdigit():
                i += 1
            if start == i:
                raise ValueError(f"malformed duration: {s!r}")
            num = int(s[start:i])

            if i >= len(s):
                raise ValueError(f"malformed duration: missing unit in {s!r}")
            unit = s[i]
            if unit not in ('h', 'm', 's'):
                raise ValueError(f"unknown unit in duration: {unit!r}")
            i += 1

            if unit == 'h':
                total_seconds += num * 3600
            elif unit == 'm':
                total_seconds += num * 60
            elif unit == 's':
                total_seconds += num

        return cls(total_seconds) if total_seconds else cls(0)


class PatternValue(TypedValue):
    """Pattern type wrapper with regex validation."""
    __slots__ = ('_value', 'original')

    def __init__(self, pattern: str, original: str):
        self._value = pattern
        self.original = original

    @classmethod
    def parse(cls, s: str) -> 'PatternValue':
        try:
            re.compile(s)
        except re.error as e:
            raise ValueError(f"invalid regex pattern: {s!r}: {e}")
        return cls(s, s)


class MapValue(TypedValue):
    """Map type wrapper for string key-value pairs."""
    __slots__ = ('_value',)

    def __str__(self):
        return ','.join(f"{k}:{v}" for k, v in sorted(self._value.items()))

    @classmethod
    def parse(cls, s: str) -> 'MapValue':
        if not s:
            return cls({})
        data = {}
        for segment in s.split(','):
            if ':' not in segment:
                raise ValueError(f"map entry missing colon: {segment!r}")
            key, value = segment.split(':', 1)
            if not key:
                raise ValueError(f"empty key in map entry: {segment!r}")
            data[key] = value
        return cls(data)


class ListValue(TypedValue):
    """List type wrapper for string sequences."""
    __slots__ = ('_value',)

    def __str__(self):
        return ','.join(self._value)

    @classmethod
    def parse(cls, s: str) -> 'ListValue':
        return cls([] if not s else s.split(','))


class RedactedValue(TypedValue):
    """Redacted type wrapper that hides the actual value."""
    _MASK = '<masked>'
    __slots__ = ('_value',)

    def to_json(self):
        return self._MASK

    def __str__(self):
        return self._MASK


class PortValue(TypedValue):
    """Port type wrapper for custom port validation."""
    __slots__ = ('_value',)

    @classmethod
    def parse(cls, s: str) -> 'PortValue':
        if not s.isdigit():
            raise ValueError(f"port must be a base-10 integer: {s!r}")
        port_num = int(s)
        if port_num < 0 or port_num > 65535:
            raise ValueError(f"port out of range (0-65535): {port_num}")
        return cls(port_num)


# ============================================================================
# Existing Functions
# ============================================================================

def compose_path(parent: str, key: str) -> str:
    """Compose a dot-separated path from parent and key."""
    return f"{parent}.{key}" if parent else key


def parse_cli_args(args: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse CLI arguments in --name=value format.

    Returns (parsed_args, unparsed_args) where parsed_args is a dict of
    name -> value pairs for arguments that match --name=value pattern.
    """
    parsed: dict[str, str] = {}
    unparsed: list[str] = []
    for arg in args:
        if arg.startswith('--') and '=' in arg:
            name, value = arg[2:].split('=', 1)
            parsed[name] = value
        else:
            unparsed.append(arg)
    return parsed, unparsed


def get_env_value(env_name: str) -> str | None:
    return os.environ.get(env_name, "").strip() or None


def get_file_value(file_path: str) -> str | None:
    """Return file content if it exists as a regular file with non-empty content."""
    try:
        if not os.path.isfile(file_path):
            return None
        return open(file_path).read().strip() or None
    except (OSError, IOError):
        return None


def validate_schema_recursive(schema: dict[str, Any], path: str, errors: list[str], pmap: dict[str, list[str]], smap: dict[str, list[str]]) -> None:
    if not isinstance(schema, dict):
        errors.append(f"{'schema root' if not path else path}: must be a JSON object")
        return

    if not schema and not path:
        errors.append("schema root must be non-empty")
        return

    seen = set()
    for key, value in schema.items():
        p = compose_path(path, key)
        if key in seen:
            errors.append(f"{p}: duplicate parameter/group name")
        seen.add(key)

        if not isinstance(value, dict):
            errors.append(f"{p}: must be an object (parameter declaration or group)")
            continue

        is_param = 'type' in value

        for field in ['default', 'env', 'file', 'arg']:
            if field in value and not isinstance(value[field], str):
                errors.append(f"{p}: '{field}' field must be a string")

        if is_param:
            t = value['type']
            if not is_valid_type(t):
                errors.append(f"{p}: unrecognized type '{t}'")

            for store_key, store_map in [('primary-store', pmap), ('secondary-store', smap)]:
                if store_key in value:
                    if not isinstance(value[store_key], str):
                        errors.append(f"{p}: '{store_key}' field must be a string")
                    else:
                        store_map.setdefault(value[store_key], []).append(p)
        else:
            for field in ['default', 'env', 'file', 'arg', 'primary-store', 'secondary-store']:
                if field in value:
                    errors.append(f"{p}: group carries source annotation '{field}'")
            validate_schema_recursive(value, p, errors, pmap, smap)


def is_valid_type(type_name: str) -> bool:
    """Check if type is a recognized built-in or custom type."""
    built_in = {'string', 'integer', 'float', 'boolean', 'duration', 'pattern', 'map', 'list', 'redacted'}
    custom = {'port'}
    return type_name in built_in or type_name in custom


def validate_schema(schema: dict[str, Any]) -> list[str]:
    """Validate schema structure. Return list of error messages (empty if valid)."""
    errors = []
    primary_store_map: dict[str, list[str]] = {}
    secondary_store_map: dict[str, list[str]] = {}

    validate_schema_recursive(schema, "", errors, primary_store_map, secondary_store_map)

    # Check for duplicate primary-store keys across full hierarchy
    for key, params in primary_store_map.items():
        if len(params) > 1:
            errors.append(f"duplicate primary-store key '{key}' used by: {', '.join(params)}")

    # Check for duplicate secondary-store keys across full hierarchy
    for key, params in secondary_store_map.items():
        if len(params) > 1:
            errors.append(f"duplicate secondary-store key '{key}' used by: {', '.join(params)}")

    return errors


def parse_value(value: str, type_name: str) -> Any:
    """Parse a value against a type and return type-wrapped value object.

    Returns type-specific value objects that support to_json() for serialization
    and __str__() for change event string representation.
    """
    if type_name == 'string':
        return StringValue(value)
    if type_name == 'integer':
        if '.' in value:
            raise ValueError("integer type does not accept decimal values")
        int(value)
        return IntegerValue(int(value))
    if type_name == 'float':
        if 'e' in value.lower():
            raise ValueError("scientific notation not allowed")
        f = float(value)
        return FloatValue(f)
    if type_name == 'boolean':
        if value.lower() in {'true', 'yes', 'on', '1', 't', 'y'}:
            return BooleanValue(True)
        if value.lower() in {'false', 'no', 'off', '0', 'f', 'n'}:
            return BooleanValue(False)
        raise ValueError(f"not a valid boolean representation: {value}")
    if type_name == 'duration':
        return DurationValue.parse(value)
    if type_name == 'pattern':
        return PatternValue.parse(value)
    if type_name == 'map':
        return MapValue.parse(value)
    if type_name == 'list':
        return ListValue.parse(value)
    if type_name == 'redacted':
        return RedactedValue(value)
    if type_name == 'port':
        return PortValue.parse(value)
    raise ValueError(f"unknown type: {type_name}")


def _store_lookup(base_url: str, key: str, endpoint: str) -> str | None:
    """Look up a key from a store endpoint.

    Returns value string if found, None if not found.
    Raises RuntimeError for connector failures or malformed responses.
    For endpoint 'primary', handles 404 with found:false; for others, expects 200 always.
    """
    url = f"{base_url}{endpoint}?key={urllib.parse.quote(key, safe='')}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if endpoint == 'primary' and e.code == 404:
            try:
                data = json.loads(e.read().decode('utf-8'))
                if not data.get('found', False):
                    return None
            except (json.JSONDecodeError, ValueError):
                pass
        raise RuntimeError(f"store returned status {e.code}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"store connection failed: {e}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"store response malformed: {e}")

    if not data.get('found', False):
        return None
    return data.get('value')


def primary_store_lookup(base_url: str, key: str) -> str | None:
    """Look up a key in the primary store.

    Returns value string if found, None if not found (404 with found:false).
    Raises RuntimeError for connector failures.
    """
    return _store_lookup(base_url, key, '/v1/primary/kv')


def secondary_store_lookup(base_url: str, key: str) -> str | None:
    """Look up a key in the secondary store.

    Returns value string if found, None if not found.
    Raises RuntimeError for connector failures or malformed responses.
    """
    return _store_lookup(base_url, key, '/v1/secondary/kv')


def resolve_parameter(
    path: str,
    decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
) -> tuple[str | None, str | None]:
    """Resolve parameter with all source support.

    Priority: arg -> secondary-store -> primary-store -> file -> env -> default
    """
    # Check arg sources (highest priority)
    key = decl.get('arg', path)
    val = cli_args.get(key)
    if val is not None:
        return val, 'arg'

    # Check secondary-store
    if secondary_store_url is not None and 'secondary-store' in decl:
        try:
            val = secondary_store_lookup(secondary_store_url, decl['secondary-store'])
            if val is not None:
                return val, 'secondary-store'
        except RuntimeError:
            raise

    # Check primary-store if configured
    if primary_store_url is not None and 'primary-store' in decl:
        try:
            val = primary_store_lookup(primary_store_url, decl['primary-store'])
            if val is not None:
                return val, 'primary-store'
        except RuntimeError:
            raise

    # Check fallback sources
    if 'file' in decl:
        val = get_file_value(decl['file'])
        if val is not None:
            return val, 'file'
    if 'env' in decl:
        val = get_env_value(decl['env'])
        if val is not None:
            return val, 'env'
    if 'default' in decl:
        return decl['default'], 'default'
    return None, None


def resolve_schema(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
    emit_event: Callable | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Resolve schema into configuration value.

    If emit_event is provided, emit seed events for each resolved parameter.
    Returns (result, errors) where result is None if resolution failed.
    Errors contain path, source, and detail information.
    """
    errors = []
    # Track parameter values for seed event emission and output
    param_values: dict[str, Any] = {}

    def resolve_node(node: dict[str, Any], current_path: str) -> dict[str, Any] | None:
        """Recursively resolve a schema node.

        Returns a dict for the resolved subtree, or None if errors occurred.
        """
        output = {}

        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key

            if isinstance(value, dict) and 'type' in value:
                # This is a parameter declaration
                try:
                    value_str, source = resolve_parameter(
                        path, value, cli_args, primary_store_url, secondary_store_url
                    )
                except RuntimeError as e:
                    errors.append({
                        'path': path,
                        'source': 'primary-store' if 'primary' in str(e) else 'secondary-store',
                        'detail': str(e)
                    })
                    return None

                if value_str is None:
                    errors.append({
                        'path': path,
                        'source': None,
                        'detail': 'unresolved parameter: no source provided value'
                    })
                    return None

                try:
                    resolved_val = parse_value(value_str, value['type'])
                    param_values[path] = resolved_val
                    output[key] = resolved_val
                except ValueError as e:
                    errors.append({
                        'path': path,
                        'source': source,
                        'detail': str(e)
                    })
                    return None

            elif isinstance(value, dict):
                # This is a group - recursively resolve
                resolved_group = resolve_node(value, path)
                if resolved_group is None:
                    return None
                output[key] = resolved_group

        return output

    result = resolve_node(schema, "")

    if result is not None and emit_event is not None:
        # Emit seed events for each resolved parameter
        for path, value in param_values.items():
            type_name = _get_param_type(schema, path) or 'string'
            emit_event({
                'path': path,
                'type': type_name,
                'previous': '',
                'current': value
            }, is_seed=True)

    return result, errors


def _get_param_type(node: dict[str, Any], path: str) -> str | None:
    """Get the type of a parameter at the given path from the original schema."""
    parts = path.split('.')
    current = node
    for part in parts:
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    if isinstance(current, dict) and 'type' in current:
        return current['type']
    return None


def check_secondary_store_config(
    schema: dict[str, Any],
    secondary_store_url: str | None,
    watch_mode: bool,
    poll_interval: float | None,
) -> str | None:
    """Validate secondary-store configuration.

    In watch mode with --secondary-store, requires at least one parameter
    declaring secondary-store, and poll_interval must be strictly positive.
    """
    if not watch_mode:
        return None

    if secondary_store_url is None:
        return None  # Not an error if no secondary-store specified

    if poll_interval is not None and poll_interval <= 0:
        return "Error: --secondary-store-poll-interval must be strictly positive"

    # Check that at least one parameter declares secondary-store
    def has_secondary_store(node: dict[str, Any]) -> bool:
        for key, value in node.items():
            if isinstance(value, dict) and 'type' in value:
                if 'secondary-store' in value:
                    return True
            elif isinstance(value, dict):
                if has_secondary_store(value):
                    return True
        return False

    if not has_secondary_store(schema):
        return "Error: no parameters declare secondary-store but --secondary-store is specified"

    return None


def parse_duration(duration_str: str) -> float | None:
    duration_str = duration_str.strip()
    if duration_str.endswith('s'):
        try:
            return float(duration_str[:-1])
        except ValueError:
            return None
    return None


def emit_change_event(path: str, type_name: str, previous: Any, current: Any) -> None:
    """Emit a single change event JSON line to stdout.

    Change events use string representations.
    """
    # Convert to string representation for change events
    prev_str = previous if previous == '' else str(previous)
    curr_str = current if current == '' else str(current)
    event = {
        'path': path,
        'type': type_name,
        'previous': prev_str,
        'current': curr_str
    }
    print(json.dumps(event))


def collect_store_params(schema: dict[str, Any], field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    def collect(node: dict[str, Any], path: str) -> None:
        for key, value in node.items():
            p = compose_path(path, key)
            if isinstance(value, dict):
                if 'type' in value and field in value:
                    result[p] = value[field]
                collect(value, p)
    collect(schema, "")
    return result


def try_update_parameter(
    path: str,
    decl: dict[str, Any],
    current_values: dict[str, str],
    schema: dict[str, Any],
    emit_event: callable,
) -> bool:
    """Try to update a single parameter value from its configured source.

    Returns True if the value changed, False otherwise.
    """
    type_name = decl.get('type', 'string')
    new_value_str = current_values.get(path)
    old_value_str = current_values.get(path, '')

    if new_value_str is None:
        return False

    # Parse the new value according to its type
    try:
        parsed = parse_value(new_value_str, type_name)
    except ValueError:
        # Silently skip parse failures
        return False

    if parsed != old_value_str:
        current_values[path] = parsed
        type_from_schema = _get_param_type(schema, path) or type_name
        emit_event(path, type_from_schema, old_value_str, parsed)
        return True

    return False


def build_output_json(param_values: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Build the output JSON configuration from current parameter values.

    Uses type-native JSON serialization for resolved values.
    """
    output: dict[str, Any] = {}

    def build(node: dict[str, Any], current_path: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if isinstance(value, dict) and 'type' in value:
                # This is a parameter
                if path in param_values:
                    val_obj = param_values[path]
                    if hasattr(val_obj, 'to_json'):
                        result[key] = val_obj.to_json()
                    else:
                        result[key] = val_obj
            elif isinstance(value, dict):
                # This is a group - recursively build
                nested = build(value, path)
                # Only include non-empty groups
                if nested:
                    result[key] = nested
        return result

    return build(schema, "")


def run_watch_mode(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    secondary_store_url: str | None,
    poll_interval: float | None,
) -> int:
    """Run in watch mode with continuous monitoring.

    Emits seed events, then full config JSON, then runtime change events.
    """
    # Track current parameter values (type-native)
    param_values: dict[str, Any] = {}

    # Initial resolution using all configured sources
    errors: list[dict[str, Any]] = []

    def resolve_with_sources() -> dict[str, Any]:
        """Resolve all parameters and return a dict of path -> resolved value."""
        values: dict[str, Any] = {}

        def resolve_node(node: dict[str, Any], current_path: str) -> None:
            for key, value in node.items():
                path = compose_path(current_path, key) if current_path else key
                if isinstance(value, dict) and 'type' in value:
                    try:
                        val_str, source = resolve_parameter(
                            path, value, cli_args, primary_store_url, secondary_store_url
                        )
                        if val_str is not None:
                            type_name = value['type']
                            parsed = parse_value(val_str, type_name)
                            values[path] = parsed
                    except RuntimeError as e:
                        raise  # Re-raise to be caught by caller
                    except ValueError:
                        # Silently skip parse failures in seed resolution
                        pass
                elif isinstance(value, dict):
                    resolve_node(value, path)

        resolve_node(schema, "")
        return values

    # Initial resolution (seed lookup)
    try:
        param_values = resolve_with_sources()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Emit seed events for each resolved parameter
    # Seed events have previous="" and current as the resolved value
    def get_type_for_path(path: str) -> str:
        return _get_param_type(schema, path) or 'string'

    for path, value in param_values.items():
        type_name = get_type_for_path(path)
        emit_change_event(path, type_name, '', value)

    # Emit the full resolved configuration as JSON
    output = build_output_json(param_values, schema)
    print(json.dumps(output))

    # Check for unresolved parameters
    def check_unresolved(node: dict[str, Any], current_path: str) -> bool:
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if isinstance(value, dict) and 'type' in value:
                if path not in param_values:
                    errors.append({
                        'path': path,
                        'source': None,
                        'detail': 'unresolved parameter: no source provided value'
                    })
                    return False
            elif isinstance(value, dict):
                if not check_unresolved(value, path):
                    return False
        return True

    errors.clear()
    if not check_unresolved(schema, ""):
        for err in errors:
            path = err['path']
            source = err.get('source')
            detail = err['detail']
            if source:
                print(f"Error: '{path}' from source '{source}': {detail}", file=sys.stderr)
            else:
                print(f"Error: {path}: {detail}", file=sys.stderr)
        return 1

    # Set up primary-store watch monitoring
    primary_params = collect_store_params(schema, 'primary-store')
    primary_versions: dict[str, int] = {path: 0 for path in primary_params}

    # Set up secondary-store parameters
    secondary_params = collect_store_params(schema, 'secondary-store')
    secondary_last_values: dict[str, Any] = {}

    if primary_params:
        # Get initial values from primary store for seed
        if primary_store_url:
            for path, pkey in primary_params.items():
                try:
                    val = primary_store_lookup(primary_store_url, pkey)
                except RuntimeError:
                    print(f"Error: failed to seed primary-store key {pkey}", file=sys.stderr)
                    return 1

                if val is None:
                    continue
                try:
                    type_name = _get_param_type(schema, path) or 'string'
                    parsed = parse_value(val, type_name)
                    if parsed != param_values.get(path):
                        param_values[path] = parsed
                        emit_change_event(path, type_name, param_values.get(path, ''), parsed)
                except (ValueError, KeyError):
                    # Skip if type lookup or parse fails
                    pass

    # Watch mode: Monitor for changes
    while True:
        if primary_params and primary_store_url:
            # Monitor primary store for changes
            primary_key_to_paths: dict[str, list[str]] = {}
            for path, pkey in primary_params.items():
                primary_key_to_paths.setdefault(pkey, []).append(path)

            # We need to track version per key
            key_versions: dict[str, int] = {k: 0 for k in primary_key_to_paths}

            # Watch loop for primary store
            while True:
                for pkey, tracked_paths in primary_key_to_paths.items():
                    try:
                        url = f"{primary_store_url}/v1/primary/watch?cursor={key_versions[pkey]}&key={urllib.parse.quote(pkey, safe='')}"
                        req = urllib.request.Request(url)
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            data = json.loads(resp.read().decode('utf-8'))
                    except urllib.error.HTTPError as e:
                        print(f"Error: primary store watch failed: {e.code}", file=sys.stderr)
                        return 1
                    except (urllib.error.URLError, json.JSONDecodeError) as e:
                        print(f"Error: primary store connection failed: {e}", file=sys.stderr)
                        return 1

                    if data is None:
                        continue

                    new_cursor = data.get('cursor', key_versions[pkey])
                    events = data.get('events', [])

                    for event in events:
                        key = event.get('key', '')
                        value = event.get('value', '')
                        version = event.get('version', 0)

                        # Only process if this is for our tracked key
                        if key != pkey:
                            continue
                        if version <= key_versions[pkey]:
                            # Stale or duplicate - skip
                            continue

                        # Update the cursor to the latest version
                        key_versions[pkey] = version

                        # Apply the update to all tracked parameters
                        for param_path in tracked_paths:
                            if param_path not in schema:
                                continue

                            # Navigate to the parameter declaration
                            parts = param_path.split('.')
                            current = schema
                            for part in parts[:-1]:
                                if part in current and isinstance(current[part], dict):
                                    current = current[part]
                                else:
                                    current = {}
                                    break
                            if not isinstance(current, dict) or parts[-1] not in current:
                                continue

                            decl = current[parts[-1]]
                            if not isinstance(decl, dict) or 'type' not in decl:
                                continue

                            type_name = decl['type']
                            old_value = param_values.get(param_path, '')

                            try:
                                new_value = parse_value(value, type_name)
                            except ValueError:
                                # Skip unparseable values
                                continue

                            if new_value != old_value:
                                param_values[param_path] = new_value
                                emit_change_event(param_path, type_name, old_value, new_value)

                    key_versions[pkey] = new_cursor

                # Secondary-store polling
                if secondary_store_url and poll_interval is not None:
                    try:
                        url = f"{secondary_store_url}/v1/secondary/batch-read"
                        keys = list(secondary_params.values())
                        body = json.dumps({'keys': keys}).encode('utf-8')
                        req = urllib.request.Request(url, data=body, method='POST')
                        req.add_header('Content-Type', 'application/json')
                        with urllib.request.urlopen(req, timeout=30) as resp:
                            data = json.loads(resp.read().decode('utf-8'))
                    except (urllib.error.URLError, json.JSONDecodeError) as e:
                        print(f"Error: secondary store poll failed: {e}", file=sys.stderr)
                        return 1

                    if data and 'items' in data:
                        for item in data.get('items', []):
                            key = item.get('key', '')
                            status = item.get('status', '')
                            value = item.get('value', '')

                            # Find the path for this key
                            path = None
                            for p, k in secondary_params.items():
                                if k == key:
                                    path = p
                                    break

                            if path is None:
                                continue

                            if status == 'ok':
                                # Parse the value according to its type
                                type_name = _get_param_type(schema, path) or 'string'
                                try:
                                    parsed = parse_value(value, type_name)
                                except ValueError:
                                    # Silently skip parse failures
                                    continue

                                old_value = secondary_last_values.get(path, '')
                                if parsed != old_value:
                                    secondary_last_values[path] = parsed
                                    # Check if different from current value
                                    current_val = param_values.get(path, '')
                                    if parsed != current_val:
                                        param_values[path] = parsed
                                        emit_change_event(path, type_name, current_val, parsed)
                                    else:
                                        # Value matches current, emit baseline event
                                        emit_change_event(path, type_name, '', parsed)

                            # For 'missing' and 'error' statuses, silently skip

                    # Wait for the poll interval
                    time.sleep(poll_interval)
    return 0


def main() -> int:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        return 1

    # First, find the schema file position
    schema_path = None
    schema_idx = None
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        # Handle flag arguments that take a value
        if arg in ('--primary-store', '--secondary-store'):
            if i + 1 < len(sys.argv):
                i += 2
            else:
                i += 1
        elif arg == '--secondary-store-poll-interval':
            if i + 1 < len(sys.argv):
                i += 2
            else:
                i += 1
        elif not arg.startswith('-'):
            schema_path = arg
            schema_idx = i
            break
        else:
            i += 1

    if schema_path is None:
        print("Error: no schema file specified", file=sys.stderr)
        return 1

    # Separate global flags from arg candidates
    global_args = sys.argv[1:schema_idx]
    arg_candidates = sys.argv[schema_idx + 1:]

    # Parse global flags
    primary_store_url = None
    secondary_store_url = None
    secondary_store_poll_interval: float | None = None
    watch_mode = False

    g = iter(global_args)
    for arg in g:
        if arg == '--primary-store':
            try:
                primary_store_url = next(g)
            except StopIteration:
                print("Error: --primary-store requires a URL argument", file=sys.stderr)
                return 1
        elif arg == '--secondary-store':
            try:
                secondary_store_url = next(g)
            except StopIteration:
                print("Error: --secondary-store requires a URL argument", file=sys.stderr)
                return 1
        elif arg == '--secondary-store-poll-interval':
            try:
                interval_str = next(g)
                interval = parse_duration(interval_str)
                if interval is None:
                    print(f"Error: invalid duration format: {interval_str}", file=sys.stderr)
                    return 1
                secondary_store_poll_interval = interval
            except StopIteration:
                print("Error: --secondary-store-poll-interval requires a duration argument", file=sys.stderr)
                return 1
        elif arg == '--watch':
            watch_mode = True

    # Parse arg candidates for CLI args
    cli_args, _ = parse_cli_args(arg_candidates)

    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except OSError as e:
        print(f"Error reading schema file: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON schema: {e}", file=sys.stderr)
        return 1

    # Schema validation (before resolution)
    validation_errors = validate_schema(schema)
    if validation_errors:
        for err in validation_errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    # Check secondary-store config
    err = check_secondary_store_config(schema, secondary_store_url, watch_mode, secondary_store_poll_interval)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    if watch_mode:
        return run_watch_mode(
            schema,
            cli_args,
            primary_store_url,
            secondary_store_url,
            secondary_store_poll_interval
        )
    else:
        # Standard mode - resolve and print
        resolved, errors = resolve_schema(schema, cli_args, primary_store_url, secondary_store_url)

        if errors:
            for err in errors:
                path = err['path']
                source = err.get('source')
                detail = err['detail']
                if source:
                    print(f"Error: '{path}' from source '{source}': {detail}", file=sys.stderr)
                else:
                    print(f"Error: {path}: {detail}", file=sys.stderr)
            return 1

        print(json.dumps(resolved))
        return 0


if __name__ == '__main__':
    sys.exit(main())
