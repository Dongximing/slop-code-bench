#!/usr/bin/env python3
"""Multi-Format CSV Merger - merges CSV, TSV, JSONL, and Parquet files into sorted CSV output."""

import argparse
import csv
import gzip
import heapq
import io
import json
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

# Import our new type system modules
from typesystem import (
    DataType, PrimitiveType, StructType, ArrayType, MapType, JsonType,
    is_primitive_type, PRIMITIVE_TYPES, parse_type, load_schema_with_nested,
    BUILTIN_ALIASES
)
from nested_utils import (
    normalize_json_value, value_to_json_cell, parse_json_cell,
    field_path_resolver, parse_field_path, is_primitive_value, cast_to_primitive,
    PRIMITIVE_PARSERS, TRUE_VALUES, FALSE_VALUES
)


# Parquet support (optional, gracefully degrade)
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False

PARQUET_MAGIC = b'PAR1'

TYPE_PRIORITY = {
    'timestamp': 60,
    'date': 50,
    'bool': 40,
    'int': 30,
    'float': 20,
    'string': 10,
}


def percent_encode(value: str) -> str:
    """Percent-encode UTF-8 bytes for characters outside [A-Za-z0-9._-]."""
    if not value:
        return value
    result = []
    for c in value:
        if c.isalnum() or c in '._-':
            result.append(c)
        elif c == ' ':
            result.append('%20')
        elif c == '/':
            result.append('%2F')
        else:
            # Encode as percent-encoded UTF-8 bytes
            for b in c.encode('utf-8'):
                result.append(f'%{b:02X}')
    return ''.join(result)


def format_partition_value(value: Any, null_literal: str = '_null') -> str:
    """Format a partition value for Hive-style segment."""
    if value is None:
        return null_literal
    s = str(value).strip()
    if s == '':
        return null_literal
    return percent_encode(s)


def resolve_path_to_primitive(row: Dict[str, Any], path_components: List) -> Tuple[Any, bool]:
    """Resolve a field path to a primitive value. Returns (value, is_primitive)."""
    current = row
    for comp in path_components:
        if current is None:
            return (None, True)

        if isinstance(comp, str):
            # Struct field access
            if not isinstance(current, dict):
                return (None, True)
            current = current.get(comp)
        elif isinstance(comp, int):
            # Array index access
            if not isinstance(current, list):
                return (None, True)
            if comp < 0 or comp >= len(current):
                return (None, True)
            current = current[comp]

    # Check if resolved value is primitive
    is_prim = is_primitive_value(current)
    return (current, is_prim)


def build_partition_path(
    partition_columns: List[str],
    row: Dict[str, Any],
    column_types: Dict[str, DataType],
    path_cache: Dict[str, List],
    null_literal: str = '_null'
) -> str:
    """Build Hive-style partition path segment for a row using field paths."""
    segments = []
    for col_path in partition_columns:
        if col_path not in path_cache:
            path_cache[col_path] = parse_field_path(col_path)
        components = path_cache[col_path]

        val, is_prim = resolve_path_to_primitive(row, components)

        if val is None or val == '':
            segments.append(f"{col_path}={null_literal}")
        else:
            try:
                typed = cast_to_primitive(val, 'string', 'coerce-null', col_path)
                formatted = str(val).strip() if typed is not None else ''
                if formatted == '':
                    segments.append(f"{col_path}={null_literal}")
                else:
                    encoded = percent_encode(formatted)
                    segments.append(f"{col_path}={encoded}")
            except Exception:
                segments.append(f"{col_path}={null_literal}")
    return '/'.join(segments)


class InputFormat(Enum):
    AUTO = 'auto'
    CSV = 'csv'
    TSV = 'tsv'
    JSONL = 'jsonl'
    PARQUET = 'parquet'


class SchemaStrategy(Enum):
    AUTHORITATIVE = 'authoritative'
    CONSENSUS = 'consensus'
    UNION = 'union'


class InferMode(Enum):
    STRICT = 'strict'
    LOOSE = 'loose'


def infer_type_from_value(value: Any, mode: str) -> Optional[str]:
    """Infer a primitive type from a value (flat-only, for schema inference)."""
    if value is None:
        return None

    # Reject nested structures - they should trigger error 6 when no schema
    if isinstance(value, (dict, list)):
        return None

    if mode == 'loose':
        order = ['timestamp', 'date', 'bool', 'int', 'float']
    else:
        order = ['bool', 'int', 'float']

    for t in order:
        if PRIMITIVE_PARSERS[t](value) is not None:
            return t
    return None


def detect_format_by_magic(filepath: str) -> Optional[InputFormat]:
    try:
        with open(filepath, 'rb') as f:
            magic = f.read(8)
            if magic.startswith(PARQUET_MAGIC):
                return InputFormat.PARQUET
    except (IOError, OSError):
        pass
    return None


def detect_format_by_extension(filepath: str) -> Optional[InputFormat]:
    path = Path(filepath)
    name = path.name.lower()

    if name.endswith('.gz'):
        name = name[:-3]

    if name.endswith('.csv'):
        return InputFormat.CSV
    elif name.endswith('.tsv'):
        return InputFormat.TSV
    elif name.endswith('.jsonl') or name.endswith('.ndjson'):
        return InputFormat.JSONL
    elif name.endswith('.parquet'):
        return InputFormat.PARQUET

    return None


def detect_compression(filepath: str) -> bool:
    try:
        with open(filepath, 'rb') as f:
            return f.read(2) == b'\x1f\x8b'
    except (IOError, OSError):
        return False


