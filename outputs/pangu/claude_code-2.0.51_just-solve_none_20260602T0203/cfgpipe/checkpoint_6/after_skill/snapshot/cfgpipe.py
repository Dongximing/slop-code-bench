#!/usr/bin/env python3
"""cfgpipe - Configuration resolver CLI with nested groups and custom types."""

import json
import os
import re
import sys
import time
import urllib.parse
from typing import Any

import requests


def parse_cli_args(cli_args: list[str]) -> tuple[dict[str, str], dict[str, str | None]]:
    """Parse CLI argument candidates into a dict of name -> value and store configs."""
    result = {}
    store_config: dict[str, str | None] = {
        "primary_store": None,
        "primary_store_prefix": None,
        "secondary_store": None,
        "secondary_store_key_prefix": None,
        "secondary_store_key_separator": None,
        "watch": None
    }
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]

        if arg == '--primary-store':
            if i + 1 < len(cli_args):
                store_config["primary_store"] = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--primary-store requires a URL")

        elif arg == '--primary-store-prefix':
            if i + 1 < len(cli_args):
                store_config["primary_store_prefix"] = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--primary-store-prefix requires a path")

        elif arg == '--secondary-store':
            if i + 1 < len(cli_args):
                store_config["secondary_store"] = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--secondary-store requires a URL")

        elif arg == '--secondary-store-key-prefix':
            if i + 1 < len(cli_args):
                store_config["secondary_store_key_prefix"] = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--secondary-store-key-prefix requires a prefix")

        elif arg == '--secondary-store-key-separator':
            if i + 1 < len(cli_args):
                store_config["secondary_store_key_separator"] = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--secondary-store-key-separator requires a separator")

        elif arg == '--watch':
            store_config["watch"] = ""
            i += 1

        elif '=' in arg:
            name_part, value = arg.split('=', 1)
            name = name_part.lstrip('-')
            result[name] = value
        else:
            # Positional argument (like schema file) - skip, handled by main()
            pass
        i += 1
    return result, store_config


class ValidationError(Exception):
    """Raised for schema validation errors."""
    pass


def compose_primary_store_key(declared_key: str, prefix: str | None) -> str:
    """Compose primary store key with optional folder prefix.

    Strip leading and trailing '/' from prefix, strip leading '/' from key.
    If prefix is empty, use key as-is. Otherwise use '<prefix>/<key>'.
    """
    if not prefix:
        return declared_key

    # Strip leading and trailing '/' from prefix
    clean_prefix = prefix.strip('/')
    # Strip leading '/' from declared key
    clean_key = declared_key.lstrip('/')

    if not clean_prefix:
        return clean_key

    return f"{clean_prefix}/{clean_key}"


def compose_secondary_store_key(declared_key: str, prefix: str | None, separator: str | None) -> str:
    """Compose secondary store key with optional prefix and separator.

    If no effective prefix, use key as-is.
    Otherwise, use '<prefix><separator><key>'.
    An empty separator means direct concatenation.
    """
    if not prefix or not prefix.strip():
        return declared_key
    if separator is None:
        # This should have been caught earlier as setup failure
        return declared_key

    effective_sep = separator if separator is not None else ""
    return f"{prefix}{effective_sep}{declared_key}"


def parse_port(value: str) -> str:
    """Parse and validate a port number string.

    Validates: base-10 integer, range 0-65535, no leading zeros except '0'.
    Returns the normalized string value.
    """
    value = value.strip()
    if not value.isdigit():
        raise ValueError(f"cannot parse '{value}' as port: not a non-negative integer")
    # Check for leading zeros (except single '0')
    if len(value) > 1 and value[0] == '0':
        raise ValueError(f"cannot parse '{value}' as port: leading zeros not allowed")
    port = int(value)
    if port < 0 or port > 65535:
        raise ValueError(f"cannot parse '{value}' as port: value out of range 0-65535")
    return value


