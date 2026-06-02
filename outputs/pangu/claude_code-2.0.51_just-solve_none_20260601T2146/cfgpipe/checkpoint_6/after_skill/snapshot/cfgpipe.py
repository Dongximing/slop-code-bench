#!/usr/bin/env python3
"""cfgpipe - Command-line configuration resolver."""

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


class FloatValue(TypedValue):
    """Float type wrapper with fixed 6-decimal JSON serialization."""
    __slots__ = ('_value',)

    def to_json(self):
        return round(self._value, 6)

    def __str__(self):
        return f"{self._value:.6f}"


class BooleanValue(TypedValue):
    """Boolean type wrapper for JSON serialization."""
    __slots__ = ('_value',)

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
        parts = []
        if ts >= 3600:
            parts.append(f"{ts // 3600}h")
            ts %= 3600
        if ts >= 60:
            parts.append(f"{ts // 60}m")
            ts %= 60
        if ts:
            parts.append(f"{ts}s")
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
            total_seconds += num * (3600 if unit == 'h' else 60 if unit == 'm' else 1)

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
        parts = {}
        for seg in s.split(','):
            if ':' not in seg or not seg.split(':')[0]:
                raise ValueError(f"map entry missing colon or has empty key: {seg!r}")
            k, v = seg.split(':', 1)
            parts[k] = v
        return cls(parts)


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
# Helper Functions
# ============================================================================

def compose_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def compose_primary_store_key(declared_key: str, prefix: str | None) -> str:
    if not prefix:
        return declared_key
    clean_prefix = prefix.strip('/')
    clean_key = declared_key.lstrip('/')
    return clean_key if not clean_prefix else f"{clean_prefix}/{clean_key}"


def compose_secondary_store_key(declared_key: str, prefix: str | None, separator: str | None) -> str:
    if not prefix or not separator:
        return declared_key
    return f"{prefix}{separator}{declared_key}"


def parse_cli_args(args: list[str]) -> tuple[dict[str, str], list[str]]:
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
    try:
        if os.path.isfile(file_path):
            return open(file_path).read().strip() or None
    except (OSError, IOError):
        pass
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

        if 'type' in value:
            if not is_valid_type(value['type']):
                errors.append(f"{p}: unrecognized type '{value['type']}'")
            for field, sm in [(k, v) for k, v in [('primary-store', pmap), ('secondary-store', smap)]]:
                if field in value:
                    if not isinstance(value[field], str):
                        errors.append(f"{p}: '{field}' field must be a string")
                    else:
                        sm.setdefault(value[field], []).append(p)
            for field in ['default', 'env', 'file', 'arg']:
                if field in value and not isinstance(value[field], str):
                    errors.append(f"{p}: '{field}' field must be a string")
        else:
            for field in ['default', 'env', 'file', 'arg', 'primary-store', 'secondary-store']:
                if field in value:
                    errors.append(f"{p}: group carries source annotation '{field}'")
            validate_schema_recursive(value, p, errors, pmap, smap)


def is_valid_type(type_name: str) -> bool:
    return type_name in {'string', 'integer', 'float', 'boolean', 'duration', 'pattern', 'map', 'list', 'redacted', 'port'}


def validate_schema(schema: dict[str, Any]) -> list[str]:
    errors = []
    pmap: dict[str, list[str]] = {}
    smap: dict[str, list[str]] = {}
    validate_schema_recursive(schema, "", errors, pmap, smap)
    for key, params in pmap.items():
        if len(params) > 1:
            errors.append(f"duplicate primary-store key '{key}' used by: {', '.join(params)}")
    for key, params in smap.items():
        if len(params) > 1:
            errors.append(f"duplicate secondary-store key '{key}' used by: {', '.join(params)}")
    return errors


def parse_value(value: str, type_name: str) -> Any:
    if type_name == 'string':
        return value
    if type_name == 'integer':
        if '.' in value:
            raise ValueError("integer type does not accept decimal values")
        return int(value)
    if type_name == 'float':
        if 'e' in value.lower():
            raise ValueError("scientific notation not allowed")
        return FloatValue(float(value))
    if type_name == 'boolean':
        vl = value.lower()
        if vl in {'true', 'yes', 'on', '1', 't', 'y'}:
            return True
        if vl in {'false', 'no', 'off', '0', 'f', 'n'}:
            return False
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

    return data.get('value') if data.get('found', False) else None