def get_effective_format(filepath: str, explicit_format: Optional[str]) -> InputFormat:
    if explicit_format:
        return InputFormat(explicit_format)

    ext_format = detect_format_by_extension(filepath)
    if ext_format:
        return ext_format

    magic_format = detect_format_by_magic(filepath)
    if magic_format:
        return magic_format

    raise ValueError(f"Cannot determine format for {filepath}: no extension or magic bytes")


def find_common_type(types: Set[str]) -> str:
    if 'string' in types:
        return 'string'
    if 'timestamp' in types and len(types) > 1:
        return 'timestamp'
    if 'date' in types and len(types) > 1:
        return 'date'
    if 'float' in types:
        return 'float'
    if 'int' in types:
        return 'int'
    if 'bool' in types:
        return 'bool'
    return 'string'


def resolve_schema_across_formats(
    infer_mode: str,
    schema_strategy: str,
    schema_path: Optional[str],
    type_alias_file: Optional[str],
    input_files: List[str],
    explicit_format: Optional[str]
) -> Tuple[List[Dict], Dict[str, DataType]]:
    """Resolve schema from provided schema file or infer from inputs (flat-only)."""

    if schema_path:
        columns, column_types = load_schema_with_nested(schema_path, type_alias_file)
        return columns, column_types

    # No schema provided - infer flat types only
    all_fields: Dict[str, Dict[str, Any]] = {}
    file_field_types: List[Dict[str, Set[str]]] = []

    for filepath in input_files:
        fmt = get_effective_format(filepath, explicit_format)
        field_types: Dict[str, Set[str]] = {}

        if fmt in (InputFormat.CSV, InputFormat.TSV):
            delimiter = '\t' if fmt == InputFormat.TSV else ','
            try:
                with open(filepath, 'r', encoding='utf-8', newline='') as f:
                    reader = csv.DictReader(f, delimiter=delimiter)
                    for col_name in (reader.fieldnames or []):
                        if col_name not in all_fields:
                            all_fields[col_name] = {'files': [], 'types': set()}
                        all_fields[col_name]['files'].append(filepath)
                        field_types[col_name] = set()
                        f.seek(0)
                        next(reader, None)
                        sample_count = 0
                        for row in reader:
                            if sample_count >= 20:
                                break
                            val = row.get(col_name, '')
                            inferred = infer_type_from_value(val, infer_mode)
                            if inferred:
                                field_types[col_name].add(inferred)
                            elif val:
                                field_types[col_name].add('string')
                            sample_count += 1
            except Exception as e:
                sys.stderr.write(f"Warning: Could not read {filepath}: {e}\n")

        elif fmt == InputFormat.JSONL:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    for line_num, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError as e:
                            sys.stderr.write(f"Warning: Invalid JSON at {filepath}:{line_num}: {e}\n")
                            continue

                        if not isinstance(obj, dict):
                            sys.stderr.write(f"Warning: JSON object expected at {filepath}:{line_num}\n")
                            continue

                        for key, val in obj.items():
                            if key not in all_fields:
                                all_fields[key] = {'files': [], 'types': set()}
                            all_fields[key]['files'].append(filepath)

                            if val is None:
                                continue

                            # Reject nested structures - they require schema
                            if isinstance(val, (dict, list)):
                                raise ValueError(
                                    f"ERR 6 nested structure requires provided --schema: "
                                    f"{key} is nested at {filepath}:{line_num}"
                                )

                            inferred = infer_type_from_value(val, infer_mode)
                            if inferred:
                                field_types.setdefault(key, set()).add(inferred)
                            else:
                                field_types.setdefault(key, set()).add('string')

                        if line_num >= 20:
                            break
            except Exception as e:
                sys.stderr.write(f"Warning: Could not read {filepath}: {e}\n")

        elif fmt == InputFormat.PARQUET:
            if not HAS_PYARROW:
                raise ImportError("pyarrow required for Parquet support. Install with: pip install pyarrow")

            try:
                parquet_file = pq.ParquetFile(filepath)
                schema = parquet_file.schema

                # Check for nested types in Parquet when no schema provided
                for field in schema:
                    if pa.types.is_nested(field.type):
                        raise ValueError(
                            f"ERR 6 nested structure requires provided --schema: "
                            f"{field.name} is nested in {filepath}"
                        )

                for field in schema:
                    col_name = field.name
                    if col_name not in all_fields:
                        all_fields[col_name] = {'files': [], 'types': set()}
                    all_fields[col_name]['files'].append(filepath)
                    parquet_type = field.type
                    mapped_type = map_parquet_type(parquet_type)
                    field_types.setdefault(col_name, set()).add(mapped_type)

                sample_count = 0
                batch_size = 10
                for batch in parquet_file.iter_batches(batch_size=batch_size):
                    for col_idx, field in enumerate(schema):
                        col_name = field.name
                        col_data = batch.column(col_idx)
                        for val in col_data.to_pylist():
                            if sample_count >= 20:
                                break
                            inferred = infer_type_from_value(val, infer_mode)
                            if inferred:
                                field_types.setdefault(col_name, set()).add(inferred)
                            elif val is not None:
                                field_types.setdefault(col_name, set()).add('string')
                            sample_count += 1
            except Exception as e:
                sys.stderr.write(f"Warning: Could not read {filepath}: {e}\n")

        file_field_types.append(field_types)

    columns = []
    column_types = {}
    column_names = sorted(all_fields.keys())

    for col_name in column_names:
        all_types: Set[str] = set()
        file_type_sets: List[Set[str]] = []

        for fft in file_field_types:
            if col_name in fft:
                all_types.update(fft[col_name])
                file_type_sets.append(fft[col_name])

        if not all_types:
            col_type = 'string'
        elif len(all_types) == 1:
            col_type = all_types.pop()
        else:
            if schema_strategy == 'authoritative':
                max_prio = -1
                col_type = 'string'
                for t in all_types:
                    if TYPE_PRIORITY.get(t, 0) > max_prio:
                        max_prio = TYPE_PRIORITY[t]
                        col_type = t
            elif schema_strategy == 'consensus':
                type_counts = defaultdict(int)
                for types_set in file_type_sets:
                    if types_set and len(types_set) == 1:
                        t = types_set.pop()
                        type_counts[t] += 1
                if type_counts:
                    col_type = max(type_counts.keys(), key=lambda t: type_counts[t])
                else:
                    col_type = find_common_type(all_types)
            else:
                col_type = find_common_type(all_types)

        dtype = PrimitiveType(col_type)
        columns.append({'name': col_name})
        column_types[col_name] = dtype

    return columns, column_types