def parse_duration(value: str) -> str:
    """Parse and normalize a duration string.

    Accepts integer-unit pairs (h/m/s) without separators. Normalizes
    to descending unit order, crossing boundaries, dropping zero-valued units.
    """
    value = value.strip()
    if value == '0s':
        return '0s'

    # Parse the duration string
    total_seconds = 0
    pattern = re.compile(r'^(\d+)([hms])')  # Match one pair at the start

    # For concatenated pairs like "1h30m", "2h1m30s"
    # We need to parse all pairs
    remaining = value
    while remaining:
        match = pattern.match(remaining)
        if not match:
            # Check if it looks like a bare number (malformed)
            if remaining.isdigit():
                raise ValueError(f"cannot parse '{value}' as duration: bare number without unit")
            raise ValueError(f"cannot parse '{value}' as duration: malformed format")
        num = int(match.group(1))
        unit = match.group(2)
        if unit == 'h':
            total_seconds += num * 3600
        elif unit == 'm':
            total_seconds += num * 60
        elif unit == 's':
            total_seconds += num
        remaining = remaining[match.end():]

    if total_seconds == 0:
        return '0s'

    # Normalize to descending order
    hours = total_seconds // 3600
    remaining_seconds = total_seconds % 3600
    minutes = remaining_seconds // 60
    seconds = remaining_seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if seconds > 0:
        parts.append(f"{seconds}s")

    return ''.join(parts)


def parse_pattern(value: str, full_path: str) -> str:
    """Validate a regex pattern string.

    Returns the original pattern on success.
    """
    try:
        re.compile(value)
    except re.error as e:
        raise ValueError(
            f"Parameter '{full_path}': invalid regex pattern '{value}': {e}"
        )
    return value






def parse_type(value: str, expected_type: str, full_path: str) -> Any:
    """Parse and validate a string value against the expected type.

    Returns the native JSON-serializable value for the type.
    Raises ValueError if parsing fails.
    """
    if expected_type == 'string':
        return value

    elif expected_type == 'integer':
        value = value.strip()
        try:
            int_val = int(value)
        except ValueError:
            raise ValueError(
                f"Parameter '{full_path}': cannot parse '{value}' as integer"
            )
        if 'e' in value.lower() or 'E' in value:
            raise ValueError(
                f"Parameter '{full_path}': scientific notation not allowed for integers"
            )
        return int_val

    elif expected_type == 'float':
        value = value.strip()
        try:
            fval = float(value)
            if 'e' in value.lower() or 'E' in value:
                raise ValueError(
                    f"Parameter '{full_path}': scientific notation not allowed"
                )
            return fval
        except ValueError:
            raise ValueError(
                f"Parameter '{full_path}': cannot parse '{value}' as float"
            )

    elif expected_type == 'boolean':
        value_lower = value.strip().lower()
        if value_lower in ('true', 'false'):
            return value_lower == 'true'
        else:
            raise ValueError(
                f"Parameter '{full_path}': boolean must be 'true' or 'false', got '{value}'"
            )

    elif expected_type == 'port':
        port_str = parse_port(value)
        return port_str

    elif expected_type == 'duration':
        normalized = parse_duration(value)
        return normalized

    elif expected_type == 'pattern':
        pattern = parse_pattern(value, full_path)
        return pattern

    elif expected_type == 'map':
        value = value.strip()
        if not value:
            return {}
        # Parse into dict for JSON serialization
        result = {}
        pairs = value.split(',')
        for pair in pairs:
            if ':' not in pair:
                raise ValueError(f"cannot parse '{value}' as map: pair '{pair}' has no colon")
            key, _, val = pair.partition(':')
            result[key] = val
        return result

    elif expected_type == 'list':
        # Parse into list for JSON serialization
        value = value.strip()
        if value:
            result = value.split(',')
        else:
            result = []
        return result

    elif expected_type == 'redacted':
        # Return a masked value for redacted types
        return "<masked>"

    raise ValueError(f"Parameter '{full_path}': unknown type '{expected_type}'")


