"""Type system with nested types support."""

import json
from typing import Any, Dict, List, Optional, Set, Tuple, Union


# Built-in type aliases (case-insensitive)
BUILTIN_ALIASES = {
    "integer": "int",
    "long": "int",
    "double": "float",
    "number": "float",
    "boolean": "bool",
    "datetime": "timestamp",
    "timestamptz": "timestamp",
    "text": "string",
    "varchar": "string",
    "list": "array",
    "json": "json",
}


def resolve_alias(name: str, custom_aliases: Dict[str, str]) -> str:
    """Resolve a type name through aliases (built-in + custom)."""
    name = name.lower()
    all_aliases = {k.lower(): v for k, v in custom_aliases.items()}
    all_aliases.update(BUILTIN_ALIASES)

    visited = set()
    current = name

    while True:
        if current in visited:
            raise ValueError(f"Cycle detected in type aliases involving '{current}'")
        visited.add(current)

        if current not in all_aliases:
            return current

        next_val = all_aliases[current]
        if next_val.lower() == "json":
            return "json"
        current = next_val.lower()


class DataType:
    """Base class for all data types."""
    pass


class PrimitiveType(DataType):
    """Primitive type: string, int, float, bool, date, timestamp."""
    def __init__(self, name: str):
        self.name = name

    def __repr__(self):
        return self.name

    def __eq__(self, other):
        return isinstance(other, PrimitiveType) and self.name == other.name

    def __hash__(self):
        return hash(self.name)


class StructType(DataType):
    """Struct type with named fields."""
    def __init__(self, fields: List[Dict[str, Any]]):
        self.fields = fields
        # Check for duplicate field names
        names = [f["name"] for f in fields]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate field names in struct")

    def __repr__(self):
        return f"struct({{", ".join(f"{f['name']}: {f['type']}" for f in self.fields)}})"

    def __eq__(self, other):
        return isinstance(other, StructType) and self.fields == other.fields

    def __hash__(self):
        return hash((tuple(json.dumps(f, sort_keys=True) for f in self.fields),))


class ArrayType(DataType):
    """Array type with homogeneous element type."""
    def __init__(self, element: DataType):
        self.element = element

    def __repr__(self):
        return f"array[{self.element}]"

    def __eq__(self, other):
        return isinstance(other, ArrayType) and self.element == other.element

    def __hash__(self):
        return hash(("array", self.element))


class MapType(DataType):
    """Map type with string keys and value type."""
    def __init__(self, key_type: str, value_type: DataType):
        if key_type != "string":
            raise ValueError("Map key type must be string")
        self.key_type = key_type
        self.value_type = value_type

    def __repr__(self):
        return f"map<{self.key_type},{self.value_type}>"

    def __eq__(self, other):
        return isinstance(other, MapType) and self.value_type == other.value_type

    def __hash__(self):
        return hash(("map", self.value_type))


class JsonType(DataType):
    """JSON type accepts any JSON value."""
    def __init__(self):
        pass

    def __repr__(self):
        return "json"

    def __eq__(self, other):
        return isinstance(other, JsonType)

    def __hash__(self):
        return hash("json")


PRIMITIVE_TYPES = {"string", "int", "float", "bool", "date", "timestamp"}


def is_primitive_type(dtype: DataType) -> bool:
    """Check if a DataType is a primitive type."""
    return isinstance(dtype, PrimitiveType)


def parse_type(type_spec: Union[str, Dict], custom_aliases: Dict[str, str]) -> DataType:
    """Parse a type specification into a DataType object."""
    if isinstance(type_spec, str):
        resolved = resolve_alias(type_spec, custom_aliases)
        if resolved == "json":
            return JsonType()
        if resolved in PRIMITIVE_TYPES:
            return PrimitiveType(resolved)
        raise ValueError(f"Unknown primitive type: {type_spec}")

    elif isinstance(type_spec, dict):
        if "struct" in type_spec:
            spec = type_spec["struct"]
            if not isinstance(spec, dict) or "fields" not in spec:
                raise ValueError("struct must be a dict with 'fields' key")
            fields = spec["fields"]
            if not isinstance(fields, list):
                raise ValueError("struct fields must be a list")
            for f in fields:
                if "name" not in f or "type" not in f:
                    raise ValueError("Each struct field must have 'name' and 'type'")
                f["type"] = parse_type(f["type"], custom_aliases)
            return StructType(fields)

        elif "array" in type_spec:
            spec = type_spec["array"]
            if not isinstance(spec, dict) or "element" not in spec:
                raise ValueError("array must be a dict with 'element' key")
            element = parse_type(spec["element"], custom_aliases)
            return ArrayType(element)

        elif "map" in type_spec:
            spec = type_spec["map"]
            if not isinstance(spec, dict) or "key" not in spec or "value" not in spec:
                raise ValueError("map must be a dict with 'key' and 'value' keys")
            if spec["key"].lower() != "string":
                raise ValueError("Map key type must be string")
            value = parse_type(spec["value"], custom_aliases)
            return MapType("string", value)

        elif "json" in type_spec:
            return JsonType()

        else:
            raise ValueError(f"Unknown nested type: {type_spec}")

    else:
        raise ValueError(f"Invalid type specification: {type_spec}")


def load_schema_with_nested(schema_path: str, type_alias_file: Optional[str] = None) -> Tuple[List[Dict], Dict[str, DataType]]:
    """Load a schema file and return column definitions and type map."""
    with open(schema_path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    columns = schema.get("columns", [])
    if not columns:
        return [], {}

    # Load custom type aliases
    custom_aliases = {}
    if type_alias_file:
        with open(type_alias_file, "r", encoding="utf-8") as f:
            alias_data = json.load(f)
        custom_aliases = alias_data.get("aliases", {})

    column_types = {}
    for col in columns:
        name = col.get("name")
        if not name:
            raise ValueError("Column missing 'name'")
        type_spec = col.get("type")
        if type_spec is None:
            raise ValueError(f"Column '{name}' missing 'type'")

        dtype = parse_type(type_spec, custom_aliases)
        column_types[name] = dtype

    return columns, column_types