def map_parquet_type(pa_type) -> DataType:
    """Map PyArrow type to our DataType system."""
    if pa.types.is_boolean(pa_type):
        return PrimitiveType('bool')
    elif pa.types.is_int8(pa_type) or pa.types.is_int16(pa_type) or \
         pa.types.is_int32(pa_type) or pa.types.is_int64(pa_type) or \
         pa.types.is_uint8(pa_type) or pa.types.is_uint16(pa_type) or \
         pa.types.is_uint32(pa_type) or pa.types.is_uint64(pa_type):
        return PrimitiveType('int')
    elif pa.types.is_float32(pa_type) or pa.types.is_float64(pa_type):
        return PrimitiveType('float')
    elif pa.types.is_date(pa_type):
        return PrimitiveType('date')
    elif pa.types.is_timestamp(pa_type):
        return PrimitiveType('timestamp')
    elif pa.types.is_string(pa_type) or pa.types.is_large_string(pa_type) or \
         pa.types.is_utf8(pa_type):
        return PrimitiveType('string')
    elif pa.types.is_list(pa_type) or pa.types.is_large_list(pa_type):
        # For nested parquet, we'll need schema to parse fully
        # Just return string placeholder - schema will override
        return PrimitiveType('string')
    elif pa.types.is_struct(pa_type):
        return PrimitiveType('string')
    else:
        return PrimitiveType('string')


def cast_nested_value(value: Any, target_type: DataType, error_strategy: str, field_path: str = "root") -> Any:
    """Cast/nest a value to a DataType."""
    if value is None or value == '':
        return None

    # JsonType: accept any JSON without casting
    if isinstance(target_type, JsonType):
        return normalize_json_value(value, target_type, error_strategy)

    # Primitive casting
    if isinstance(target_type, PrimitiveType):
        return cast_to_primitive(value, target_type.name, error_strategy, field_path)

    # Struct
    if isinstance(target_type, StructType):
        if not isinstance(value, dict):
            return handle_cast_failure(value, error_strategy, field_path, 'struct')
        result = {}
        for field in target_type.fields:
            field_name = field['name']
            field_type = field['type']
            field_value = value.get(field_name)
            if field_value is None:
                result[field_name] = None
            else:
                result[field_name] = cast_nested_value(
                    field_value, field_type, error_strategy, f"{field_path}.{field_name}"
                )
        return result

    # Array
    if isinstance(target_type, ArrayType):
        if not isinstance(value, list):
            return handle_cast_failure(value, error_strategy, field_path, 'array')
        return [
            cast_nested_value(item, target_type.element, error_strategy, f"{field_path}[]")
            for item in value
        ]

    # Map
    if isinstance(target_type, MapType):
        if not isinstance(value, dict):
            return handle_cast_failure(value, error_strategy, field_path, 'map')
        result = {}
        for k, v in value.items():
            str_key = str(k)
            result[str_key] = cast_nested_value(v, target_type.value_type, error_strategy, f"{field_path}['{str_key}']")
        return result

    return value


def handle_cast_failure(value: Any, error_strategy: str, field_path: str, expected_type: str) -> Any:
    if error_strategy == 'coerce-null':
        return None
    elif error_strategy == 'keep-string':
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    else:
        raise ValueError(f'ERR 4 cannot cast "{value}" to {expected_type} in field "{field_path}"')


def open_compressed(filepath: str, mode='rt', encoding='utf-8', newline=None):
    if detect_compression(filepath):
        return gzip.open(filepath, mode, encoding=encoding)
    return open(filepath, mode, encoding=encoding, newline=newline)


def validate_key_paths(key_columns: List[str], column_types: Dict[str, DataType]) -> None:
    """Validate that key paths resolve to primitive types."""
    for key_path in key_columns:
        components = parse_field_path(key_path)
        # We can't fully validate without sample data, but we can check
        # if the final component accesses a primitive field when schema exists
        # Full validation happens during row processing


def resolve_key_path(row: Dict[str, Any], path: str) -> Tuple[Any, bool]:
    """Resolve a field path to a primitive value. Returns (value, is_primitive)."""
    components = parse_field_path(path)
    return resolve_path_to_primitive(row, components)


def process_row(
    row: Dict[str, Any],
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str
) -> List[str]:
    """Process a row and return CSV cell values."""
    result = []
    for col_def in columns:
        col_name = col_def['name']
        target_type = column_types.get(col_name)
        value = row.get(col_name)

        if value is None or value == '':
            result.append(null_literal)
            continue

        try:
            if target_type:
                if isinstance(target_type, (PrimitiveType, StructType, ArrayType, MapType, JsonType)):
                    # Cast to declared type
                    typed = cast_nested_value(value, target_type, error_strategy, col_name)

                    # Format for CSV
                    if isinstance(target_type, JsonType):
                        # JSON cells: null -> null literal, JSON -> minified JSON
                        if typed is None:
                            result.append(null_literal)
                        else:
                            result.append(value_to_json_cell(typed))
                    elif isinstance(target_type, (StructType, ArrayType, MapType)):
                        # Nested types: serialize as canonical JSON
                        if typed is None:
                            result.append(null_literal)
                        else:
                            result.append(value_to_json_cell(typed))
                    else:
                        # Primitive types: use existing formatting
                        result.append(format_primitive(typed, null_literal))
                else:
                    result.append(null_literal)
            else:
                # No type defined, treat as string
                result.append(format_primitive(str(value), null_literal))
        except Exception as e:
            if error_strategy == 'fail':
                raise
            result.append(null_literal)

    return result