def walk_schema(
    schema: dict[str, Any],
    path: str = ""
) -> list[tuple[str, dict[str, Any]]]:
    """Walk a nested schema and yield (full_path, param_spec) for all parameters."""
    if not isinstance(schema, dict):
        raise ValidationError(f"Schema must be a non-empty object at '{path}'")

    if not schema:
        raise ValidationError(f"Schema root must be a non-empty object at '{path}'")

    result = []
    primary_store_keys: dict[str, list[str]] = {}
    secondary_store_keys: dict[str, list[str]] = {}

    def walk(obj: dict[str, Any], current_path: str):
        for key, value in obj.items():
            full_path = f"{current_path}.{key}" if current_path else key

            if isinstance(value, dict):
                if 'type' in value:
                    # This is a parameter declaration
                    param_spec = value
                    param_type = param_spec.get('type')
                    if param_type not in ('string', 'integer', 'float', 'boolean', 'port', 'duration', 'pattern', 'map', 'list', 'redacted'):
                        raise ValidationError(
                            f"Parameter '{full_path}': unrecognized type '{param_type}'"
                        )

                    for field in ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'):
                        if field in param_spec and not isinstance(param_spec[field], str):
                            raise ValidationError(
                                f"Parameter '{full_path}': '{field}' must be a string, "
                                f"got {type(param_spec[field]).__name__}"
                            )

                    # Track primary-store keys for duplicate detection across full hierarchy
                    if 'primary-store' in param_spec:
                        pstore_key = param_spec['primary-store']
                        if pstore_key not in primary_store_keys:
                            primary_store_keys[pstore_key] = []
                        primary_store_keys[pstore_key].append(full_path)

                    # Track secondary-store keys for duplicate detection
                    if 'secondary-store' in param_spec:
                        sstore_key = param_spec['secondary-store']
                        if sstore_key not in secondary_store_keys:
                            secondary_store_keys[sstore_key] = []
                        secondary_store_keys[sstore_key].append(full_path)

                    result.append((full_path, param_spec))
                else:
                    # This is a group - recursively walk it
                    # Check that the group doesn't have source-annotation keys with non-object values
                    for field in ('default', 'env', 'file', 'arg', 'primary-store', 'secondary-store'):
                        if field in value and not isinstance(value[field], dict):
                            raise ValidationError(
                                f"Group '{full_path}': '{field}' must be an object, "
                                f"got {type(value[field]).__name__}"
                            )
                    walk(value, full_path)
            else:
                # Invalid: should be either a param (dict with 'type') or a group (dict without)
                if isinstance(value, (list, tuple, int, float, bool, str)):
                    raise ValidationError(
                        f"Entry '{full_path}': must be an object (parameter or group), "
                        f"got {type(value).__name__}"
                    )

    try:
        walk(schema, "")
    except ValidationError:
        raise  # Re-raise validation errors

    # Check for duplicate primary-store keys
    for key, params in primary_store_keys.items():
        if len(params) > 1:
            raise ValidationError(
                f"Duplicate primary-store key '{key}' used by parameters {params}"
            )

    return result


def validate_schema(schema: dict[str, Any]) -> None:
    """Validate the schema structure and field types.

    Raises:
        ValidationError: If schema is invalid.
    """
    walk_schema(schema)


