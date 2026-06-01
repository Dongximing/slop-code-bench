#!/usr/bin/env python3
"""
Multi-Format CSV Merger

A command-line tool that ingests multiple heterogeneous files (CSV, TSV, JSON Lines, Parquet),
reconciles their schemas, and produces one sorted CSV output.

Supports nested types (struct, array, map) with canonical JSON output for nested columns.
"""

import argparse
import csv
import gzip
import heapq
import io
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

try:
    import pyarrow.parquet as pq
    import pyarrow as pa
except ImportError:
    print("Error: pyarrow is required for Parquet support", file=sys.stderr)
    sys.exit(2)


# Exit codes
EXIT_SUCCESS = 0
EXIT_TYPE_CAST_ERROR = 1
EXIT_ALIAS_CYCLE = 2
EXIT_KEY_NOT_PRIMITIVE = 3
EXIT_COMPRESSION_MISMATCH = 5
EXIT_NESTED_REQUIRES_SCHEMA = 6
EXIT_AMBIGUOUS_FORMAT = 2


# Type alias definitions
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
}

PRIMITIVE_TYPES = {"string", "int", "float", "bool", "date", "timestamp"}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Merge and sort multiple heterogeneous files"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output file path or '-' for stdout",
    )
    parser.add_argument(
        "--key",
        required=True,
        help="Comma-separated list of key columns (field paths) for sorting",
    )
    parser.add_argument(
        "--partition-by",
        help="Comma-separated list of field paths for partitioning output",
    )
    parser.add_argument(
        "--desc",
        action="store_true",
        help="Sort in descending order",
    )
    parser.add_argument(
        "--schema",
        help="JSON file with schema definition",
    )
    parser.add_argument(
        "--type-alias-file",
        help="JSON file with custom type aliases",
    )
    parser.add_argument(
        "--infer",
        choices=["strict", "loose"],
        default="strict",
        help="Inference mode when schema not provided (default: strict)",
    )
    parser.add_argument(
        "--schema-strategy",
        choices=["authoritative", "consensus", "union"],
        default="authoritative",
        help="Schema resolution strategy for conflicting types (default: authoritative)",
    )
    parser.add_argument(
        "--on-type-error",
        choices=["coerce-null", "fail", "keep-string"],
        default="coerce-null",
        help="Action on type casting error (default: coerce-null)",
    )
    parser.add_argument(
        "--memory-limit-mb",
        type=int,
        default=64,
        help="Memory limit in MB (default: 64)",
    )
    parser.add_argument(
        "--temp-dir",
        help="Directory for temporary files",
    )
    parser.add_argument(
        "--csv-quotechar",
        default='"',
        help="CSV quote character (default: '\"')",
    )
    parser.add_argument(
        "--csv-escapechar",
        help="CSV escape character (default: same as quotechar, doubled)",
    )
    parser.add_argument(
        "--csv-null-literal",
        default="",
        help="Literal for null values in output (default: empty string)",
    )
    parser.add_argument(
        "--input-format",
        choices=["auto", "csv", "tsv", "jsonl", "parquet"],
        default="auto",
        help="Input format (default: auto-detect)",
    )
    parser.add_argument(
        "--compression",
        choices=["auto", "none", "gzip"],
        default="auto",
        help="Compression format (default: auto-detect)",
    )
    parser.add_argument(
        "--parquet-row-group-bytes",
        type=int,
        default=128 * 1024 * 1024,  # 128 MB
        help="Advisory row group size for Parquet reading (default: 128MB)",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input files (CSV, TSV, JSON Lines, Parquet)",
    )
    return parser.parse_args()


# ============================================================================
# Type Alias Resolution
# ============================================================================

class TypeAliasResolver:
    """Resolve type aliases to canonical types."""

    def __init__(self, custom_aliases: Optional[Dict[str, str]] = None):
        self.aliases = dict(BUILTIN_ALIASES)
        if custom_aliases:
            self.aliases.update({k.lower(): v.lower() for k, v in custom_aliases.items()})

    def resolve(self, type_name: str, visited: Optional[Set[str]] = None) -> str:
        """Resolve a type name to its canonical form, detecting cycles."""
        if visited is None:
            visited = set()

        type_lower = type_name.lower()

        if type_lower in visited:
            print(
                f"Error: Cycle detected in type alias resolution for '{type_name}'",
                file=sys.stderr
            )
            sys.exit(EXIT_ALIAS_CYCLE)

        # If it's a primitive type or a nested type keyword, return as-is
        if type_lower in PRIMITIVE_TYPES or type_lower in ("struct", "array", "map", "json"):
            return type_lower

        # Check if it's an alias
        if type_lower in self.aliases:
            visited.add(type_lower)
            return self.resolve(self.aliases[type_lower], visited)

        # Check for array<T> or list<T> pattern
        array_match = re.match(r'^(array|list)<(.+)>$', type_lower)
        if array_match:
            inner_type = self.resolve(array_match.group(2), visited)
            return f"array<{inner_type}>"

        # Check for map<string,T> pattern
        map_match = re.match(r'^map<(string),(.+)>$', type_lower)
        if map_match:
            value_type = self.resolve(map_match.group(2), visited)
            return f"map<string,{value_type}>"

        # Unknown type - return as-is (will be validated later)
        return type_lower

    def is_alias_for_json(self, type_name: str) -> bool:
        """Check if a type name is an alias for json type."""
        resolved = self.resolve(type_name)
        return resolved == "json"


