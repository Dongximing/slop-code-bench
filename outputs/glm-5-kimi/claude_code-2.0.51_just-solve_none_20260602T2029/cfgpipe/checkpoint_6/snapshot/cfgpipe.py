#!/usr/bin/env python3
"""
cfgpipe - Extended Type System

A command-line configuration resolver that reads a JSON schema document,
resolves each declared parameter from local sources, and writes the resolved
configuration to stdout as JSON with type-native serialization.

Watch mode monitors primary-store and secondary-store for runtime updates
and emits change events using string representations.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any, Optional, Dict, List

import requests


def parse_boolean(value: str) -> bool:
    lower = value.lower().strip()
    if lower in ('true', 'yes', '1', 'on', 'y'):
        return True
    if lower in ('false', 'no', '0', 'off', 'n'):
        return False
    raise ValueError(f"Cannot parse '{value}' as boolean")


def parse_integer(value: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as integer")
    check_str = stripped.lstrip('+-')
    if not check_str or not check_str.isdigit():
        raise ValueError(f"Cannot parse '{value}' as integer")
    return int(stripped)


def parse_port(value: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as port")
    if len(stripped) > 1 and stripped[0] == '0':
        raise ValueError(f"Cannot parse '{value}' as port: invalid leading zeros")
    if not stripped.isdigit():
        raise ValueError(f"Cannot parse '{value}' as port: not a valid integer")
    port_num = int(stripped)
    if not 0 <= port_num <= 65535:
        raise ValueError(f"Port {port_num} out of valid range 0-65535")
    return port_num


def parse_float(value: str) -> float:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Cannot parse '{value}' as float")
    try:
        return float(stripped)
    except ValueError:
        raise ValueError(f"Cannot parse '{value}' as float")


def parse_duration(value: str) -> int:
    if not value:
        raise ValueError(f"Cannot parse empty string as duration")

    total_seconds = 0
    i = 0
    n = len(value)

    while i < n:
        if not value[i].isdigit():
            raise ValueError(f"Cannot parse '{value}' as duration: expected digit at position {i}")

        j = i
        while j < n and value[j].isdigit():
            j += 1

        if j >= n:
            raise ValueError(f"Cannot parse '{value}' as duration: missing unit after number")

        num_str = value[i:j]
        num = int(num_str)

        unit = value[j]
        if unit == 'h':
            total_seconds += num * 3600
        elif unit == 'm':
            total_seconds += num * 60
        elif unit == 's':
            total_seconds += num
        else:
            raise ValueError(f"Cannot parse '{value}' as duration: unknown unit '{unit}'")

        i = j + 1

    if total_seconds < 0:
        raise ValueError(f"Cannot parse '{value}' as duration: negative duration not allowed")

    return total_seconds


def format_duration(total_seconds: int) -> str:
    if isinstance(total_seconds, float):
        total_seconds = int(total_seconds)

    if total_seconds == 0:
        return '0s'

    parts = []

    hours = total_seconds // 3600
    if hours > 0:
        parts.append(f"{hours}h")
        total_seconds -= hours * 3600

    minutes = total_seconds // 60
    if minutes > 0:
        parts.append(f"{minutes}m")
        total_seconds -= minutes * 60

    if total_seconds > 0:
        parts.append(f"{total_seconds}s")

    return ''.join(parts)


def parse_pattern(value: str) -> str:
    try:
        re.compile(value)
        return value
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")


def parse_map(value: str) -> Dict[str, str]:
    if not value:
        return {}

    result = {}
    segments = value.split(',')

    for segment in segments:
        if ':' not in segment:
            raise ValueError(f"Cannot parse '{segment}' as map entry: missing colon")
        colon_idx = segment.index(':')
        key = segment[:colon_idx]
        val = segment[colon_idx + 1:]
        result[key] = val

    return result


def format_map(map_value: Dict[str, str]) -> str:
    if not map_value:
        return ''

    sorted_keys = sorted(map_value.keys())
    pairs = [f"{k}:{map_value[k]}" for k in sorted_keys]
    return ','.join(pairs)


def parse_list(value: str) -> List[str]:
    if not value:
        return []

    return value.split(',')


def format_list(list_value: List[str]) -> str:
    return ','.join(list_value)


REDACTED_MASK = "<masked>"


def format_value_for_json(value: Any, type_name: str) -> Any:
    if type_name == 'string':
        return value
    if type_name == 'integer':
        return value
    if type_name == 'float':
        formatted = f"{value:.6f}"
        if '.' in formatted:
            formatted = formatted.rstrip('0')
            if formatted.endswith('.'):
                formatted += '0'
        return _FloatStr(formatted)
    if type_name == 'boolean':
        return value
    if type_name == 'port':
        return str(value)
    if type_name == 'duration':
        return format_duration(value)
    if type_name == 'pattern':
        return value
    if type_name == 'map':
        return value
    if type_name == 'list':
        return value
    if type_name == 'redacted':
        return REDACTED_MASK
    return value


class _FloatStr(str):
    """Marker class for float strings that should serialize as JSON numbers."""
    pass


def _build_json(obj) -> str:
    if isinstance(obj, _FloatStr):
        return str(obj)
    elif isinstance(obj, float):
        formatted = f"{obj:.6f}"
        formatted = formatted.rstrip('0')
        if formatted.endswith('.'):
            formatted += '0'
        return formatted
    elif isinstance(obj, dict):
        pairs = [f'"{k}":{_build_json(v)}' for k, v in obj.items()]
        return '{' + ','.join(pairs) + '}'
    elif isinstance(obj, list):
        items = [_build_json(v) for v in obj]
        return '[' + ','.join(items) + ']'
    elif isinstance(obj, bool):
        return 'true' if obj else 'false'
    elif isinstance(obj, str):
        return json.dumps(obj)
    elif isinstance(obj, int):
        return str(obj)
    elif obj is None:
        return 'null'
    else:
        return json.dumps(obj)


def format_value_for_event(value: Any, type_name: str) -> str:
    if type_name == 'string':
        return value
    if type_name == 'integer':
        return str(value)
    if type_name == 'float':
        return f"{value:.6f}"
    if type_name == 'boolean':
        return 'true' if value else 'false'
    if type_name == 'port':
        return str(value)
    if type_name == 'duration':
        return format_duration(value)
    if type_name == 'pattern':
        return value
    if type_name == 'map':
        return format_map(value)
    if type_name == 'list':
        return format_list(value)
    if type_name == 'redacted':
        return REDACTED_MASK
    return str(value)


def parse_value(value: str, type_name: str) -> Any:
    if type_name == 'string':
        return value
    if type_name == 'integer':
        return parse_integer(value)
    if type_name == 'float':
        return parse_float(value)
    if type_name == 'boolean':
        return parse_boolean(value)
    if type_name == 'port':
        return parse_port(value)
    if type_name == 'duration':
        return parse_duration(value)
    if type_name == 'pattern':
        return parse_pattern(value)
    if type_name == 'map':
        return parse_map(value)
    if type_name == 'list':
        return parse_list(value)
    if type_name == 'redacted':
        return value
    raise ValueError(f"Unknown type: {type_name}")


ALL_VALID_TYPES = ('string', 'integer', 'float', 'boolean', 'port', 'duration', 'pattern', 'map', 'list', 'redacted')

SOURCE_ANNOTATIONS = ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store')


def load_schema(schema_path: str) -> dict:
    if not os.path.isfile(schema_path):
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        with open(schema_path, 'r') as f:
            schema = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in schema file: {e}")

    return schema


def validate_schema_recursive(
    node: Any,
    path: str,
    primary_store_map: dict,
    secondary_store_map: dict
) -> list:
    errors = []

    if not isinstance(node, dict):
        errors.append(f"Schema node at '{path}' must be an object")
        return errors

    if 'type' in node and isinstance(node['type'], str):
        type_name = node['type']

        if type_name not in ALL_VALID_TYPES:
            errors.append(f"Parameter '{path}' has unrecognized type: {type_name}")
            return errors

        for source in SOURCE_ANNOTATIONS:
            if source in node and not isinstance(node[source], str):
                errors.append(f"Parameter '{path}' source '{source}' must be a string")

        if 'primary-store' in node:
            key = node['primary-store']
            if key not in primary_store_map:
                primary_store_map[key] = []
            primary_store_map[key].append(path)

        if 'secondary-store' in node:
            key = node['secondary-store']
            if key not in secondary_store_map:
                secondary_store_map[key] = []
            secondary_store_map[key].append(path)
    else:
        for key, value in node.items():
            child_path = f"{path}.{key}" if path else key

            if key in SOURCE_ANNOTATIONS:
                if not isinstance(value, dict):
                    errors.append(f"Group '{path}' has invalid annotation '{key}'")
                    continue

            if not isinstance(value, dict):
                errors.append(f"Entry '{child_path}' must be an object")
                continue

            child_errors = validate_schema_recursive(value, child_path, primary_store_map, secondary_store_map)
            errors.extend(child_errors)

    return errors


def validate_schema(schema: dict) -> None:
    if not isinstance(schema, dict):
        raise ValueError("Schema root must be an object")

    if len(schema) == 0:
        raise ValueError("Schema root must be a non-empty object")

    primary_store_map = {}
    secondary_store_map = {}

    errors = validate_schema_recursive(schema, "", primary_store_map, secondary_store_map)

    if errors:
        raise ValueError(errors[0])

    duplicate_errors = []
    for key, paths in primary_store_map.items():
        if len(paths) > 1:
            duplicate_errors.append((key, sorted(paths)))

    if duplicate_errors:
        key, paths = duplicate_errors[0]
        paths_str = ', '.join(paths)
        raise ValueError(f"Duplicate primary-store key '{key}' used by parameters: {paths_str}")

    duplicate_errors = []
    for key, paths in secondary_store_map.items():
        if len(paths) > 1:
            duplicate_errors.append((key, sorted(paths)))

    if duplicate_errors:
        key, paths = duplicate_errors[0]
        paths_str = ', '.join(paths)
        raise ValueError(f"Duplicate secondary-store key '{key}' used by parameters: {paths_str}")


def collect_parameters(schema: dict, path: str = "") -> list:
    params = []

    for key, value in schema.items():
        child_path = f"{path}.{key}" if path else key

        if 'type' in value and isinstance(value['type'], str):
            params.append((child_path, value))
        else:
            params.extend(collect_parameters(value, child_path))

    return params


def parse_poll_interval(duration_str: str) -> float:
    """Parse a duration string for poll interval (supports ms, s, m, h)."""
    duration_str = duration_str.strip()
    if not duration_str:
        raise ValueError("Duration cannot be empty")

    if duration_str.endswith('ms'):
        value = duration_str[:-2]
        return float(value) / 1000.0
    elif duration_str.endswith('s'):
        value = duration_str[:-1]
        return float(value)
    elif duration_str.endswith('m'):
        value = duration_str[:-1]
        return float(value) * 60.0
    elif duration_str.endswith('h'):
        value = duration_str[:-1]
        return float(value) * 3600.0
    else:
        return float(duration_str)


def parse_global_flags(args: list) -> tuple[dict, list]:
    global_flags = {}
    remaining_args = []
    i = 0

    while i < len(args):
        arg = args[i]
        if arg == '--primary-store':
            if i + 1 >= len(args):
                raise ValueError("--primary-store requires a value")
            global_flags['primary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--primary-store='):
            global_flags['primary-store'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--primary-store-prefix':
            if i + 1 >= len(args):
                raise ValueError("--primary-store-prefix requires a value")
            global_flags['primary-store-prefix'] = args[i + 1]
            i += 2
        elif arg.startswith('--primary-store-prefix='):
            global_flags['primary-store-prefix'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store':
            if i + 1 >= len(args):
                raise ValueError("--secondary-store requires a value")
            global_flags['secondary-store'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store='):
            global_flags['secondary-store'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store-key-prefix':
            if i + 1 >= len(args):
                raise ValueError("--secondary-store-key-prefix requires a value")
            global_flags['secondary-store-key-prefix'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store-key-prefix='):
            global_flags['secondary-store-key-prefix'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store-key-separator':
            if i + 1 >= len(args):
                raise ValueError("--secondary-store-key-separator requires a value")
            global_flags['secondary-store-key-separator'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store-key-separator='):
            global_flags['secondary-store-key-separator'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--secondary-store-poll-interval':
            if i + 1 >= len(args):
                raise ValueError("--secondary-store-poll-interval requires a value")
            global_flags['secondary-store-poll-interval'] = args[i + 1]
            i += 2
        elif arg.startswith('--secondary-store-poll-interval='):
            global_flags['secondary-store-poll-interval'] = arg.split('=', 1)[1]
            i += 1
        elif arg == '--watch':
            global_flags['watch'] = True
            i += 1
        else:
            remaining_args.append(arg)
            i += 1

    return global_flags, remaining_args


def compose_primary_store_key(declared_key: str, prefix: Optional[str]) -> str:
    """
    Compose the actual primary-store key from declared key and optional prefix.

    - Strip leading and trailing `/` from the prefix
    - Strip leading `/` from the declared key
    - If remaining prefix is empty, use remaining key with no separator
    - Otherwise compose as `<prefix>/<key>` with fixed `/` separator
    """
    if not prefix:
        return declared_key

    # Strip leading and trailing / from prefix
    stripped_prefix = prefix.strip('/')

    # Strip leading / from declared key
    stripped_key = declared_key.lstrip('/')

    if not stripped_prefix:
        return stripped_key

    return f"{stripped_prefix}/{stripped_key}"


def compose_secondary_store_key(declared_key: str, prefix: Optional[str], separator: Optional[str]) -> str:
    """
    Compose the actual secondary-store key from declared key and optional prefix/separator.

    - If prefix is empty or not configured, use key as-is
    - Separator is used exactly as supplied (empty separator means direct concatenation)
    """
    if not prefix:
        return declared_key

    sep = separator if separator is not None else ''
    return f"{prefix}{sep}{declared_key}"


def parse_cli_args(arg_candidates: list) -> dict:
    result = {}
    for arg in arg_candidates:
        if '=' in arg:
            if arg.startswith('--'):
                key, value = arg[2:].split('=', 1)
                result[key] = value
            elif arg.startswith('-'):
                key, value = arg[1:].split('=', 1)
                result[key] = value
    return result


def lookup_primary_store(base_url: str, key: str) -> tuple[Optional[str], Optional[int], Optional[str]]:
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            try:
                data = response.json()
                if data.get('found') is True:
                    return (data.get('value'), data.get('version'), None)
                return (None, None, None)
            except (json.JSONDecodeError, ValueError) as e:
                return (None, None, f"Malformed response from primary store: {e}")
        elif response.status_code == 404:
            try:
                data = response.json()
                if data.get('found') is False:
                    return (None, None, None)
                return (None, None, f"Unexpected 404 response from primary store")
            except (json.JSONDecodeError, ValueError):
                return (None, None, f"Malformed 404 response from primary store")
        else:
            return (None, None, f"Primary store returned status {response.status_code}")
    except requests.exceptions.Timeout:
        return (None, None, "Primary store request timed out")
    except requests.exceptions.ConnectionError as e:
        return (None, None, f"Failed to connect to primary store: {e}")
    except requests.exceptions.RequestException as e:
        return (None, None, f"Primary store request failed: {e}")


def lookup_secondary_store(base_url: str, key: str) -> tuple[Optional[str], Optional[str]]:
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{base_url.rstrip('/')}/v1/secondary/kv?key={encoded_key}"

    try:
        response = requests.get(url, timeout=30)

        if response.status_code == 200:
            try:
                data = response.json()
                if data.get('found') is True:
                    return (data.get('value'), None)
                elif data.get('found') is False:
                    return (None, None)
                return (None, f"Malformed response from secondary store: missing 'found' field")
            except (json.JSONDecodeError, ValueError) as e:
                return (None, f"Malformed response from secondary store: {e}")
        else:
            return (None, f"Secondary store returned status {response.status_code}")
    except requests.exceptions.Timeout:
        return (None, "Secondary store request timed out")
    except requests.exceptions.ConnectionError as e:
        return (None, f"Failed to connect to secondary store: {e}")
    except requests.exceptions.RequestException as e:
        return (None, f"Secondary store request failed: {e}")


def resolve_from_sources(
    param_path: str,
    param_decl: dict,
    cli_args: dict,
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    primary_versions: Optional[dict] = None,
    primary_store_prefix: Optional[str] = None,
    secondary_store_key_prefix: Optional[str] = None,
    secondary_store_key_separator: Optional[str] = None
) -> tuple[bool, Any, str, str]:
    type_name = param_decl['type']

    if 'arg' in param_decl:
        arg_name = param_decl['arg']
        if arg_name in cli_args:
            arg_val = cli_args[arg_name]
            try:
                parsed = parse_value(arg_val, type_name)
                return (True, parsed, 'arg', arg_val)
            except ValueError as e:
                return (False, None, 'arg', str(e))

    if 'secondary-store' in param_decl and secondary_store_url:
        declared_key = param_decl['secondary-store']
        composed_key = compose_secondary_store_key(declared_key, secondary_store_key_prefix, secondary_store_key_separator)
        value, error = lookup_secondary_store(secondary_store_url, composed_key)

        if error is not None:
            return (False, None, 'secondary-store', error)

        if value is not None:
            try:
                parsed = parse_value(value, type_name)
                return (True, parsed, 'secondary-store', value)
            except ValueError as e:
                return (False, None, 'secondary-store', str(e))

    if 'primary-store' in param_decl and primary_store_url:
        declared_key = param_decl['primary-store']
        composed_key = compose_primary_store_key(declared_key, primary_store_prefix)
        value, version, error = lookup_primary_store(primary_store_url, composed_key)

        if error is not None:
            return (False, None, 'primary-store', error)

        if value is not None:
            try:
                parsed = parse_value(value, type_name)
                if primary_versions is not None and version is not None:
                    primary_versions[declared_key] = version
                return (True, parsed, 'primary-store', value)
            except ValueError as e:
                return (False, None, 'primary-store', str(e))

    if 'file' in param_decl:
        file_path = param_decl['file']
        if os.path.isfile(file_path):
            try:
                with open(file_path, 'r') as f:
                    content = f.read()
                content = content.strip()
                if content:
                    try:
                        parsed = parse_value(content, type_name)
                        return (True, parsed, 'file', content)
                    except ValueError as e:
                        return (False, None, 'file', str(e))
            except (IOError, OSError):
                pass

    if 'env' in param_decl:
        env_var = param_decl['env']
        if env_var in os.environ:
            env_val = os.environ[env_var]
            try:
                parsed = parse_value(env_val, type_name)
                return (True, parsed, 'env', env_val)
            except ValueError as e:
                return (False, None, 'env', str(e))

    if 'default' in param_decl:
        default_val = param_decl['default']
        try:
            parsed = parse_value(default_val, type_name)
            return (True, parsed, 'default', default_val)
        except ValueError as e:
            return (False, None, 'default', str(e))

    return (False, None, None, None)


def build_output_structure_with_path(resolved_params: dict, path_prefix: str, schema: dict) -> dict:
    result = {}

    for key, value in schema.items():
        child_path = f"{path_prefix}.{key}" if path_prefix else key

        if 'type' in value and isinstance(value['type'], str):
            result[key] = resolved_params[child_path]
        else:
            result[key] = build_output_structure_with_path(resolved_params, child_path, value)

    return result


def emit_change_event(path: str, type_name: str, previous: str, current: str):
    event = {
        "path": path,
        "type": type_name,
        "previous": previous,
        "current": current
    }
    print(json.dumps(event), flush=True)


def watch_primary_store_with_prefix(
    base_url: str,
    monitored_keys: dict,
    current_values: dict,
    versions: dict,
    cursor: int
) -> tuple[int, bool, str]:
    """
    Watch primary store with composed key support.

    monitored_keys: maps composed_key -> (param_path, type_name, declared_key)
    versions: maps declared_key -> version

    Runtime events arrive with composed keys; we map them back to declared keys
    for version tracking, but emit events with schema-declared param_path.
    """
    if not monitored_keys:
        return (cursor, True, None)

    # Watch API expects the composed keys
    keys_list = list(monitored_keys.keys())
    encoded_keys = [urllib.parse.quote(k, safe='') for k in keys_list]
    keys_param = '&key='.join(encoded_keys)
    url = f"{base_url.rstrip('/')}/v1/primary/watch?cursor={cursor}&key={keys_param}"

    try:
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            return (cursor, False, f"Primary store watch returned status {response.status_code}")

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            return (cursor, False, f"Malformed response from primary store watch: {e}")

        new_cursor = data.get('cursor', cursor)
        events = data.get('events', [])

        for event in events:
            composed_key = event.get('key')
            value = event.get('value')
            version = event.get('version')

            if composed_key not in monitored_keys:
                # Ignore updates that don't correspond to any monitored parameter
                continue

            param_path, type_name, declared_key = monitored_keys[composed_key]

            # Version tracking uses declared_key
            if declared_key in versions:
                if version <= versions[declared_key]:
                    continue

            versions[declared_key] = version

            try:
                parsed = parse_value(value, type_name)
                formatted = format_value_for_event(parsed, type_name)
            except ValueError:
                continue

            previous = current_values.get(param_path, "")

            if formatted != previous:
                emit_change_event(param_path, type_name, previous, formatted)
                current_values[param_path] = formatted

        return (new_cursor, True, None)

    except requests.exceptions.Timeout:
        return (cursor, False, "Primary store watch request timed out")
    except requests.exceptions.ConnectionError as e:
        return (cursor, False, f"Failed to connect to primary store: {e}")
    except requests.exceptions.RequestException as e:
        return (cursor, False, f"Primary store watch request failed: {e}")


def watch_primary_store(
    base_url: str,
    monitored_keys: dict,
    current_values: dict,
    versions: dict,
    cursor: int
) -> tuple[int, bool, str]:
    if not monitored_keys:
        return (cursor, True, None)

    keys_list = list(monitored_keys.keys())
    encoded_keys = [urllib.parse.quote(k, safe='') for k in keys_list]
    keys_param = '&key='.join(encoded_keys)
    url = f"{base_url.rstrip('/')}/v1/primary/watch?cursor={cursor}&key={keys_param}"

    try:
        response = requests.get(url, timeout=30)

        if response.status_code != 200:
            return (cursor, False, f"Primary store watch returned status {response.status_code}")

        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            return (cursor, False, f"Malformed response from primary store watch: {e}")

        new_cursor = data.get('cursor', cursor)
        events = data.get('events', [])

        for event in events:
            key = event.get('key')
            value = event.get('value')
            version = event.get('version')

            if key not in monitored_keys:
                continue

            param_path, type_name = monitored_keys[key]

            if key in versions:
                if version <= versions[key]:
                    continue

            versions[key] = version

            try:
                parsed = parse_value(value, type_name)
                formatted = format_value_for_event(parsed, type_name)
            except ValueError:
                continue

            previous = current_values.get(param_path, "")

            if formatted != previous:
                emit_change_event(param_path, type_name, previous, formatted)
                current_values[param_path] = formatted

        return (new_cursor, True, None)

    except requests.exceptions.Timeout:
        return (cursor, False, "Primary store watch request timed out")
    except requests.exceptions.ConnectionError as e:
        return (cursor, False, f"Failed to connect to primary store: {e}")
    except requests.exceptions.RequestException as e:
        return (cursor, False, f"Primary store watch request failed: {e}")


def batch_read_secondary_store(base_url: str, keys: list) -> tuple[Optional[list], Optional[str]]:
    url = f"{base_url.rstrip('/')}/v1/secondary/batch-read"

    try:
        response = requests.post(
            url,
            json={"keys": keys},
            timeout=30
        )

        if response.status_code != 200:
            return (None, f"Secondary store batch-read returned status {response.status_code}")

        try:
            data = response.json()
            return (data.get('items', []), None)
        except (json.JSONDecodeError, ValueError) as e:
            return (None, f"Malformed response from secondary store batch-read: {e}")

    except requests.exceptions.Timeout:
        return (None, "Secondary store batch-read request timed out")
    except requests.exceptions.ConnectionError as e:
        return (None, f"Failed to connect to secondary store: {e}")
    except requests.exceptions.RequestException as e:
        return (None, f"Secondary store batch-read request failed: {e}")


def poll_secondary_store(base_url: str, monitored_keys: dict, current_values: dict, baselines: dict, last_successful: dict) -> bool:
    """
    Poll secondary store with resilient per-key failure handling.

    monitored_keys: maps composed_key -> (param_path, type_name)
    last_successful: maps composed_key -> last successful formatted value (or None if never succeeded)

    Returns True always (does not exit on errors). Per-key failures are isolated.
    """
    if not monitored_keys:
        return True

    keys_list = list(monitored_keys.keys())
    items, error = batch_read_secondary_store(base_url, keys_list)

    if error:
        # Whole batch failed - skip this cycle but continue monitoring
        return True

    for item in items:
        composed_key = item.get('key')
        status = item.get('status')

        if composed_key not in monitored_keys:
            continue

        param_path, type_name = monitored_keys[composed_key]

        if status == 'ok':
            value = item.get('value')
            if value is None:
                continue

            # Try to parse the value
            try:
                parsed = parse_value(value, type_name)
                formatted = format_value_for_event(parsed, type_name)
            except ValueError:
                # Parse failure - silently skip during monitoring
                # But preserve last successful value
                continue

            # Check if this is first successful read, or differs from last successful
            last_ok = last_successful.get(composed_key)
            if last_ok is None:
                # First successful read
                last_successful[composed_key] = formatted
                previous = current_values.get(param_path, "")
                if formatted != previous:
                    emit_change_event(param_path, type_name, previous, formatted)
                    current_values[param_path] = formatted
            elif formatted != last_ok:
                # Changed from last successful
                last_successful[composed_key] = formatted
                previous = current_values.get(param_path, "")
                if formatted != previous:
                    emit_change_event(param_path, type_name, previous, formatted)
                    current_values[param_path] = formatted
        elif status == 'error':
            # Per-key operational failure - skip this key, preserve last successful
            continue

    return True


def run_watch_mode(
    schema: dict,
    params: list,
    cli_args: dict,
    primary_store_url: Optional[str],
    secondary_store_url: Optional[str],
    secondary_store_poll_interval: float,
    primary_store_prefix: Optional[str] = None,
    secondary_store_key_prefix: Optional[str] = None,
    secondary_store_key_separator: Optional[str] = None
):
    # For primary store: monitored_keys maps composed_key -> (param_path, type_name, declared_key)
    # We need to track declared_key for version tracking
    primary_monitored = {}  # composed_key -> (param_path, type_name, declared_key)
    primary_versions = {}   # declared_key -> version
    primary_cursor = 0

    # For secondary store: monitored_keys maps composed_key -> (param_path, type_name)
    secondary_monitored = {}
    secondary_last_successful = {}  # composed_key -> last successful formatted value

    current_values = {}

    resolved = {}
    for param_path, param_decl in params:
        type_name = param_decl['type']

        found, parsed_value, source_type, raw_or_error = resolve_from_sources(
            param_path, param_decl, cli_args, primary_store_url, secondary_store_url,
            primary_versions, primary_store_prefix, secondary_store_key_prefix,
            secondary_store_key_separator
        )

        if not found:
            if source_type is not None:
                print(f"Error: Parameter '{param_path}' failed to parse from {source_type}: {raw_or_error}", file=sys.stderr)
            else:
                print(f"Error: Unresolved parameter: {param_path}", file=sys.stderr)
            sys.exit(1)

        formatted = format_value_for_event(parsed_value, type_name)
        resolved[param_path] = format_value_for_json(parsed_value, type_name)
        current_values[param_path] = formatted

        emit_change_event(param_path, type_name, "", formatted)

        if 'primary-store' in param_decl and primary_store_url:
            declared_key = param_decl['primary-store']
            composed_key = compose_primary_store_key(declared_key, primary_store_prefix)
            primary_monitored[composed_key] = (param_path, type_name, declared_key)

        if 'secondary-store' in param_decl and secondary_store_url:
            declared_key = param_decl['secondary-store']
            composed_key = compose_secondary_store_key(declared_key, secondary_store_key_prefix, secondary_store_key_separator)
            secondary_monitored[composed_key] = (param_path, type_name)
            # Initialize last_successful with seed-time value
            secondary_last_successful[composed_key] = formatted

    output = build_output_structure_with_path(resolved, "", schema)
    print(_build_json(output), flush=True)

    has_primary = len(primary_monitored) > 0
    has_secondary = len(secondary_monitored) > 0

    if not has_primary and not has_secondary:
        sys.exit(0)

    last_secondary_poll = 0.0

    while True:
        now = time.time()

        if has_secondary:
            if now - last_secondary_poll >= secondary_store_poll_interval:
                poll_secondary_store(
                    secondary_store_url,
                    secondary_monitored,
                    current_values,
                    {},  # baselines not used anymore
                    secondary_last_successful
                )
                last_secondary_poll = now

        if has_primary:
            new_cursor, success, error = watch_primary_store_with_prefix(
                primary_store_url,
                primary_monitored,
                current_values,
                primary_versions,
                primary_cursor
            )
            if not success:
                print(f"Error: {error}", file=sys.stderr)
                sys.exit(1)
            primary_cursor = new_cursor

            time.sleep(0.1)
        else:
            if has_secondary:
                sleep_time = secondary_store_poll_interval - (time.time() - last_secondary_poll)
                if sleep_time > 0:
                    time.sleep(sleep_time)


def main():
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    try:
        global_flags, remaining_args = parse_global_flags(sys.argv[1:])
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if len(remaining_args) < 1:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    schema_path = remaining_args[0]
    arg_candidates = remaining_args[1:]

    primary_store_url = global_flags.get('primary-store')
    secondary_store_url = global_flags.get('secondary-store')
    watch_mode = global_flags.get('watch', False)
    secondary_store_poll_interval_str = global_flags.get('secondary-store-poll-interval')

    primary_store_prefix = global_flags.get('primary-store-prefix')
    secondary_store_key_prefix = global_flags.get('secondary-store-key-prefix')
    secondary_store_key_separator = global_flags.get('secondary-store-key-separator')

    # Validate: if --secondary-store-key-prefix is provided, --secondary-store-key-separator is required
    if secondary_store_key_prefix is not None and secondary_store_key_separator is None:
        print("Error: --secondary-store-key-prefix requires --secondary-store-key-separator", file=sys.stderr)
        sys.exit(1)

    secondary_store_poll_interval = 5.0
    if secondary_store_poll_interval_str:
        try:
            secondary_store_poll_interval = parse_poll_interval(secondary_store_poll_interval_str)
        except ValueError as e:
            print(f"Error: Invalid --secondary-store-poll-interval: {e}", file=sys.stderr)
            sys.exit(1)

    try:
        schema = load_schema(schema_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        validate_schema(schema)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    params = collect_parameters(schema)

    params_with_primary_store = [
        path for path, decl in params
        if 'primary-store' in decl
    ]

    if params_with_primary_store and not primary_store_url:
        param_list = ', '.join(sorted(params_with_primary_store))
        print(f"Error: Parameter(s) {param_list} declare primary-store but --primary-store is not configured", file=sys.stderr)
        sys.exit(1)

    params_with_secondary_store = [
        path for path, decl in params
        if 'secondary-store' in decl
    ]

    if params_with_secondary_store and not secondary_store_url:
        param_list = ', '.join(sorted(params_with_secondary_store))
        print(f"Error: Parameter(s) {param_list} declare secondary-store but --secondary-store is not configured", file=sys.stderr)
        sys.exit(1)

    if watch_mode and secondary_store_url:
        if secondary_store_poll_interval <= 0:
            print(f"Error: --secondary-store-poll-interval must be strictly positive", file=sys.stderr)
            sys.exit(1)

        if not params_with_secondary_store:
            print(f"Error: --secondary-store configured but no parameters declare secondary-store", file=sys.stderr)
            sys.exit(1)

    cli_args = parse_cli_args(arg_candidates)

    if watch_mode:
        run_watch_mode(
            schema,
            params,
            cli_args,
            primary_store_url,
            secondary_store_url,
            secondary_store_poll_interval,
            primary_store_prefix,
            secondary_store_key_prefix,
            secondary_store_key_separator
        )
    else:
        resolved = {}
        unresolved = []

        for param_path, param_decl in params:
            found, parsed_value, source_type, raw_or_error = resolve_from_sources(
                param_path, param_decl, cli_args, primary_store_url, secondary_store_url,
                None, primary_store_prefix, secondary_store_key_prefix, secondary_store_key_separator
            )

            if found:
                resolved[param_path] = format_value_for_json(parsed_value, param_decl['type'])
            elif source_type is not None:
                print(f"Error: Parameter '{param_path}' failed to parse from {source_type}: {raw_or_error}", file=sys.stderr)
                sys.exit(1)
            else:
                unresolved.append(param_path)

        if unresolved:
            print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
            sys.exit(1)

        output = build_output_structure_with_path(resolved, "", schema)

        print(_build_json(output))
        sys.exit(0)


if __name__ == '__main__':
    main()