def primary_store_lookup(base_url: str, key: str, prefix: str | None = None) -> str | None:
    return _store_lookup(base_url, compose_primary_store_key(key, prefix), '/v1/primary/kv')


def secondary_store_lookup(base_url: str, key: str, prefix: str | None = None, separator: str | None = None) -> str | None:
    return _store_lookup(base_url, compose_secondary_store_key(key, prefix, separator), '/v1/secondary/kv')


def resolve_parameter(
    path: str,
    decl: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    primary_store_prefix: str | None,
    secondary_store_url: str | None,
    secondary_store_key_prefix: str | None,
    secondary_store_key_separator: str | None,
) -> tuple[str | None, str | None]:
    key = decl.get('arg', path)
    if val := cli_args.get(key):
        return val, 'arg'

    if secondary_store_url and 'secondary-store' in decl:
        try:
            if val := secondary_store_lookup(
                secondary_store_url, decl['secondary-store'],
                secondary_store_key_prefix, secondary_store_key_separator
            ):
                return val, 'secondary-store'
        except RuntimeError:
            raise

    if primary_store_url and 'primary-store' in decl:
        try:
            if val := primary_store_lookup(
                primary_store_url, decl['primary-store'], primary_store_prefix
            ):
                return val, 'primary-store'
        except RuntimeError:
            raise

    if 'file' in decl and (val := get_file_value(decl['file'])) is not None:
        return val, 'file'
    if 'env' in decl and (val := get_env_value(decl['env'])) is not None:
        return val, 'env'
    if 'default' in decl:
        return decl['default'], 'default'
    return None, None