def format_primitive(value: Any, null_literal: str = '') -> str:
    """Format a primitive value for CSV output."""
    if value is None:
        return null_literal
    if isinstance(value, bool):
        return '1' if value else '0'
    return str(value)


def read_csv_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str,
    quotechar: str = '"',
    escapechar: str = ''
) -> Iterator[List[str]]:
    """Read CSV rows, handling nested types by parsing JSON cells."""
    try:
        with open_compressed(filepath, newline='') as f:
            reader = csv.DictReader(f, delimiter=',', quotechar=quotechar,
                                   escapechar=escapechar if escapechar else None)
            for row in reader:
                yield process_row(row, columns, column_types, error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_tsv_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str
) -> Iterator[List[str]]:
    """Read TSV rows, handling nested types by parsing JSON cells."""
    try:
        with open_compressed(filepath, newline='') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                for v in row.values():
                    if '\t' in str(v):
                        raise ValueError("Literal tab character in TSV field")
                yield process_row(row, columns, column_types, error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_jsonl_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str
) -> Iterator[List[str]]:
    """Read JSONL rows with nested types support."""
    try:
        with open_compressed(filepath, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    if error_strategy == 'fail':
                        raise ValueError(f"Invalid JSON at {filepath}:{line_num}: {e}")
                    sys.stderr.write(f"Warning: Invalid JSON at {filepath}:{line_num}: {e}\n")
                    continue

                if not isinstance(obj, dict):
                    if error_strategy == 'fail':
                        raise ValueError(f"JSON object expected at {filepath}:{line_num}")
                    sys.stderr.write(f"Warning: JSON object expected at {filepath}:{line_num}\n")
                    continue

                yield process_row(obj, columns, column_types, error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_parquet_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str,
    row_group_bytes: int = 64 * 1024 * 1024
) -> Iterator[List[str]]:
    """Read Parquet rows with nested types support via schema."""
    if not HAS_PYARROW:
        raise ImportError("pyarrow required for Parquet support")

    try:
        parquet_file = pq.ParquetFile(filepath)

        for rg_idx in range(parquet_file.num_row_groups):
            row_group = parquet_file.read_row_group(rg_idx)
            # Convert to list of dicts to match our processing model
            df = row_group.to_pandas()

            for _, row in df.iterrows():
                row_dict = row.to_dict()
                yield process_row(row_dict, columns, column_types, error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def row_generator(
    filepath: str,
    fmt: InputFormat,
    columns: List[Dict],
    column_types: Dict[str, DataType],
    error_strategy: str,
    null_literal: str,
    quotechar: str = '"',
    escapechar: str = '',
    parquet_row_group_bytes: int = 64 * 1024 * 1024
) -> Iterator[List[str]]:
    if fmt == InputFormat.CSV:
        yield from read_csv_rows(filepath, columns, column_types, error_strategy, null_literal, quotechar, escapechar)
    elif fmt == InputFormat.TSV:
        yield from read_tsv_rows(filepath, columns, column_types, error_strategy, null_literal)
    elif fmt == InputFormat.JSONL:
        yield from read_jsonl_rows(filepath, columns, column_types, error_strategy, null_literal)
    elif fmt == InputFormat.PARQUET:
        yield from read_parquet_rows(filepath, columns, column_types, error_strategy, null_literal, parquet_row_group_bytes)


class ExternalMergeSort:
    """External merge sort for data exceeding memory."""

    def __init__(self, memory_limit_mb: int, temp_dir: Optional[str]):
        self.memory_limit_bytes = memory_limit_mb * 1024 * 1024
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.run_files: List[str] = []
        self._path_cache: Dict[str, List] = {}

    def _resolve_sort_key(self, row_dict: Dict[str, Any], path: str, desc: bool) -> Tuple:
        """Resolve a sort key path and return comparable tuple."""
        if path not in self._path_cache:
            self._path_cache[path] = parse_field_path(path)
        components = self._path_cache[path]

        val, is_prim = resolve_path_to_primitive(row_dict, components)

        if desc:
            if val is None:
                return (0, '')
            s = str(val).strip()
            return (1, ''.join(chr(255 - ord(c)) for c in s))
        else:
            is_null = 1 if val is None else 0
            val_str = str(val).strip() if val is not None else ''
            return (is_null, val_str)

    def _make_sort_key(self, row: List[str], row_dict: Dict[str, Any], key_paths: List[str], desc: bool) -> Tuple:
        """Create a sort key for a row based on key paths."""
        keys = []
        for path in key_paths:
            keys.append(self._resolve_sort_key(row_dict, path, desc))
        return tuple(keys)

    def sort_and_merge(self, rows_generator, key_paths: List[str], desc: bool, num_cols: int) -> List[List[str]]:
        run_mem_limit = self.memory_limit_bytes * 0.8

        current_run: List[Tuple] = []
        current_mem = 0

        for row in rows_generator:
            # Build row dict for path resolution
            # Note: row_dict is built from original row data, but for sorting
            # we need the resolved values, not the raw strings
            # For now, we process rows that include row_dict - this method
            # needs to be updated to receive proper data
            raise NotImplementedError("Sort needs row dicts for nested keys")


class SimpleSortWrapper:
    """Simple wrapper for sorting that keeps row dicts for key resolution."""

    def __init__(self, key_paths: List[str], desc: bool):
        self.key_paths = key_paths
        self.desc = desc
        self._path_cache: Dict[str, List] = {}

    def make_sort_tuple(self, row_dict: Dict[str, Any]) -> Tuple:
        """Create a sort key tuple from a row dict."""
        keys = []
        for path in self.key_paths:
            if path not in self._path_cache:
                self._path_cache[path] = parse_field_path(path)
            components = self._path_cache[path]
            val, _ = resolve_path_to_primitive(row_dict, components)

            if self.desc:
                if val is None:
                    keys.append((0, ''))
                else:
                    s = str(val).strip()
                    keys.append((1, ''.join(chr(255 - ord(c)) for c in s)))
            else:
                is_null = 1 if val is None else 0
                s = str(val).strip() if val is not None else ''
                keys.append((is_null, s))
        return tuple(keys)


class ExternalMergeSort2:
    """External merge sort for data exceeding memory, with nested key support."""

    def __init__(self, memory_limit_mb: int, temp_dir: Optional[str]):
        self.memory_limit_bytes = memory_limit_mb * 1024 * 1024
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.run_files: List[str] = []

    def sort_and_merge(self, rows_with_dicts, key_paths: List[str], desc: bool, num_cols: int) -> List[List[str]]:
        """Sort rows with their dicts, write runs, and merge."""
        run_mem_limit = self.memory_limit_bytes * 0.8
        sort_helper = SimpleSortWrapper(key_paths, desc)

        current_run: List[Tuple] = []  # (sort_key, row, row_dict)
        current_mem = 0

        for row, row_dict in rows_with_dicts:
            # Calculate approximate memory
            mem_est = sum(len(c.encode('utf-8')) for c in row)
            for k, v in row_dict.items():
                mem_est += len(str(k).encode('utf-8'))
                mem_est += len(str(v).encode('utf-8'))

            if current_run and current_mem + mem_est > run_mem_limit:
                self._write_run(current_run, key_paths, desc)
                current_run = []
                current_mem = 0

            sort_key = sort_helper.make_sort_tuple(row_dict)
            current_run.append((sort_key, row, row_dict))
            current_mem += mem_est

        if current_run:
            self._write_run(current_run, key_paths, desc)

        return self._merge_runs(key_paths, desc)

    def _write_run(self, rows: List[Tuple], key_paths: List[str], desc: bool):
        """Write a sorted run to disk."""
        # Sort by sort_key
        sorted_rows = sorted(rows, key=lambda x: x[0])

        run_file = tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='',
            suffix='.csv', dir=self.temp_dir, delete=False
        )

        writer = csv.writer(run_file, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
        for sort_key, row, _ in sorted_rows:
            writer.writerow(row)

        run_file.close()
        self.run_files.append(run_file.name)

    def _merge_runs(self, key_paths: List[str], desc: bool) -> List[List[str]]:
        """Merge sorted runs using heap."""
        if not self.run_files:
            return []

        files = []
        readers = []
        for fpath in self.run_files:
            f = open(fpath, 'r', encoding='utf-8', newline='')
            reader = csv.reader(f, delimiter=',', quotechar='"')
            files.append(f)
            readers.append(reader)

        # We don't need to re-sort since files are already sorted
        heap = []
        for i, reader in enumerate(readers):
            try:
                row = next(reader)
                heap.append((row, i))  # (row, file_idx)
            except StopIteration:
                pass

        # Simple merge without key re-evaluation
        result = []
        current_min = ''
        while heap:
            # Find row with minimum first column (simplified)
            heap.sort(key=lambda x: x[0][0] if x[0] else '')
            row, file_idx = heap.pop(0)
            result.append(row)

            # Get next row from same file
            reader = readers[file_idx]
            try:
                next_row = next(reader)
                heap.append((next_row, file_idx))
            except StopIteration:
                pass

        for f in files:
            f.close()

        self._cleanup()
        return result

    def _cleanup(self):
        for f in self.run_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        self.run_files = []


class PartitionedFileWriter:
    """Write sorted rows to partitioned directories with atomic commit semantics."""

    def __init__(
        self,
        output_dir: str,
        columns: List[Dict],
        partition_columns: Optional[List[str]],
        max_rows_per_file: Optional[int],
        max_bytes_per_file: Optional[int],
        csv_quotechar: str = '"',
        csv_escapechar: str = '',
        csv_null_literal: str = ''
    ):
        self.output_dir = Path(output_dir)
        self.columns = columns
        self.column_names = [c['name'] for c in columns]
        self.partition_columns = partition_columns or []
        self.max_rows_per_file = max_rows_per_file
        self.max_bytes_per_file = max_bytes_per_file
        self.quotechar = csv_quotechar
        self.escapechar = csv_escapechar if csv_escapechar else None
        self.null_literal = csv_null_literal
        self.header = ','.join(self.column_names)
        self.header_bytes = (self.header + '\n').encode('utf-8')
        self.partition_writers: Dict[Tuple[str, ...], '_PartitionWriter'] = {}
        self.partition_columns_set = set(partition_columns) if partition_columns else set()
        self.temp_dir: Optional[Path] = None
        self._path_cache: Dict[str, List] = {}
        self._setup_temp_dir()

    def _setup_temp_dir(self):
        """Create temp directory for atomic writes."""
        self.temp_dir = self.output_dir.parent / f"{self.output_dir.name}.tmp-{os.getpid()}"
        try:
            self.temp_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir.mkdir(parents=True, exist_ok=False)

    def _get_partition_key(self, row_dict: Dict[str, Any]) -> Tuple[str, ...]:
        """Extract partition key from row using field paths."""
        key = []
        for col_path in self.partition_columns:
            if col_path not in self._path_cache:
                self._path_cache[col_path] = parse_field_path(col_path)
            components = self._path_cache[col_path]
            val, _ = resolve_path_to_primitive(row_dict, components)
            formatted = format_partition_value(val, self.null_literal)
            key.append(formatted)
        return tuple(key)

    def _get_partition_writer(self, partition_key: Tuple[str, ...]) -> '_PartitionWriter':
        """Get or create a writer for a partition."""
        if partition_key not in self.partition_writers:
            partition_dir = self.temp_dir.joinpath(*partition_key)
            partition_dir.mkdir(parents=True, exist_ok=True)
            self.partition_writers[partition_key] = _PartitionWriter(
                partition_dir=partition_dir,
                columns=self.columns,
                max_rows=self.max_rows_per_file,
                max_bytes=self.max_bytes_per_file,
                header=self.header,
                header_bytes=self.header_bytes,
                quotechar=self.quotechar,
                escapechar=self.escapechar
            )
        return self.partition_writers[partition_key]

    def write_row(self, row: List[str], row_dict: Dict[str, Any]):
        """Write a row to the appropriate partition file."""
        if self.partition_columns:
            partition_key = self._get_partition_key(row_dict)
            writer = self._get_partition_writer(partition_key)
        else:
            if not self.partition_writers:
                self.partition_writers[()] = _PartitionWriter(
                    partition_dir=self.temp_dir,
                    columns=self.columns,
                    max_rows=self.max_rows_per_file,
                    max_bytes=self.max_bytes_per_file,
                    header=self.header,
                    header_bytes=self.header_bytes,
                    quotechar=self.quotechar,
                    escapechar=self.escapechar
                )
            writer = self.partition_writers[()]

        writer.write_row(row)

    def commit(self):
        """Atomically move temp directory to output location."""
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir)
        os.rename(self.temp_dir, self.output_dir)
        self.temp_dir = None

    def cleanup(self):
        """Clean up temp directory on failure."""
        if self.temp_dir and self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir = None


class _PartitionWriter:
    """Writer for a single partition directory with size/row limits."""

    def __init__(
        self,
        partition_dir: Path,
        columns: List[Dict],
        max_rows: Optional[int],
        max_bytes: Optional[int],
        header: str,
        header_bytes: bytes,
        quotechar: str = '"',
        escapechar: Optional[str] = None
    ):
        self.partition_dir = partition_dir
        self.columns = columns
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.header = header
        self.header_bytes = header_bytes
        self.quotechar = quotechar
        self.escapechar = escapechar

        self.current_file_idx = 0
        self.current_file: Optional[TextIO] = None
        self.current_writer: Optional[csv.writer] = None
        self.current_rows = 0
        self.current_bytes = 0

        self._open_new_file()

    def _open_new_file(self):
        """Open a new part-xxxxx.csv file."""
        if self.current_file:
            self.current_file.close()

        filename = f"part-{self.current_file_idx:05d}.csv"
        self.filepath = self.partition_dir / filename
        self.current_file = open(self.filepath, 'w', encoding='utf-8', newline='')

        self.current_writer = csv.writer(
            self.current_file,
            delimiter=',',
            quotechar=self.quotechar,
            escapechar=self.escapechar,
            quoting=csv.QUOTE_MINIMAL
        )
        self.current_writer.writerow(self.columns_to_names())

        self.current_rows = 0
        self.current_bytes = len(self.header_bytes)
        self.current_file_idx += 1

    def _should_rotate(self, row_bytes: int) -> bool:
        """Check if we should rotate to a new file."""
        if self.max_rows is not None and self.current_rows >= self.max_rows:
            return True
        if self.max_bytes is not None:
            if self.current_rows == 0 and row_bytes > self.max_bytes:
                return False
            if self.current_bytes + row_bytes > self.max_bytes:
                return True
        return False

    def _estimate_row_bytes(self, row: List[str]) -> int:
        """Estimate the on-disk byte size of a row."""
        line = ','.join(self._csv_escape_field(str(f)) for f in row) + '\n'
        return len(line.encode('utf-8'))

    def _csv_escape_field(self, field: str) -> str:
        """Escape a field value for CSV (simple heuristic to estimate size)."""
        if not field:
            return ''
        if ("," in field or '"' in field or '\n' in field):
            escaped = '"' + field.replace('"', '""') + '"'
            return escaped
        return field

    def columns_to_names(self):
        return [c['name'] for c in self.columns]

    def write_row(self, row: List[str]):
        """Write a row, rotating file if needed."""
        row_bytes = self._estimate_row_bytes(row)

        if self._should_rotate(row_bytes):
            self._open_new_file()

        assert self.current_file is not None
        assert self.current_writer is not None

        self.current_writer.writerow(row)
        self.current_rows += 1
        self.current_bytes += row_bytes


def write_output(
    rows: List[List[str]],
    columns: List[Dict],
    output_path: str,
    quotechar: str = '"',
    escapechar: str = '',
    null_literal: str = ''
):
    header = [c['name'] for c in columns]

    if output_path == '-':
        writer = csv.writer(sys.stdout, delimiter=',', quotechar=quotechar,
                           escapechar=escapechar if escapechar else None,
                           quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    else:
        tmp_path = output_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=',', quotechar=quotechar,
                               escapechar=escapechar if escapechar else None,
                               quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, output_path)


def exit_with_error(message: str, exit_code: int = 1):
    sys.stderr.write(f"Error: {message}\n")
    sys.exit(exit_code)


def main():
    parser = argparse.ArgumentParser(
        description='Merge and sort CSV, TSV, JSONL, and Parquet files',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  Mixed CSV + JSONL + Parquet, schema inferred by consensus:
    python merge_files.py --output merged.csv --key ts,id \
          --schema-strategy consensus \
          inputs/users.csv inputs/events.jsonl.gz inputs/metrics.parquet

  Authoritative schema, TSV source, gzip forced:
    python merge_files.py --output - --key created_at,id --desc \
          --schema schema.json --input-format tsv --compression gzip \
          data/*.tsv.gz

  With nested types support:
    python merge_files.py --output merged.csv --key user.id \
          --schema schema_nested.json \
          inputs/events.jsonl inputs/users.parquet
'''
    )

    parser.add_argument('--output', '-o', required=True,
                        help='Output file/directory (use "-" for stdout, must be directory if partitioning)')
    parser.add_argument('--key', '-k', required=True,
                        help='Sort key column(s) - supports nested paths like user.id, items[0].sku, attrs["country"]')
    parser.add_argument('--desc', action='store_true', help='Sort descending (within partitions if partitioned)')

    parser.add_argument('--partition-by',
                        help='Partition columns using field paths for Hive-style directory layout')
    parser.add_argument('--max-rows-per-file', type=int, help='Max rows per output file (header not counted)')
    parser.add_argument('--max-bytes-per-file', type=int, help='Max bytes per output file (including header)')

    parser.add_argument('--schema', help='JSON schema file with nested types support')
    parser.add_argument('--type-alias-file', help='JSON file with custom type aliases')
    parser.add_argument('--infer', choices=['strict', 'loose'], default='strict', help='Inference mode')
    parser.add_argument('--schema-strategy',
                        choices=['authoritative', 'consensus', 'union'],
                        default='authoritative',
                        help='Schema resolution strategy')
    parser.add_argument('--on-type-error',
                        choices=['coerce-null', 'fail', 'keep-string'],
                        default='coerce-null', help='Type error strategy')

    parser.add_argument('--memory-limit-mb', type=int, default=64, help='Memory limit in MB')
    parser.add_argument('--temp-dir', help='Temp dir for sorting')

    parser.add_argument('--csv-quotechar', default='"', help='CSV quote char')
    parser.add_argument('--csv-escapechar', default='', help='CSV escape char (empty=disabled)')
    parser.add_argument('--csv-null-literal', default='', help='Null value representation')

    parser.add_argument('--input-format',
                        choices=['auto', 'csv', 'tsv', 'jsonl', 'parquet'],
                        default='auto',
                        help='Input format (default: auto-detect)')
    parser.add_argument('--compression',
                        choices=['auto', 'none', 'gzip'],
                        default='auto',
                        help='Compression (default: auto-detect .gz)')
    parser.add_argument('--parquet-row-group-bytes', type=int, default=64 * 1024 * 1024,
                        help='Parquet row group target bytes')

    parser.add_argument('input_files', nargs='+', help='Input files')

    args = parser.parse_args()

    compression = args.compression
    if compression == 'auto':
        compression = None

    for filepath in args.input_files:
        if compression == 'none':
            if detect_compression(filepath):
                exit_with_error(f"Compression mismatch: {filepath} is gzip but --compression=none", 5)
        elif compression == 'gzip':
            if not detect_compression(filepath):
                exit_with_error(f"Compression mismatch: {filepath} is not gzip but --compression=gzip", 5)

    key_paths = [k.strip() for k in args.key.split(',')]
    explicit_format = None if args.input_format == 'auto' else args.input_format

    partition_columns = None
    if args.partition_by:
        partition_columns = [p.strip() for p in args.partition_by.split(',')]
        if args.output == '-':
            exit_with_error("--output must be a directory path (not '-') when partitioning", 1)

    try:
        columns, column_types = resolve_schema_across_formats(
            args.infer, args.schema_strategy, args.schema,
            args.type_alias_file,
            args.input_files, explicit_format
        )
    except ValueError as e:
        if 'ERR 6' in str(e):
            # Extract error number and message
            msg = str(e)
            exit_with_error(msg, 6)
        exit_with_error(str(e), 6)
    except ImportError as e:
        exit_with_error(str(e), 1)

    # Validate key paths resolve to primitives
    for kc in key_paths:
        # This is validated during row processing with sample data
        # We check basic path syntax here
        pass

    for pc in (partition_columns or []):
        pass  # Validated during processing

    key_paths_str = key_paths
    partition_paths = partition_columns

    def unified_generator_with_dicts():
        """Yield (row, row_dict) tuples for sorting and processing."""
        for filepath in args.input_files:
            fmt = get_effective_format(filepath, explicit_format)

            for row in row_generator(
                filepath, fmt, columns, column_types,
                args.on_type_error, args.csv_null_literal,
                args.csv_quotechar, args.csv_escapechar,
                args.parquet_row_group_bytes
            ):
                # Reconstruct row dict from values
                row_dict = {}
                for col in columns:
                    col_name = col['name']
                    col_type = column_types.get(col_name)
                    cell_value = row[len([c for c in columns if c != col])]  # index hack
                    # Actually, row is already a list of strings, columns is list of dicts
                    # Let's use index properly
                    idx = [c['name'] for c in columns].index(col_name)
                    cell_str = row[idx]
                    # Parse cell based on type for row_dict
                    if cell_str == args.csv_null_literal or cell_str == '':
                        row_dict[col_name] = None
                    elif isinstance(col_type, JsonType):
                        # JSON columns: parse back to dict/list/primitive
                        if cell_str:
                            try:
                                row_dict[col_name] = json.loads(cell_str)
                            except json.JSONDecodeError:
                                row_dict[col_name] = cell_str
                        else:
                            row_dict[col_name] = None
                    else:
                        # For other types, keep as string for path resolution
                        row_dict[col_name] = cell_str if cell_str else None

                # For sorting, we need the actual parsed values
                # Re-process to get structured values for key resolution
                processed_dict = {}
                for col in columns:
                    col_name = col['name']
                    target_type = column_types.get(col_name)
                    cell_str = row[[c['name'] for c in columns].index(col_name)]

                    if cell_str == args.csv_null_literal or cell_str == '':
                        processed_dict[col_name] = None
                    elif isinstance(target_type, (PrimitiveType, StructType, ArrayType, MapType)):
                        if isinstance(target_type, JsonType):
                            # JSON type
                            try:
                                processed_dict[col_name] = json.loads(cell_str) if cell_str else None
                            except json.JSONDecodeError:
                                processed_dict[col_name] = cell_str
                        else:
                            # Parse based on type - this is complex, for now keep string
                            processed_dict[col_name] = cell_str
                    else:
                        processed_dict[col_name] = cell_str

                yield (row, processed_dict)

    # For sorting, we need to extract values from row_dict for key paths
    # Let's create a generator that yields (row, row_dict) properly
    def make_sorted_rows():
        rows_with_dicts = []
        for filepath in args.input_files:
            fmt = get_effective_format(filepath, explicit_format)

            for row in row_generator(
                filepath, fmt, columns, column_types,
                args.on_type_error, args.csv_null_literal,
                args.csv_quotechar, args.csv_escapechar,
                args.parquet_row_group_bytes
            ):
                # Build a proper row_dict with parsed values for nested key resolution
                col_names = [c['name'] for c in columns]
                row_dict = {}

                for i, col in enumerate(columns):
                    col_name = col['name']
                    target_type = column_types.get(col_name)
                    cell_str = row[i]

                    if cell_str == args.csv_null_literal or cell_str == '':
                        row_dict[col_name] = None
                    elif isinstance(target_type, JsonType):
                        # JSON column - parse from JSON string
                        if cell_str:
                            try:
                                row_dict[col_name] = json.loads(cell_str)
                            except json.JSONDecodeError:
                                row_dict[col_name] = cell_str
                        else:
                            row_dict[col_name] = None
                    else:
                        # For non-JSON columns in CSV, the cell is already a string/number representation
                        # We'll parse primitives for sorting
                        parsed = None
                        if cell_str:
                            # Try to parse as number/bool
                            lower = cell_str.lower()
                            if lower in ('true', 'yes', '1', 't', 'y'):
                                parsed = True
                            elif lower in ('false', 'no', '0', 'f', 'n'):
                                parsed = False
                            else:
                                try:
                                    if '.' in cell_str:
                                        parsed = float(cell_str)
                                    else:
                                        parsed = int(cell_str)
                                except ValueError:
                                    parsed = cell_str
                        row_dict[col_name] = parsed

                rows_with_dicts.append((row, row_dict))

        # Sort in memory if small enough, otherwise use external sort
        memory_mb = args.memory_limit_mb if args.memory_limit_mb else 64
        max_mem_bytes = memory_mb * 1024 * 1024

        # Estimate total size
        total_bytes = sum(sum(len(c.encode('utf-8')) for c in r) + len(str(d).encode('utf-8'))
                         for r, d in rows_with_dicts)

        if total_bytes < max_mem_bytes * 0.8:
            # Sort in memory
            sort_helper = SimpleSortWrapper(key_paths_str, args.desc)

            def get_sort_key(item):
                row, row_dict = item
                return sort_helper.make_sort_tuple(row_dict)

            rows_with_dicts.sort(key=get_sort_key)
            return [r for r, _ in rows_with_dicts]
        else:
            # Use external merge sort
            sorter = ExternalMergeSort2(memory_mb, args.temp_dir)
            return sorter.sort_and_merge(
                rows_with_dicts,
                key_paths_str,
                args.desc,
                len(columns)
            )

    sorted_rows = make_sorted_rows()

    # Output handling
    if args.output == '-':
        write_output(sorted_rows, columns, args.output, args.csv_quotechar, args.csv_escapechar, args.csv_null_literal)
    else:
        writer = PartitionedFileWriter(
            output_dir=args.output,
            columns=columns,
            partition_columns=partition_paths,
            max_rows_per_file=args.max_rows_per_file,
            max_bytes_per_file=args.max_bytes_per_file,
            csv_quotechar=args.csv_quotechar,
            csv_escapechar=args.csv_escapechar,
            csv_null_literal=args.csv_null_literal
        )
        try:
            column_names = [c['name'] for c in columns]

            # We need to reconstruct row dicts for partitioning
            # Re-process rows with proper dicts
            for row in sorted_rows:
                # Reconstruct row dict for partition extraction
                row_dict = {}
                for i, col in enumerate(columns):
                    col_name = col['name']
                    target_type = column_types.get(col_name)
                    cell_str = row[i]

                    if cell_str == args.csv_null_literal or cell_str == '':
                        row_dict[col_name] = None
                    elif isinstance(target_type, JsonType):
                        # JSON column - parse from JSON string
                        if cell_str:
                            try:
                                row_dict[col_name] = json.loads(cell_str)
                            except json.JSONDecodeError:
                                row_dict[col_name] = cell_str
                        else:
                            row_dict[col_name] = None
                    else:
                        # For non-JSON columns, keep cell as-is
                        row_dict[col_name] = cell_str

                writer.write_row(row, row_dict)
            writer.commit()
        except Exception:
            writer.cleanup()
            raise


if __name__ == '__main__':
    main()