def load_type_aliases(alias_file: str) -> Dict[str, str]:
    """Load type aliases from a JSON file."""
    with open(alias_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("aliases", {})


# ============================================================================
# Nested Type Definitions
# ============================================================================

# Type definition can be:
# - str: primitive type name
# - dict with 'struct': {'struct': {'fields': [...]}}
# - dict with 'array': {'array': {'element': <type>}}
# - dict with 'map': {'map': {'key': 'string', 'value': <type>}}
# - dict with 'json': special flexible type

TypeDefinition = Union[str, Dict[str, Any]]


def is_nested_type(type_def: TypeDefinition) -> bool:
    """Check if a type definition represents a nested type."""
    if isinstance(type_def, str):
        return type_def.lower() == "json"
    if isinstance(type_def, dict):
        return any(k in type_def for k in ("struct", "array", "map", "json"))
    return False


def get_type_kind(type_def: TypeDefinition) -> str:
    """Get the kind of a type definition: primitive, struct, array, map, json."""
    if isinstance(type_def, str):
        type_lower = type_def.lower()
        if type_lower == "json":
            return "json"
        return "primitive"
    if isinstance(type_def, dict):
        if "struct" in type_def:
            return "struct"
        if "array" in type_def:
            return "array"
        if "map" in type_def:
            return "map"
        if "json" in type_def:
            return "json"
    return "primitive"


def is_primitive_type(type_def: TypeDefinition) -> bool:
    """Check if a type definition is a primitive type."""
    return get_type_kind(type_def) == "primitive"


# ============================================================================
# Field Path Parsing and Resolution
# ============================================================================

class FieldPath:
    """
    Parse and resolve field paths for nested types.

    Supports:
    - Simple field: "user"
    - Dot notation: "user.id", "items.0.sku"
    - Bracket notation: 'attrs["country"]', 'items[0]'
    """

    def __init__(self, path_str: str):
        self.original = path_str
        self.parts = self._parse(path_str)

    def _parse(self, path_str: str) -> List[Tuple[str, Optional[int]]]:
        """
        Parse a field path into components.
        Returns list of (field_name, array_index) tuples.
        array_index is None for non-array accesses.
        """
        parts = []
        i = 0
        current_field = []

        while i < len(path_str):
            c = path_str[i]

            if c == '.':
                if current_field:
                    parts.append((''.join(current_field), None))
                    current_field = []
                i += 1
            elif c == '[':
                if current_field:
                    parts.append((''.join(current_field), None))
                    current_field = []
                # Find matching ]
                j = i + 1
                if j < len(path_str) and path_str[j] == '"':
                    # String key: ["key"]
                    j += 1
                    key_start = j
                    while j < len(path_str) and path_str[j] != '"':
                        j += 1
                    key = path_str[key_start:j]
                    j += 1  # Skip closing quote
                    if j < len(path_str) and path_str[j] == ']':
                        j += 1
                    parts.append((key, None))  # Map key access
                    i = j
                else:
                    # Numeric index: [0]
                    while j < len(path_str) and path_str[j] != ']':
                        j += 1
                    index_str = path_str[i+1:j]
                    try:
                        index = int(index_str)
                    except ValueError:
                        raise ValueError(f"Invalid array index in path: {path_str}")
                    parts.append(('', index))  # Array index access
                    i = j + 1
            else:
                current_field.append(c)
                i += 1

        if current_field:
            parts.append((''.join(current_field), None))

        return parts

    def __str__(self) -> str:
        return self.original

    def __repr__(self) -> str:
        return f"FieldPath({self.original!r})"


def resolve_field_path(
    value: Any,
    path: FieldPath,
    type_def: TypeDefinition,
    alias_resolver: TypeAliasResolver,
) -> Tuple[Optional[Any], bool]:
    """
    Resolve a field path in a nested value.
    Returns (resolved_value, success) tuple.
    If the path doesn't exist or resolves to null, returns (None, True).
    If the path traverses a non-nested type incorrectly, returns (None, False).
    """
    current_value = value
    current_type = type_def

    for field_name, array_index in path.parts:
        kind = get_type_kind(current_type)

        if kind == "struct":
            struct_def = current_type["struct"]
            fields = struct_def.get("fields", [])
            field_found = False
            for field in fields:
                if field["name"] == field_name:
                    current_type = field["type"]
                    if isinstance(current_type, str):
                        current_type = alias_resolver.resolve(current_type)
                    if current_value is not None:
                        if isinstance(current_value, dict):
                            current_value = current_value.get(field_name)
                        else:
                            return None, False
                    field_found = True
                    break
            if not field_found:
                return None, True  # Field not in struct, treat as null

        elif kind == "array":
            array_def = current_type["array"]
            element_type = array_def.get("element", "string")
            if isinstance(element_type, str):
                element_type = alias_resolver.resolve(element_type)

            if array_index is not None:
                # Array index access
                current_type = element_type
                if current_value is not None:
                    if isinstance(current_value, list):
                        if 0 <= array_index < len(current_value):
                            current_value = current_value[array_index]
                        else:
                            return None, True  # Out of bounds, treat as null
                    else:
                        return None, False
            elif field_name:
                # Trying to access field on array - invalid
                return None, False

        elif kind == "map":
            map_def = current_type["map"]
            value_type = map_def.get("value", "string")
            if isinstance(value_type, str):
                value_type = alias_resolver.resolve(value_type)

            if field_name and array_index is None:
                # Map key access
                current_type = value_type
                if current_value is not None:
                    if isinstance(current_value, dict):
                        current_value = current_value.get(field_name)
                    else:
                        return None, False
            else:
                return None, False

        elif kind == "json":
            # JSON type - just navigate the value
            if current_value is not None:
                if field_name and isinstance(current_value, dict):
                    current_value = current_value.get(field_name)
                elif array_index is not None and isinstance(current_value, list):
                    if 0 <= array_index < len(current_value):
                        current_value = current_value[array_index]
                    else:
                        return None, True
                else:
                    return None, False
            # JSON type stays as json for further navigation
            current_type = "json"

        else:
            # Primitive type - can't navigate further
            return None, False

    # Final value must be primitive for key/partition
    final_kind = get_type_kind(current_type)
    if final_kind != "primitive" and final_kind != "json":
        return None, False

    return current_value, True


def parse_field_paths(paths_str: str) -> List[FieldPath]:
    """Parse comma-separated field paths."""
    return [FieldPath(p.strip()) for p in paths_str.split(",") if p.strip()]


def is_int(value: str) -> bool:
    """Check if string can be parsed as integer."""
    if not value:
        return False
    try:
        int(value)
        return True
    except ValueError:
        return False


def is_float(value: str) -> bool:
    """Check if string can be parsed as float."""
    if not value:
        return False
    try:
        float(value)
        return True
    except ValueError:
        return False


def is_bool(value: str) -> bool:
    """Check if string can be parsed as boolean."""
    if not value:
        return False
    return value.lower() in ("true", "false", "1", "0", "yes", "no")


def is_date(value: str) -> bool:
    """Check if string is a valid date in YYYY-MM-DD format."""
    if not value:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_timestamp(value: str) -> bool:
    """Check if string is a valid timestamp."""
    if not value:
        return False
    try:
        parse_timestamp(value)
        return True
    except ValueError:
        return False


def parse_timestamp(value: str) -> str:
    """Parse timestamp and normalize to ISO format."""
    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    raise ValueError(f"Cannot parse timestamp: {value}")


def infer_type_from_string(value: str, mode: str) -> Optional[str]:
    """Infer type from string value."""
    if not value:
        return None

    if is_timestamp(value):
        return "timestamp"
    if is_date(value):
        return "date"
    if is_bool(value):
        return "bool"
    if is_int(value):
        return "int"
    if is_float(value):
        return "float"
    return "string"


def infer_type_from_typed(value: Any) -> Optional[str]:
    """Infer type from typed value (from JSONL or Parquet)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        # For typed sources, check if string has temporal format
        if is_timestamp(value):
            return "timestamp"
        if is_date(value):
            return "date"
        return "string"
    return "string"


def resolve_type(types: List[Optional[str]], mode: str) -> str:
    """Resolve final type from list of inferred types."""
    non_null_types = [t for t in types if t is not None]

    if not non_null_types:
        return "string"

    unique_types = set(non_null_types)

    if len(unique_types) == 1:
        return non_null_types[0]

    if mode == "strict":
        return "string"
    else:
        # Loose mode: try to find compatible type
        numeric_types = {"int", "float"}
        if unique_types.issubset(numeric_types):
            return "float"
        temporal_types = {"date", "timestamp"}
        if unique_types.issubset(temporal_types):
            return "timestamp"
        return "string"


PARQUET_MAGIC = b'PAR1'


def detect_format_by_extension(filepath: str) -> Optional[str]:
    """Detect format from file extension."""
    base = filepath.lower()
    if base.endswith('.gz'):
        base = base[:-3]

    if base.endswith('.csv'):
        return 'csv'
    elif base.endswith('.tsv'):
        return 'tsv'
    elif base.endswith('.jsonl') or base.endswith('.ndjson'):
        return 'jsonl'
    elif base.endswith('.parquet'):
        return 'parquet'
    return None


def detect_compression_by_extension(filepath: str) -> Optional[str]:
    """Detect compression from file extension."""
    if filepath.lower().endswith('.gz'):
        return 'gzip'
    return 'none'


def detect_format_by_magic_bytes(filepath: str) -> Optional[str]:
    """Detect format by reading magic bytes."""
    try:
        with open(filepath, 'rb') as f:
            header = f.read(4)
            if header == PARQUET_MAGIC:
                return 'parquet'
    except (IOError, OSError):
        pass
    return None


def detect_input_format(
    filepath: str,
    format_arg: str,
    compression_arg: str,
) -> Tuple[str, str]:
    """
    Detect input format and compression.
    Returns (format, compression) tuple.
    """
    if format_arg != 'auto':
        detected_format = format_arg
    else:
        detected_format = detect_format_by_extension(filepath)
        if detected_format is None:
            magic_format = detect_format_by_magic_bytes(filepath)
            if magic_format == 'parquet':
                detected_format = 'parquet'
            else:
                print(
                    f"Error: Cannot determine format for '{filepath}'",
                    file=sys.stderr
                )
                sys.exit(EXIT_AMBIGUOUS_FORMAT)

    if compression_arg != 'auto':
        detected_compression = compression_arg
    else:
        detected_compression = detect_compression_by_extension(filepath)

    if detected_format == 'parquet' and detected_compression == 'gzip':
        print(
            f"Error: Compression mismatch for '{filepath}': "
            f"Parquet files should not have external compression",
            file=sys.stderr
        )
        sys.exit(EXIT_COMPRESSION_MISMATCH)

    return detected_format, detected_compression


def open_file(
    filepath: str,
    compression: str,
) -> io.IOBase:
    """Open file with appropriate compression handling."""
    if compression == 'gzip':
        try:
            return gzip.open(filepath, 'rt', encoding='utf-8', newline='')
        except gzip.BadGzipFile as e:
            print(
                f"Error: Compression mismatch for '{filepath}': {e}",
                file=sys.stderr
            )
            sys.exit(EXIT_COMPRESSION_MISMATCH)
    else:
        return open(filepath, 'r', encoding='utf-8', newline='')


def open_file_binary(
    filepath: str,
    compression: str,
) -> io.IOBase:
    """Open file in binary mode with appropriate compression handling."""
    if compression == 'gzip':
        return gzip.open(filepath, 'rb')
    else:
        return open(filepath, 'rb')


class FileInfo:
    """Information about an input file."""
    def __init__(
        self,
        filepath: str,
        format_type: str,
        compression: str,
    ):
        self.filepath = filepath
        self.format_type = format_type
        self.compression = compression
        self.is_typed = format_type in ('jsonl', 'parquet')


def read_csv_file(
    file_info: FileInfo,
    quotechar: str,
    escapechar: Optional[str],
) -> Generator[Tuple[Dict[str, str], Dict[str, Optional[str]]], None, None]:
    """
    Read CSV file and yield (row_dict, types_dict) tuples.
    types_dict contains inferred types for each value.
    """
    doublequote = escapechar is None

    try:
        with open_file(file_info.filepath, file_info.compression) as f:
            reader = csv.reader(
                f,
                delimiter=',',
                quotechar=quotechar,
                doublequote=doublequote,
                escapechar=escapechar if escapechar else None,
            )

            try:
                header = next(reader)
            except StopIteration:
                return

            for row in reader:
                row_dict = {}
                types_dict = {}
                for i, col_name in enumerate(header):
                    if i < len(row):
                        row_dict[col_name] = row[i]
                        types_dict[col_name] = None
                    else:
                        row_dict[col_name] = ""
                        types_dict[col_name] = None

                yield row_dict, types_dict
    except gzip.BadGzipFile as e:
        print(
            f"Error: Compression mismatch for '{file_info.filepath}': {e}",
            file=sys.stderr
        )
        sys.exit(EXIT_COMPRESSION_MISMATCH)


def read_tsv_file(
    file_info: FileInfo,
) -> Generator[Tuple[Dict[str, str], Dict[str, Optional[str]]], None, None]:
    """
    Read TSV file and yield (row_dict, types_dict) tuples.
    TSV has no quoting, tab delimiter.
    """
    try:
        with open_file(file_info.filepath, file_info.compression) as f:
            header_line = f.readline()
            if not header_line:
                return
            header = header_line.rstrip('\n').split('\t')

            for line_num, line in enumerate(f, start=2):
                line = line.rstrip('\n')
                if not line:
                    continue

                fields = line.split('\t')

                if len(fields) > len(header):
                    print(
                        f"Error: Line {line_num} in '{file_info.filepath}' has more "
                        f"fields than header (embedded tabs not allowed in TSV)",
                        file=sys.stderr
                    )
                    sys.exit(EXIT_COMPRESSION_MISMATCH)

                row_dict = {}
                types_dict = {}
                for i, col_name in enumerate(header):
                    if i < len(fields):
                        row_dict[col_name] = fields[i]
                        types_dict[col_name] = None
                    else:
                        row_dict[col_name] = ""
                        types_dict[col_name] = None

                yield row_dict, types_dict
    except gzip.BadGzipFile as e:
        print(
            f"Error: Compression mismatch for '{file_info.filepath}': {e}",
            file=sys.stderr
        )
        sys.exit(EXIT_COMPRESSION_MISMATCH)


def read_jsonl_file(
    file_info: FileInfo,
) -> Generator[Tuple[Dict[str, Any], Dict[str, Optional[str]]], None, None]:
    """
    Read JSON Lines file and yield (row_dict, types_dict) tuples.
    JSONL values come typed.
    """
    try:
        with open_file(file_info.filepath, file_info.compression) as f:
            for line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    print(
                        f"Error: Invalid JSON at line {line_num} in "
                        f"'{file_info.filepath}': {e}",
                        file=sys.stderr
                    )
                    sys.exit(EXIT_TYPE_CAST_ERROR)

                # Validate flat structure
                for key, value in obj.items():
                    if isinstance(value, (list, dict)):
                        print(
                            f"Error: Nested field '{key}' at line {line_num} in "
                            f"'{file_info.filepath}' (flat schema required)",
                            file=sys.stderr
                        )
                        sys.exit(EXIT_NESTED_FIELD_ERROR)

                types_dict = {}
                for key, value in obj.items():
                    types_dict[key] = infer_type_from_typed(value)

                yield obj, types_dict
    except gzip.BadGzipFile as e:
        print(
            f"Error: Compression mismatch for '{file_info.filepath}': {e}",
            file=sys.stderr
        )
        sys.exit(EXIT_COMPRESSION_MISMATCH)


def read_parquet_file(
    file_info: FileInfo,
    row_group_bytes: int,
) -> Generator[Tuple[Dict[str, Any], Dict[str, Optional[str]]], None, None]:
    """
    Read Parquet file row-group-wise and yield (row_dict, types_dict) tuples.
    Parquet values come typed.
    """
    try:
        parquet_file = pq.ParquetFile(file_info.filepath)
    except Exception as e:
        print(
            f"Error: Cannot open Parquet file '{file_info.filepath}': {e}",
            file=sys.stderr
        )
        sys.exit(EXIT_TYPE_CAST_ERROR)

    schema = parquet_file.schema_arrow

    for field in schema:
        if pa.types.is_struct(field.type) or pa.types.is_list(field.type):
            print(
                f"Error: Nested field '{field.name}' in Parquet file "
                f"'{file_info.filepath}' (flat schema required)",
                file=sys.stderr
            )
            sys.exit(EXIT_NESTED_FIELD_ERROR)

    num_row_groups = parquet_file.num_row_groups

    for rg_idx in range(num_row_groups):
        try:
            table = parquet_file.read_row_group(rg_idx)
        except Exception as e:
            print(
                f"Error: Cannot read row group {rg_idx} from "
                f"'{file_info.filepath}': {e}",
                file=sys.stderr
            )
            sys.exit(EXIT_TYPE_CAST_ERROR)

        for i in range(table.num_rows):
            row_dict = {}
            types_dict = {}

            for col_idx, field in enumerate(table.schema):
                col_name = field.name
                value = table[col_idx][i].as_py()

                if value is None:
                    row_dict[col_name] = None
                    types_dict[col_name] = None
                else:
                    row_dict[col_name] = value
                    types_dict[col_name] = infer_type_from_typed(value)

            yield row_dict, types_dict


def read_file(
    file_info: FileInfo,
    quotechar: str,
    escapechar: Optional[str],
    row_group_bytes: int,
) -> Generator[Tuple[Dict[str, Any], Dict[str, Optional[str]]], None, None]:
    """Read file based on format and yield (row_dict, types_dict) tuples."""
    if file_info.format_type == 'csv':
        yield from read_csv_file(file_info, quotechar, escapechar)
    elif file_info.format_type == 'tsv':
        yield from read_tsv_file(file_info)
    elif file_info.format_type == 'jsonl':
        yield from read_jsonl_file(file_info)
    elif file_info.format_type == 'parquet':
        yield from read_parquet_file(file_info, row_group_bytes)
    else:
        raise ValueError(f"Unknown format: {file_info.format_type}")


# Schema Resolution

def load_schema(schema_path: str) -> List[Tuple[str, str]]:
    """Load schema from JSON file."""
    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    columns = []
    for col in data["columns"]:
        columns.append((col["name"], col["type"]))

    return columns


class SchemaInferrer:
    """Infer schema from heterogeneous inputs."""

    def __init__(self, infer_mode: str, schema_strategy: str):
        self.infer_mode = infer_mode
        self.schema_strategy = schema_strategy
        self.all_columns: Set[str] = set()
        # column -> list of (type, is_typed, file_priority)
        self.column_types: Dict[str, List[Tuple[Optional[str], bool, int]]] = defaultdict(list)
        self.file_order = 0

    def add_file_info(
        self,
        file_info: FileInfo,
        rows_with_types: Iterator[Tuple[Dict[str, Any], Dict[str, Optional[str]]]],
    ):
        """Add type information from a file."""
        is_typed = file_info.is_typed
        priority = self.file_order
        self.file_order += 1

        for row_dict, types_dict in rows_with_types:
            for col_name, value in row_dict.items():
                self.all_columns.add(col_name)
                col_type = types_dict.get(col_name)
                self.column_types[col_name].append((col_type, is_typed, priority))

    def resolve_column_type(
        self,
        col_name: str,
    ) -> str:
        """Resolve final type for a column based on strategy."""
        type_info = self.column_types.get(col_name, [])

        if not type_info:
            return "string"

        if self.schema_strategy == "authoritative":
            typed_types = [(t, p) for t, is_typed, p in type_info if is_typed and t is not None]
            if typed_types:
                return typed_types[0][0]
            types = [t for t, _, _ in type_info if t is not None]
            return resolve_type(types, self.infer_mode)

        elif self.schema_strategy == "consensus":
            type_counts: Dict[str, int] = defaultdict(int)
            for t, _, _ in type_info:
                if t is not None:
                    type_counts[t] += 1

            if not type_counts:
                return "string"

            return max(type_counts.keys(), key=lambda x: type_counts[x])

        elif self.schema_strategy == "union":
            types = [t for t, _, _ in type_info if t is not None]
            if not types:
                return "string"

            unique_types = set(types)

            if len(unique_types) == 1:
                return types[0]

            numeric_types = {"int", "float"}
            if unique_types.issubset(numeric_types):
                return "float"

            temporal_types = {"date", "timestamp"}
            if unique_types.issubset(temporal_types):
                return "timestamp"

            return "string"

        return "string"

    def get_schema(self) -> List[Tuple[str, str]]:
        """Get final schema with lexicographic column order."""
        sorted_columns = sorted(self.all_columns)
        schema = []
        for col_name in sorted_columns:
            col_type = self.resolve_column_type(col_name)
            schema.append((col_name, col_type))
        return schema


def infer_schema_from_files(
    file_infos: List[FileInfo],
    infer_mode: str,
    schema_strategy: str,
    quotechar: str,
    escapechar: Optional[str],
    row_group_bytes: int,
) -> List[Tuple[str, str]]:
    """Infer schema from all input files."""
    inferrer = SchemaInferrer(infer_mode, schema_strategy)

    for file_info in file_infos:
        rows_with_types = read_file(file_info, quotechar, escapechar, row_group_bytes)
        inferrer.add_file_info(file_info, rows_with_types)

    return inferrer.get_schema()


def cast_value(
    value: Any,
    target_type: str,
    on_error: str,
    null_literal: str,
    source_is_typed: bool,
) -> Tuple[Optional[Any], bool]:
    """
    Cast value to target type.
    Returns (casted_value, success) tuple.
    """
    if value is None or (isinstance(value, str) and not value):
        return None, True

    try:
        if target_type == "string":
            return str(value), True

        elif target_type == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                return value, True
            if isinstance(value, float):
                if value.is_integer() and -2**63 <= value <= 2**63 - 1:
                    return int(value), True
                raise ValueError(f"Float {value} cannot be converted to int")
            str_val = str(value)
            int_val = int(str_val)
            return int_val, True

        elif target_type == "float":
            if isinstance(value, float):
                return value, True
            if isinstance(value, int) and not isinstance(value, bool):
                return float(value), True
            str_val = str(value)
            return float(str_val), True

        elif target_type == "bool":
            if isinstance(value, bool):
                return "true" if value else "false", True
            if isinstance(value, (int, float)):
                return "true" if value else "false", True
            str_val = str(value).lower()
            if str_val in ("true", "1", "yes"):
                return "true", True
            elif str_val in ("false", "0", "no"):
                return "false", True
            raise ValueError(f"Invalid boolean: {value}")

        elif target_type == "date":
            if isinstance(value, str):
                datetime.strptime(value, "%Y-%m-%d")
                return value, True
            raise ValueError(f"Cannot cast to date: {value}")

        elif target_type == "timestamp":
            if isinstance(value, str):
                return parse_timestamp(value), True
            raise ValueError(f"Cannot cast to timestamp: {value}")

        else:
            raise ValueError(f"Unknown type: {target_type}")

    except (ValueError, TypeError) as e:
        if on_error == "coerce-null":
            return None, True
        elif on_error == "fail":
            return None, False
        elif on_error == "keep-string":
            return str(value), True
        return None, True


def apply_schema_to_row(
    row_dict: Dict[str, Any],
    types_dict: Dict[str, Optional[str]],
    schema: List[Tuple[str, str]],
    on_error: str,
    null_literal: str,
    source_is_typed: bool,
    filepath: str,
) -> Optional[Dict[str, Optional[Any]]]:
    """
    Apply schema to a row, casting values as needed.
    Returns None if casting fails and on_error is 'fail'.
    """
    schema_cols = [col[0] for col in schema]
    schema_types = {col[0]: col[1] for col in schema}

    result = {}
    for col_name in schema_cols:
        if col_name in row_dict:
            raw_value = row_dict[col_name]

            if raw_value is None or (isinstance(raw_value, str) and not raw_value):
                result[col_name] = None
            else:
                casted, success = cast_value(
                    raw_value,
                    schema_types[col_name],
                    on_error,
                    null_literal,
                    source_is_typed,
                )
                if not success:
                    print(
                        f"Error: Cannot cast '{raw_value}' to "
                        f"{schema_types[col_name]} in column '{col_name}' "
                        f"in file '{filepath}'",
                        file=sys.stderr,
                    )
                    return None
                result[col_name] = casted
        else:
            result[col_name] = None

    return result


class ReverseComparator:
    """Wrapper that reverses comparison for descending sort."""
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value > other.value

    def __le__(self, other):
        return self.value >= other.value

    def __gt__(self, other):
        return self.value < other.value

    def __ge__(self, other):
        return self.value <= other.value

    def __eq__(self, other):
        return self.value == other.value


def make_sort_key(
    row: Dict[str, Optional[Any]],
    key_columns: List[str],
    descending: bool,
) -> Tuple:
    """Create sort key for a row."""
    key_parts = []
    for col in key_columns:
        value = row.get(col)
        if value is None:
            if descending:
                key_parts.append((0, 0))
            else:
                key_parts.append((2, 0))
        else:
            str_value = str(value)
            if descending:
                key_parts.append((1, ReverseComparator(str_value)))
            else:
                key_parts.append((1, str_value))

    return tuple(key_parts)


def write_sorted_batch(
    batch: List[Tuple[int, Dict[str, Optional[Any]]]],
    key_columns: List[str],
    descending: bool,
    temp_dir: str,
    schema: List[Tuple[str, str]],
) -> str:
    """Write a sorted batch to a temporary file."""
    def sort_key(item):
        order, row = item
        return (make_sort_key(row, key_columns, descending), order)

    batch.sort(key=sort_key)

    fd, temp_path = tempfile.mkstemp(suffix=".csv", dir=temp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for order, row in batch:
                row_data = []
                for col_name, col_type in schema:
                    value = row.get(col_name)
                    if value is None:
                        row_data.append("")
                    elif col_type == "bool":
                        row_data.append(str(value).lower())
                    else:
                        row_data.append(str(value))
                writer.writerow([order] + row_data)
    except Exception:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise

    return temp_path


def read_sorted_file(
    filepath: str,
    schema: List[Tuple[str, str]],
) -> Generator[Tuple[int, Dict[str, Optional[Any]]], None, None]:
    """Read a sorted temporary file."""
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            order = int(row[0])
            row_dict = {}
            for i, col in enumerate(schema):
                if i + 1 < len(row):
                    val = row[i + 1]
                    row_dict[col[0]] = val if val else None
                else:
                    row_dict[col[0]] = None
            yield order, row_dict


def external_sort(
    rows: Iterator[Tuple[int, Dict[str, Optional[Any]]]],
    key_columns: List[str],
    descending: bool,
    memory_limit_mb: int,
    temp_dir: str,
    schema: List[Tuple[str, str]],
    null_literal: str,
) -> Generator[Dict[str, Optional[Any]], None, None]:
    """Perform external merge sort."""
    memory_limit_bytes = memory_limit_mb * 1024 * 1024

    temp_files = []
    current_batch = []
    current_size = 0

    try:
        for order, row in rows:
            row_size = sum(
                len(str(v)) if v is not None else 0 for v in row.values()
            )
            row_size += len(row) * 8 + 8

            if current_size + row_size > memory_limit_bytes and current_batch:
                temp_file = write_sorted_batch(
                    current_batch,
                    key_columns,
                    descending,
                    temp_dir,
                    schema,
                )
                temp_files.append(temp_file)
                current_batch = []
                current_size = 0

            current_batch.append((order, row))
            current_size += row_size

        if current_batch:
            temp_file = write_sorted_batch(
                current_batch,
                key_columns,
                descending,
                temp_dir,
                schema,
            )
            temp_files.append(temp_file)

        yield from merge_sorted_files(
            temp_files, key_columns, descending, schema, null_literal
        )

    finally:
        for tf in temp_files:
            try:
                os.remove(tf)
            except OSError:
                pass


def merge_sorted_files(
    temp_files: List[str],
    key_columns: List[str],
    descending: bool,
    schema: List[Tuple[str, str]],
    null_literal: str,
) -> Generator[Dict[str, Optional[Any]], None, None]:
    """Merge sorted temporary files using heap."""
    if not temp_files:
        return

    if len(temp_files) == 1:
        for _, row in read_sorted_file(temp_files[0], schema):
            yield row
        return

    file_iters = [read_sorted_file(f, schema) for f in temp_files]
    heap = []

    def get_key(item):
        order, row, fidx = item
        return (make_sort_key(row, key_columns, descending), order)

    for i, it in enumerate(file_iters):
        try:
            order, row = next(it)
            heapq.heappush(heap, (get_key((order, row, i)), order, row, i))
        except StopIteration:
            pass

    while heap:
        _, order, row, fidx = heapq.heappop(heap)
        yield row

        try:
            next_order, next_row = next(file_iters[fidx])
            heapq.heappush(
                heap,
                (get_key((next_order, next_row, fidx)), next_order, next_row, fidx)
            )
        except StopIteration:
            pass


def write_output(
    rows: Iterator[Dict[str, Optional[Any]]],
    output_path: str,
    schema: List[Tuple[str, str]],
    null_literal: str,
    quotechar: str,
    escapechar: Optional[str],
) -> None:
    """Write output to file or stdout."""
    if output_path == "-":
        output_file = sys.stdout
        need_close = False
    else:
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir)

        fd, temp_path = tempfile.mkstemp(
            suffix=".csv",
            dir=output_dir if output_dir else None
        )
        output_file = os.fdopen(fd, "w", encoding="utf-8", newline="")
        need_close = True
        final_path = output_path
        output_path = temp_path

    try:
        doublequote = escapechar is None

        writer = csv.writer(
            output_file,
            quotechar=quotechar,
            doublequote=doublequote,
            escapechar=escapechar if escapechar else None,
            lineterminator="\n",
        )

        header = [col[0] for col in schema]
        writer.writerow(header)

        for row in rows:
            row_data = []
            for col_name, col_type in schema:
                value = row.get(col_name)
                if value is None:
                    row_data.append(null_literal)
                elif col_type == "bool":
                    row_data.append(str(value).lower())
                else:
                    row_data.append(str(value))
            writer.writerow(row_data)

    finally:
        if need_close:
            output_file.close()
            os.rename(output_path, final_path)


def main() -> int:
    args = parse_args()

    key_columns = [k.strip() for k in args.key.split(",")]

    quotechar = args.csv_quotechar
    escapechar = args.csv_escapechar

    temp_dir = args.temp_dir
    cleanup_temp = False
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="csv_merge_")
        cleanup_temp = True

    try:
        file_infos = []
        for filepath in args.inputs:
            format_type, compression = detect_input_format(
                filepath,
                args.input_format,
                args.compression,
            )
            file_infos.append(FileInfo(filepath, format_type, compression))

        if args.schema:
            schema = load_schema(args.schema)
        else:
            schema = infer_schema_from_files(
                file_infos,
                args.infer,
                args.schema_strategy,
                quotechar,
                escapechar,
                args.parquet_row_group_bytes,
            )

        schema_cols = set(col[0] for col in schema)
        for key_col in key_columns:
            if key_col not in schema_cols:
                print(
                    f"Error: Key column '{key_col}' not in schema",
                    file=sys.stderr,
                )
                return EXIT_KEY_NOT_IN_SCHEMA

        def row_generator():
            order = 0
            for file_info in file_infos:
                for row_dict, types_dict in read_file(
                    file_info,
                    quotechar,
                    escapechar,
                    args.parquet_row_group_bytes,
                ):
                    result = apply_schema_to_row(
                        row_dict,
                        types_dict,
                        schema,
                        args.on_type_error,
                        args.csv_null_literal,
                        file_info.is_typed,
                        file_info.filepath,
                    )
                    if result is None:
                        return EXIT_TYPE_CAST_ERROR
                    yield order, result
                    order += 1

        total_size = 0
        for filepath in args.inputs:
            try:
                total_size += os.path.getsize(filepath)
            except OSError:
                pass

        memory_bytes = args.memory_limit_mb * 1024 * 1024

        if total_size > memory_bytes * 0.5:
            sorted_rows = external_sort(
                row_generator(),
                key_columns,
                args.desc,
                args.memory_limit_mb,
                temp_dir,
                schema,
                args.csv_null_literal,
            )
        else:
            rows_list = list(row_generator())
            rows_list.sort(
                key=lambda x: (
                    make_sort_key(x[1], key_columns, args.desc),
                    x[0],
                )
            )
            sorted_rows = (row for _, row in rows_list)

        write_output(
            sorted_rows,
            args.output,
            schema,
            args.csv_null_literal,
            quotechar,
            escapechar,
        )

        return EXIT_SUCCESS

    finally:
        if cleanup_temp:
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