def resolve_schema(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    primary_store_prefix: str | None,
    secondary_store_url: str | None,
    secondary_store_key_prefix: str | None,
    secondary_store_key_separator: str | None,
    emit_event: Callable | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    errors: list[dict[str, Any]] = []
    param_values: dict[str, Any] = {}

    def resolve_node(node: dict[str, Any], current_path: str) -> dict[str, Any] | None:
        output = {}
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if not isinstance(value, dict) or 'type' not in value:
                resolved = resolve_node(value, path)
                return None if resolved is None and value else resolved

            try:
                val_str, source = resolve_parameter(
                    path, value, cli_args,
                    primary_store_url, primary_store_prefix,
                    secondary_store_url,
                    secondary_store_key_prefix,
                    secondary_store_key_separator
                )
            except RuntimeError as e:
                errors.append({'path': path, 'detail': str(e)})
                return None
            if val_str is None:
                errors.append({'path': path, 'detail': 'unresolved parameter'})
                return None
            try:
                resolved_val = parse_value(val_str, value['type'])
                param_values[path] = resolved_val
                output[key] = resolved_val
            except ValueError as e:
                errors.append({'path': path, 'detail': str(e)})
                return None

        return output or output

    result = resolve_node(schema, "")
    if result is not None and emit_event:
        for path, value in param_values.items():
            emit_event({'path': path, 'previous': '', 'current': value}, is_seed=True)

    return result, errors


def get_param_type(node: dict[str, Any], path: str) -> str | None:
    parts = path.split('.')
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node.get('type') if isinstance(node, dict) else None


def check_secondary_store_config(schema: dict[str, Any], secondary_store_url: str | None, watch_mode: bool, poll_interval: float | None) -> str | None:
    if not watch_mode or not secondary_store_url:
        return None
    if poll_interval is not None and poll_interval <= 0:
        return "Error: --secondary-store-poll-interval must be strictly positive"

    def has_secondary_store(node: dict[str, Any]) -> bool:
        for v in node.values():
            if isinstance(v, dict):
                if 'type' in v and 'secondary-store' in v:
                    return True
                if has_secondary_store(v):
                    return True
        return False

    if not has_secondary_store(schema):
        return "Error: no parameters declare secondary-store but --secondary-store is specified"
    return None


def emit_change_event(path: str, type_name: str, previous: Any, current: Any) -> None:
    print(json.dumps({
        'path': path,
        'type': type_name,
        'previous': '' if previous == '' else str(previous),
        'current': '' if current == '' else str(current)
    }))


def collect_store_params(schema: dict[str, Any], field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    def collect(node: dict[str, Any], path: str) -> None:
        for k, v in node.items():
            p = compose_path(path, k)
            if isinstance(v, dict) and 'type' in v and field in v:
                result[p] = v[field]
            if isinstance(v, dict):
                collect(v, p)
    collect(schema, "")
    return result


def build_output_json(param_values: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}

    def build(node: dict[str, Any], current_path: str) -> dict[str, Any]:
        result = {}
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if isinstance(value, dict) and 'type' in value and path in param_values:
                val = param_values[path]
                result[key] = val.to_json() if isinstance(val, TypedValue) else val
            elif isinstance(value, dict):
                nested = build(value, path)
                if nested:
                    result[key] = nested
        return result

    return build(schema, "")


def _get_param_decl(schema: dict[str, Any], path: str) -> dict[str, Any] | None:
    parts = path.split('.')
    node = schema
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        return None
    decl = node[parts[-1]]
    return decl if isinstance(decl, dict) and 'type' in decl else None


def _run_watch_mode_initial_resolution(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    primary_store_prefix: str | None,
    secondary_store_url: str | None,
    secondary_store_key_prefix: str | None,
    secondary_store_key_separator: str | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    param_values: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    def resolve_node(node: dict[str, Any], current_path: str) -> None:
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if isinstance(value, dict) and 'type' in value:
                try:
                    val_str, _ = resolve_parameter(
                        path, value, cli_args,
                        primary_store_url, primary_store_prefix,
                        secondary_store_url,
                        secondary_store_key_prefix,
                        secondary_store_key_separator
                    )
                    if val_str is not None:
                        param_values[path] = parse_value(val_str, value['type'])
                except RuntimeError as e:
                    raise
                except ValueError:
                    pass
            elif isinstance(value, dict):
                resolve_node(value, path)

    try:
        resolve_node(schema, "")
    except RuntimeError as e:
        errors.append({'detail': str(e)})
        return {}, errors

    def check_unresolved(node: dict[str, Any], current_path: str) -> bool:
        for key, value in node.items():
            path = compose_path(current_path, key) if current_path else key
            if isinstance(value, dict) and 'type' in value and path not in param_values:
                errors.append({'path': path, 'detail': 'unresolved parameter'})
                return False
            if isinstance(value, dict) and not check_unresolved(value, path):
                return False
        return True

    if not check_unresolved(schema, ""):
        return {}, errors
    return param_values, errors


def run_watch_mode(
    schema: dict[str, Any],
    cli_args: dict[str, str],
    primary_store_url: str | None,
    primary_store_prefix: str | None,
    secondary_store_url: str | None,
    secondary_store_key_prefix: str | None,
    secondary_store_key_separator: str | None,
    poll_interval: float | None,
) -> int:
    param_values, errors = _run_watch_mode_initial_resolution(
        schema, cli_args, primary_store_url, primary_store_prefix,
        secondary_store_url, secondary_store_key_prefix, secondary_store_key_separator
    )
    if errors:
        for err in errors:
            print(f"Error: {err.get('path', '')}: {err['detail']}", file=sys.stderr)
        return 1

    for path, value in param_values.items():
        decl = _get_param_decl(schema, path)
        type_name = decl['type'] if decl else 'string'
        emit_change_event(path, type_name, '', value)

    print(json.dumps(build_output_json(param_values, schema)))

    primary_params = collect_store_params(schema, 'primary-store')
    secondary_params = collect_store_params(schema, 'secondary-store')
    secondary_last_values: dict[str, Any] = {}

    if primary_params and primary_store_url:
        primary_key_to_paths: dict[str, list[str]] = {}
        for path, pkey in primary_params.items():
            primary_key_to_paths.setdefault(pkey, []).append(path)

        key_versions: dict[str, int] = {k: 0 for k in primary_key_to_paths}

        while True:
            for pkey in primary_key_to_paths:
                tracked = primary_key_to_paths[pkey]
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
                for event in data.get('events', []):
                    key = event.get('key', '')
                    value = event.get('value', '')
                    version = event.get('version', 0)
                    if key != pkey or version <= key_versions[pkey]:
                        continue

                    key_versions[pkey] = version
                    for param_path in tracked:
                        decl = _get_param_decl(schema, param_path)
                        if not decl:
                            continue
                        old_value = param_values.get(param_path, '')
                        try:
                            new_value = parse_value(value, decl['type'])
                        except ValueError:
                            continue
                        if new_value != old_value:
                            param_values[param_path] = new_value
                            emit_change_event(param_path, decl['type'], old_value, new_value)

                key_versions[pkey] = new_cursor

            if secondary_store_url and poll_interval is not None:
                try:
                    url = f"{secondary_store_url}/v1/secondary/batch-read"
                    body = json.dumps({'keys': list(secondary_params.values())}).encode('utf-8')
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
                        path = next((p for p, k in secondary_params.items() if k == key), None)
                        if path is None or status != 'ok':
                            continue

                        decl = _get_param_decl(schema, path)
                        if not decl:
                            continue
                        try:
                            parsed = parse_value(value, decl['type'])
                        except ValueError:
                            continue

                        old_stored = secondary_last_values.get(path, '')
                        if parsed != old_stored:
                            secondary_last_values[path] = parsed
                            current_val = param_values.get(path, '')
                            if parsed != current_val:
                                param_values[path] = parsed
                                emit_change_event(path, decl['type'], current_val, parsed)
                            else:
                                emit_change_event(path, decl['type'], '', parsed)

                time.sleep(poll_interval)

    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [flags...] <schema> [args...]", file=sys.stderr)
        return 1

    schema_path = None
    schema_idx = None
    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg in ('--primary-store', '--secondary-store'):
            i += 2 if i + 1 < len(sys.argv) else 1
        elif arg == '--secondary-store-poll-interval':
            i += 2 if i + 1 < len(sys.argv) else 1
        elif not arg.startswith('-'):
            schema_path = arg
            schema_idx = i
            break
        else:
            i += 1

    if not schema_path:
        print("Error: no schema file specified", file=sys.stderr)
        return 1

    global_args = sys.argv[1:schema_idx]
    arg_candidates = sys.argv[schema_idx + 1:]
    cli_args, _ = parse_cli_args(arg_candidates)

    primary_store_url = None
    secondary_store_url = None
    secondary_store_poll_interval: float | None = None
    watch_mode = False
    primary_store_prefix: str | None = None
    secondary_store_key_prefix: str | None = None
    secondary_store_key_separator: str | None = None

    g = iter(global_args)
    for arg in g:
        if arg == '--primary-store':
            primary_store_url = next(g, None)
        elif arg == '--secondary-store':
            secondary_store_url = next(g, None)
        elif arg == '--secondary-store-poll-interval':
            interval_str = next(g, None)
            if interval_str:
                interval = float(interval_str[:-1]) if interval_str.endswith('s') else float(interval_str)
                secondary_store_poll_interval = interval if interval_str else None
            else:
                secondary_store_poll_interval = None
        elif arg == '--primary-store-prefix':
            primary_store_prefix = next(g, None)
        elif arg == '--secondary-store-key-prefix':
            secondary_store_key_prefix = next(g, None)
        elif arg == '--secondary-store-key-separator':
            secondary_store_key_separator = next(g, None)
        elif arg == '--watch':
            watch_mode = True

    try:
        with open(schema_path) as f:
            schema = json.load(f)
    except OSError as e:
        print(f"Error reading schema file: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON schema: {e}", file=sys.stderr)
        return 1

    validation_errors = validate_schema(schema)
    if validation_errors:
        for err in validation_errors:
            print(f"Error: {err}", file=sys.stderr)
        return 1

    err = check_secondary_store_config(schema, secondary_store_url, watch_mode, secondary_store_poll_interval)
    if err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    if watch_mode:
        return run_watch_mode(
            schema, cli_args,
            primary_store_url, primary_store_prefix,
            secondary_store_url,
            secondary_store_key_prefix, secondary_store_key_separator,
            secondary_store_poll_interval
        )

    resolved, errors = resolve_schema(
        schema, cli_args,
        primary_store_url, primary_store_prefix,
        secondary_store_url,
        secondary_store_key_prefix, secondary_store_key_separator
    )

    if errors:
        for err in errors:
            print(f"Error: {err.get('path', '')}: {err['detail']}", file=sys.stderr)
        return 1

    print(json.dumps(resolved))
    return 0


if __name__ == '__main__':
    sys.exit(main())