def load_schema(schema_path: str) -> dict[str, Any]:
    """Load and parse the schema file.

    Raises:
        FileNotFoundError: If schema file doesn't exist.
        ValidationError: If JSON is invalid or schema is malformed.
    """
    try:
        with open(schema_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        schema = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValidationError(f"Invalid JSON in schema file: {e}")

    validate_schema(schema)
    return schema


def fetch_from_primary_store(
    full_path: str,
    param_spec: dict[str, Any],
    store_url: str,
    store_prefix: str | None = None
) -> str | None:
    """Fetch a parameter value from the primary store with prefix composition."""
    declared_key = param_spec['primary-store']
    composed_key = compose_primary_store_key(declared_key, store_prefix)
    encoded_key = urllib.parse.quote(composed_key, safe='')
    url = f"{store_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException as e:
        raise ValueError(
            f"Parameter '{full_path}': primary-store connector failure: {e}"
        )

    if response.status_code == 404:
        try:
            data = response.json()
            if data.get('found') is False:
                return None
            else:
                raise ValueError(
                    f"Parameter '{full_path}': primary-store responded 404 with invalid body"
                )
        except (json.JSONDecodeError, KeyError):
            raise ValueError(
                f"Parameter '{full_path}': primary-store responded 404 with malformed body"
            )

    if response.status_code != 200:
        raise ValueError(
            f"Parameter '{full_path}': primary-store connector failure: HTTP {response.status_code}"
        )

    try:
        data = response.json()
    except json.JSONDecodeError:
        raise ValueError(
            f"Parameter '{full_path}': primary-store responded with malformed JSON"
        )

    if not data.get('found', False):
        return None

    value = data.get('value')
    if value is None:
        raise ValueError(
            f"Parameter '{full_path}': primary-store response missing 'value' field"
        )

    if not isinstance(value, str):
        raise ValueError(
            f"Parameter '{full_path}': primary-store 'value' must be a string, got {type(value).__name__}"
        )

    param_type = param_spec['type']
    try:
        return parse_type(value, param_type, full_path)
    except ValueError as e:
        raise ValueError(
            f"Parameter '{full_path}': primary-store parse error: {str(e).removeprefix(f'Parameter \"{full_path}\": ')}"
        )


def fetch_from_secondary_store(
    full_path: str,
    param_spec: dict[str, Any],
    store_url: str | None,
    key_prefix: str | None,
    key_separator: str | None
) -> str | None:
    """Fetch a parameter value from secondary store (consul key-value).

    Returns the raw string value from the store, or None if not found/unavailable.
    Silently returns None for network errors during resolution (watch mode handles resilience).
    """
    if store_url is None:
        return None

    declared_key = param_spec.get('secondary-store')
    if not declared_key:
        return None

    composed_key = compose_secondary_store_key(declared_key, key_prefix, key_separator)
    encoded_key = urllib.parse.quote(composed_key, safe='')
    url = f"{store_url.rstrip('/')}/v1/kv/{encoded_key}?raw"

    try:
        response = requests.get(url, timeout=30)
    except requests.RequestException:
        # During resolution, failure is silent (secondary store is optional/backup)
        return None

    if response.status_code == 404:
        return None

    if response.status_code != 200:
        return None

    value = response.text.strip()
    if not value:
        return None

    return value


def resolve_parameter(
    full_path: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str],
    primary_store_url: str | None = None,
    primary_store_prefix: str | None = None,
    secondary_store_url: str | None = None,
    secondary_store_key_prefix: str | None = None,
    secondary_store_key_separator: str | None = None
) -> str | None:
    """Resolve a single parameter from its sources.

    Priority order (lowest to highest): default, env, file, primary-store, secondary-store, arg.
    Returns the resolved value as a string, or None if unresolved.
    Raises ValueError if parsing fails.
    """
    param_type = param_spec['type']
    candidates = []

    if 'default' in param_spec:
        candidates.append(('default', param_spec['default']))

    if 'env' in param_spec:
        env_var = param_spec['env']
        env_value = os.environ.get(env_var)
        if env_value is not None and env_value.strip():
            candidates.append(('env', env_value.strip()))

    if 'file' in param_spec:
        file_path = param_spec['file']
        try:
            with open(file_path, 'r') as f:
                file_content = f.read()
            trimmed = file_content.strip()
            if trimmed:
                candidates.append(('file', trimmed))
        except (FileNotFoundError, OSError, IsADirectoryError):
            pass

    if primary_store_url is not None and 'primary-store' in param_spec:
        pstore_value = fetch_from_primary_store(
            full_path, param_spec, primary_store_url, primary_store_prefix
        )
        if pstore_value is not None:
            candidates.append(('primary-store', pstore_value))

    if secondary_store_url is not None and 'secondary-store' in param_spec:
        sstore_value = fetch_from_secondary_store(
            full_path, param_spec,
            secondary_store_url, secondary_store_key_prefix, secondary_store_key_separator
        )
        if sstore_value is not None:
            candidates.append(('secondary-store', sstore_value))

    if 'arg' in param_spec:
        arg_name = param_spec['arg']
        if arg_name in arg_values:
            candidates.append(('arg', arg_values[arg_name]))

    parsed_candidates = []
    for source, value in candidates:
        try:
            parsed_val = parse_type(value, param_type, full_path)
            parsed_candidates.append((source, parsed_val))
        except ValueError as e:
            raise ValueError(
                f"Parameter '{full_path}': {source} source parse error: "
                f"{str(e).removeprefix(f'Parameter \"{full_path}\": ')}"
            )

    return parsed_candidates[-1][1] if parsed_candidates else None


