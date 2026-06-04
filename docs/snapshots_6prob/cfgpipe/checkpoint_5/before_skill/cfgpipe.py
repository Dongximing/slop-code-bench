#!/usr/bin/env python3
"""cfgpipe - Configuration resolver CLI with nested groups and custom types."""

import json
import os
import re
import sys
import urllib.parse
from typing import Any

import requests


def parse_cli_args(cli_args: list[str]) -> tuple[dict[str, str], str | None]:
    """Parse CLI argument candidates into a dict of name -> value."""
    result = {}
    primary_store_url = None
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]
        if arg == '--primary-store':
            if i + 1 < len(cli_args):
                primary_store_url = cli_args[i + 1]
                i += 2
            else:
                raise ValueError("--primary-store requires a base URL")
        elif '=' in arg:
            name_part, value = arg.split('=', 1)
            name = name_part.lstrip('-')
            result[name] = value
            i += 1
        else:
            i += 1
    return result, primary_store_url


class ValidationError(Exception):
    """Raised for schema validation errors."""
    pass


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


def parse_map(value: str) -> str:
    """Parse a key:value map into a deterministic string representation.

    Comma-separated key:value pairs, splitting on first colon.
    Returns a comma-separated string with pairs sorted by key.
    """
    value = value.strip()
    if not value:
        return ''

    pairs = value.split(',')
    parsed = {}
    for pair in pairs:
        if ':' not in pair:
            raise ValueError(f"cannot parse '{value}' as map: pair '{pair}' has no colon")
        key, _, val = pair.partition(':')
        # Key is before the first colon, val is the rest
        parsed[key] = val

    # Sort by key and build deterministic representation
    sorted_items = sorted(parsed.items(), key=lambda x: x[0])
    return ','.join(f"{key}:{val}" for key, val in sorted_items)


def parse_list(value: str) -> str:
    """Parse a comma-separated list of strings.

    Preserves insertion order (the original order).
    Returns a comma-separated string (same as input, but may be useful for validation).
    """
    value = value.strip()
    return value  # Insertion order preserved as input string


def parse_redacted(value: str) -> str:
    """Parse a redacted value.

    Accepts any input, returns the value as-is (stored for later masking).
    """
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

                    for field in ('default', 'env', 'file', 'arg', 'primary-store'):
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

                    result.append((full_path, param_spec))
                else:
                    # This is a group - recursively walk it
                    # Check that the group doesn't have source-annotation keys with non-object values
                    for field in ('default', 'env', 'file', 'arg', 'primary-store'):
                        if field in value and not isinstance(value[field], dict):
                            raise ValidationError(
                                f"Group '{full_path}': '{field}' must be an object, "
                                f"got {type(value[field]).__name__}"
                            )
                    walk(value, full_path)
            else:
                # Invalid: should be either a param (dict with 'type') or a group (dict without)
                if isinstance(value, (list, tuple, int, float, bool, str)):
                    if 'type' in value if isinstance(value, dict) else False:
                        pass
                    else:
                        # Check if it looks like a value type
                        if not isinstance(value, dict):
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
    primary_store_url: str
) -> str | None:
    """Fetch a parameter value from the primary store."""
    key = param_spec['primary-store']
    encoded_key = urllib.parse.quote(key, safe='')
    url = f"{primary_store_url.rstrip('/')}/v1/primary/kv?key={encoded_key}"

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


def resolve_parameter(
    full_path: str,
    param_spec: dict[str, Any],
    arg_values: dict[str, str],
    primary_store_url: str | None = None
) -> str | None:
    """Resolve a single parameter from its sources.

    Priority order (lowest to highest): default, env, file, primary-store, arg.
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
        pstore_value = fetch_from_primary_store(full_path, param_spec, primary_store_url)
        if pstore_value is not None:
            candidates.append(('primary-store', pstore_value))

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

    if parsed_candidates:
        return parsed_candidates[-1][1]

    return None


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


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python cfgpipe.py [global-flags...] <schema-file> [arg-candidates...]", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    primary_store_url = None
    schema_position = -1

    i = 0
    while i < len(args):
        if args[i] == '--primary-store':
            if i + 1 < len(args):
                primary_store_url = args[i + 1]
                i += 2
            else:
                print("Error: --primary-store requires a base URL", file=sys.stderr)
                sys.exit(1)
        else:
            if schema_position == -1:
                schema_position = i
            i += 1

    if schema_position == -1:
        print("Error: Schema file not specified", file=sys.stderr)
        sys.exit(1)

    schema_path = args[schema_position]
    cli_args = args[schema_position + 1:]

    try:
        schema = load_schema(schema_path)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValidationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        arg_values, _ = parse_cli_args(cli_args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
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
    unresolved: list[str] = []
    errors: list[str] = []

    for full_path, param_spec in walk_schema(schema):
        try:
            resolved = resolve_parameter(full_path, param_spec, arg_values, primary_store_url)
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
    output, _ = build_output(resolved_params, param_types, schema)

    # Output resolved configuration as JSON
    print(json.dumps(output))
    sys.exit(0)


if __name__ == '__main__':
    main()
