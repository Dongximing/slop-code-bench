"""Utility functions for nested type handling."""

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from typesystem import DataType, PrimitiveType, StructType, ArrayType, MapType, JsonType, is_primitive_type, PRIMITIVE_TYPES
from datetime import datetime, date


TRUE_VALUES = {'true', 'yes', '1', 't', 'y'}
FALSE_VALUES = {'false', 'no', '0', 'f', 'n'}


def parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).lower().strip()
    if s in TRUE_VALUES:
        return True
    if s in FALSE_VALUES:
        return False
    return None


def parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (ValueError, AttributeError):
        return None


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (ValueError, AttributeError):
        return None


def parse_date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    s = str(value).strip()
    try:
        parsed = datetime.strptime(s, '%Y-%m-%d').date()
        return parsed.isoformat()
    except (ValueError, AttributeError):
        return None


def parse_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(value, date) and not isinstance(value, datetime):
        return f"{value.isoformat()}T00:00:00Z"
    s = str(value).strip()
    formats = [
        '%Y-%m-%dT%H:%M:%S.%fZ',
        '%Y-%m-%dT%H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%SZ',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%d %H:%M:%S',
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            continue
    return None


PRIMITIVE_PARSERS = {
    'string': lambda x: str(x).strip() if x is not None else None,
    'int': parse_int,
    'float': parse_float,
    'bool': parse_bool,
    'date': parse_date,
    'timestamp': parse_timestamp,
}


def cast_to_primitive(value: Any, target_type: str, error_strategy: str, field_path: str) -> Any:
    """Cast a value to a primitive type."""
    parser = PRIMITIVE_PARSERS.get(target_type)
    if not parser:
        if error_strategy == 'fail':
            raise ValueError(f"Unknown type: {target_type}")
        return None

    try:
        result = parser(value)
        if result is not None:
            return result
    except Exception:
        pass

    if error_strategy == 'coerce-null':
        return None
    elif error_strategy == 'keep-string':
        return str(value).strip() if value is not None else None
    else:
        raise ValueError(f'Cannot cast "{value}" to {target_type} in field "{field_path}"')


def normalize_json_value(value: Any, dtype: DataType, error_strategy: str) -> Any:
    """Normalize a JSON value according to its declared type."""
    # Handle empty string as null
    if value == '':
        value = None

    # JsonType: accept any JSON without casting, only normalize
    if isinstance(dtype, JsonType):
        return normalize_json(value)

    # Primitive casting
    if isinstance(dtype, PrimitiveType):
        return cast_to_primitive(value, dtype.name, error_strategy, "root")

    # Handle null
    if value is None:
        return None

    # Struct
    if isinstance(dtype, StructType):
        if not isinstance(value, dict):
            if error_strategy == 'coerce-null':
                return None
            elif error_strategy == 'keep-string':
                return json.dumps(value, ensure_ascii=False)
            else:
                raise ValueError(f'Cannot cast to struct: expected dict, got {type(value).__name__} in field "root"')

        result = {}
        for field in dtype.fields:
            field_name = field['name']
            field_type = field['type']
            field_value = value.get(field_name)
            if field_value is None:
                result[field_name] = None
            else:
                result[field_name] = normalize_json_value(field_value, field_type, error_strategy)
        return result

    # Array
    if isinstance(dtype, ArrayType):
        if not isinstance(value, list):
            if error_strategy == 'coerce-null':
                return None
            elif error_strategy == 'keep-string':
                return json.dumps(value, ensure_ascii=False)
            else:
                raise ValueError(f'Cannot cast to array: expected list, got {type(value).__name__} in field "root"')

        return [normalize_json_value(item, dtype.element, error_strategy) for item in value]

    # Map
    if isinstance(dtype, MapType):
        if not isinstance(value, dict):
            if error_strategy == 'coerce-null':
                return None
            elif error_strategy == 'keep-string':
                return json.dumps(value, ensure_ascii=False)
            else:
                raise ValueError(f'Cannot cast to map: expected dict, got {type(value).__name__} in field "root"')

        result = {}
        for k, v in value.items():
            str_key = str(k)
            result[str_key] = normalize_json_value(v, dtype.value_type, error_strategy)
        return result

    return value


def normalize_json(value: Any) -> Any:
    """Accept any JSON value without casting, only normalize structure."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: normalize_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_json(item) for item in value]
    return str(value)


def value_to_json_cell(value: Any) -> str:
    """Convert a normalized value to a JSON cell string for CSV."""
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def parse_json_cell(cell: str) -> Any:
    """Parse a CSV cell containing JSON literal."""
    if cell == '':
        return None
    if cell == 'null':
        return None
    try:
        return json.loads(cell)
    except json.JSONDecodeError:
        return None  # This will be handled by error strategy


def field_path_resolver(row: Dict[str, Any], path: str) -> Any:
    """Resolve a field path (e.g., user.id, items[0].qty) to a value."""
    # Parse path into components
    components = parse_field_path(path)

    current = row
    for comp in components:
        if current is None:
            return None

        if isinstance(comp, str):
            # Struct field access
            if not isinstance(current, dict):
                return None
            current = current.get(comp)
        elif isinstance(comp, int):
            # Array index access
            if not isinstance(current, list):
                return None
            if comp < 0 or comp >= len(current):
                return None
            current = current[comp]

    return current


def parse_field_path(path: str) -> List[Union[str, int]]:
    """Parse a field path string into components."""
    components = []
    i = 0

    while i < len(path):
        if path[i] == '.':
            i += 1
            # Start of struct field name
            name_start = i
            while i < len(path) and path[i] not in '.[':
                i += 1
            if i > name_start:
                components.append(path[name_start:i])
        elif path[i] == '[':
            # Map key or array index
            i += 1
            # Parse the content inside brackets
            if path[i] == '"':
                # Map string key
                i += 1
                key_start = i
                while i < len(path) and path[i] != '"':
                    i += 1
                if i < len(path):
                    components.append(path[key_start:i])
                    i += 1  # Skip closing quote
            else:
                # Array index (numeric)
                num_start = i
                while i < len(path) and path[i].isdigit():
                    i += 1
                if i > num_start:
                    components.append(int(path[num_start:i]))
        elif path[i] == ']':
            i += 1
        else:
            i += 1

    return components


def is_primitive_value(value: Any) -> bool:
    """Check if a value is a JSON primitive (not dict or list)."""
    return value is None or isinstance(value, (str, int, float, bool))


def get_resolved_type(value: Any) -> Optional[str]:
    """Get the inferred primitive type of a value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        # Try to detect date/timestamp
        try:
            if len(value) == 10 and value[4] == '-' and value[7] == '-':
                return "date"
            if 'T' in value:
                return "timestamp"
        except (IndexError, TypeError):
            pass
        return "string"
    return None