def to_string_representation(value: Any, value_type: str) -> str:
    """Convert a native value to its string representation for change events."""
    if value_type == 'duration':
        return str(value)
    elif value_type == 'pattern':
        return str(value)
    elif value_type == 'map':
        # Deterministic lexicographic key order
        if isinstance(value, dict):
            sorted_items = sorted(value.items(), key=lambda x: x[0])
            return ','.join(f"{key}:{val}" for key, val in sorted_items)
        return ''
    elif value_type == 'list':
        if isinstance(value, list):
            return ','.join(value)
        return ''
    elif value_type == 'port':
        return str(value)
    elif value_type == 'redacted':
        return str(value)
    elif value_type in ('string', 'integer', 'float', 'boolean'):
        return str(value)
    else:
        return str(value)


def build_output(
    resolved_params: dict[str, Any],
    param_types: dict[str, str],
    schema: dict[str, Any],
    generate_change_events: bool = False
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build nested output structure from resolved parameters.

    Args:
        resolved_params: Mapping from full dotted path to resolved native value.
        param_types: Mapping from full path to parameter type string.
        schema: The original schema (used to determine structure).
        generate_change_events: Whether to also generate change events.

    Returns:
        Tuple of (nested dict, list of change events if generate_change_events is True).
    """
    output = {}
    change_events = []

    def insert(path: str, value: Any):
        parts = path.split('.')
        current = output
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value

    for full_path, value in resolved_params.items():
        insert(full_path, value)
        if generate_change_events:
            value_type = param_types.get(full_path, 'string')
            str_repr = to_string_representation(value, value_type)
            # For seed-time events, previous is ""
            event = {
                "path": full_path,
                "type": value_type,
                "previous": "",
                "current": str_repr
            }
            change_events.append(event)

    return output, change_events


def watch_mode(
    schema: dict[str, Any],
    store_config: dict[str, str | None],
    initial_resolved_params: dict[str, Any],
    initial_param_types: dict[str, str],
    param_specs: dict[str, dict[str, Any]]
) -> None:
    """Watch for configuration changes and emit events in real-time.

    Resilient secondary reads: failures for individual keys do not block
    processing of other keys in the same poll cycle.
    """
    primary_store_url = store_config.get("primary_store")
    primary_store_prefix = store_config.get("primary_store_prefix")
    secondary_store_url = store_config.get("secondary_store")
    secondary_store_key_prefix = store_config.get("secondary_store_key_prefix")
    secondary_store_key_separator = store_config.get("secondary_store_key_separator")

    # Track last successful observed values for each parameter
    last_values: dict[str, Any] = dict(initial_resolved_params)
    # Track which parameters are sourced from primary vs secondary stores
    param_sources: dict[str, str] = {}
    for full_path, param_spec in param_specs.items():
        if 'primary-store' in param_spec:
            param_sources[full_path] = 'primary'
        elif 'secondary-store' in param_spec:
            param_sources[full_path] = 'secondary'
        else:
            param_sources[full_path] = 'other'

    # Track observed versions for primary store keys (for tracking updates)
    primary_tracked_versions: dict[str, int] = {}
    # Initialize versions from initial resolution
    if primary_store_url:
        for full_path, param_spec in param_specs.items():
            if 'primary-store' in param_spec:
                composed_key = compose_primary_store_key(
                    param_spec['primary-store'],
                    primary_store_prefix
                )
                # Initialize version as 0 or track it
                primary_tracked_versions[composed_key] = 0

    # Map from composed store key -> list of parameter paths
    primary_key_to_params: dict[str, list[str]] = {}
    for full_path, param_spec in param_specs.items():
        if 'primary-store' in param_spec:
            composed_key = compose_primary_store_key(
                param_spec['primary-store'],
                primary_store_prefix
            )
            if composed_key not in primary_key_to_params:
                primary_key_to_params[composed_key] = []
            primary_key_to_params[composed_key].append(full_path)

    # For secondary store, map from composed key to parameter path
    secondary_key_to_param: dict[str, str | None] = {}
    for full_path, param_spec in param_specs.items():
        if 'secondary-store' in param_spec:
            composed_key = compose_secondary_store_key(
                param_spec['secondary-store'],
                secondary_store_key_prefix,
                secondary_store_key_separator
            )
            secondary_key_to_param[composed_key] = full_path

    # Poll configuration
    poll_interval = 1  # seconds

    while True:
        time.sleep(poll_interval)

        # Track changes in this cycle
        changes: list[dict[str, Any]] = []

        # Check primary store for updates
        if primary_store_url:
            for composed_key, param_paths in primary_key_to_params.items():
                encoded_key = urllib.parse.quote(composed_key, safe='')
                url = f"{primary_store_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

                try:
                    response = requests.get(url, timeout=30)
                    if response.status_code == 200:
                        try:
                            data = response.json()
                            if data.get('found', False):
                                new_value = data.get('value')
                                new_version = data.get('version', 0)

                                # Check version
                                tracked_version = primary_tracked_versions.get(composed_key, 0)
                                if new_version > tracked_version and isinstance(new_value, str):
                                    # Value changed, emit events for all parameters using this key
                                    for param_path in param_paths:
                                        param_spec = param_specs[param_path]
                                        param_type = param_spec.get('type', 'string')
                                        old_str = to_string_representation(last_values.get(param_path), param_type)
                                        try:
                                            parsed_new = parse_type(new_value, param_type, param_path)
                                            if parsed_new != last_values.get(param_path):
                                                str_repr = to_string_representation(parsed_new, param_type)
                                                changes.append({
                                                    "path": param_path,
                                                    "type": param_type,
                                                    "previous": old_str,
                                                    "current": str_repr
                                                })
                                                last_values[param_path] = parsed_new
                                        except ValueError:
                                            # Parse failure - silently skipped
                                            pass
                                    primary_tracked_versions[composed_key] = new_version
                        except (json.JSONDecodeError, KeyError):
                            pass
                except requests.RequestException:
                    pass

        # Check secondary store for updates (resilient per-key reads)
        if secondary_store_url:
            for composed_key, param_path in secondary_key_to_param.items():
                if param_path is None:
                    continue

                encoded_key = urllib.parse.quote(composed_key, safe='')
                url = f"{secondary_store_url.rstrip('/')}/v1/kv/{encoded_key}?raw"

                try:
                    response = requests.get(url, timeout=30)
                    if response.status_code == 200:
                        new_value = response.text.strip()
                        param_spec = param_specs[param_path]
                        param_type = param_spec.get('type', 'string')

                        try:
                            parsed_new = parse_type(new_value, param_type, param_path)
                            # If value differs from last successful observation, emit event
                            if parsed_new != last_values.get(param_path):
                                old_str = to_string_representation(last_values.get(param_path), param_type)
                                str_repr = to_string_representation(parsed_new, param_type)
                                changes.append({
                                    "path": param_path,
                                    "type": param_type,
                                    "previous": old_str,
                                    "current": str_repr
                                })
                                last_values[param_path] = parsed_new
                        except ValueError:
                            # Parse failure - silently skipped per spec
                            pass
                except requests.RequestException:
                    # Per-key failure - skip silently, last value preserved
                    pass

        # Output all change events for this cycle
        for change in changes:
            print(json.dumps(change))
            sys.stdout.flush()


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]

    try:
        arg_values, store_config = parse_cli_args(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Find schema position by looking for the first non-flag argument
    # (the schema file is the first argument that doesn't start with '-')
    schema_position = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith('--'):
            i += 1
        elif '=' in arg:
            # Key=value format, could be an arg or a flag
            name_part, _ = arg.split('=', 1)
            if name_part.startswith('--'):
                # This is a flag with =, already parsed by parse_cli_args
                i += 1
            else:
                # This is a user-provided arg, schema file should be before this
                break
        else:
            # This is likely the schema file
            schema_position = i
            break

    if schema_position is None:
        print("Error: Schema file not specified", file=sys.stderr)
        sys.exit(1)

    schema_path = args[schema_position]

    try:
        schema = load_schema(schema_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    primary_store_url = store_config.get("primary_store")
    primary_store_prefix = store_config.get("primary_store_prefix")
    secondary_store_url = store_config.get("secondary_store")
    secondary_store_key_prefix = store_config.get("secondary_store_key_prefix")
    secondary_store_key_separator = store_config.get("secondary_store_key_separator")
    watch_mode_enabled = store_config.get("watch") is not None

    # Filter arg_values to only include non-flag arguments
    # (parse_cli_args already extracted them, just need to ensure no overlap)
    # arg_values already contains only the user-provided key=value pairs

    # Validate secondary-store key-prefix/separator setup
    if secondary_store_key_prefix is not None and secondary_store_key_separator is None:
        print("Error: --secondary-store-key-prefix requires --secondary-store-key-separator", file=sys.stderr)
        sys.exit(1)

    # Check if any parameters declare primary-store but --primary-store is not configured
    if primary_store_url is None:
        params_with_pstore = [
            full_path for full_path, spec in walk_schema(schema)
            if 'primary-store' in spec
        ]
        if params_with_pstore:
            if len(params_with_pstore) == 1:
                print(f"Error: Parameter '{params_with_pstore[0]}' requires --primary-store flag", file=sys.stderr)
            else:
                print(f"Error: Parameters {params_with_pstore} require --primary-store flag", file=sys.stderr)
            sys.exit(1)

    # Resolve each parameter
    resolved_params: dict[str, Any] = {}
    param_types: dict[str, str] = {}
    param_specs: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    errors: list[str] = []

    for full_path, param_spec in walk_schema(schema):
        param_specs[full_path] = param_spec
        try:
            resolved = resolve_parameter(
                full_path, param_spec, arg_values,
                primary_store_url, primary_store_prefix,
                secondary_store_url, secondary_store_key_prefix, secondary_store_key_separator
            )
            param_type = param_spec.get('type', 'string')
            if resolved is None:
                unresolved.append(full_path)
            else:
                resolved_params[full_path] = resolved
                param_types[full_path] = param_type
        except ValueError as e:
            errors.append(str(e))

    if errors:
        for err in errors:
            print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)

    if unresolved:
        if len(unresolved) == 1:
            print(f"Error: Unresolved parameter: {unresolved[0]}", file=sys.stderr)
        else:
            print(f"Error: Unresolved parameters: {', '.join(unresolved)}", file=sys.stderr)
        sys.exit(1)

    # Build nested output structure
    output, change_events = build_output(
        resolved_params, param_types, schema, generate_change_events=watch_mode_enabled
    )

    if watch_mode_enabled:
        # Output initial state as JSON
        print(json.dumps(output))
        sys.stdout.flush()

        # Output initial seed-time change events
        for event in change_events:
            print(json.dumps(event))
            sys.stdout.flush()

        # Enter watch mode loop
        watch_mode(schema, store_config, resolved_params, param_types, param_specs)
    else:
        # Output resolved configuration as JSON
        print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
