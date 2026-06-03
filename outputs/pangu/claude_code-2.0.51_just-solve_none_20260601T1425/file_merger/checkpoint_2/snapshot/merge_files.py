#!/usr/bin/env python3
"""
Multi-Format CSV Merger

A command-line tool that ingests multiple CSV, TSV, JSONL, and Parquet files,
aligns their schemas, and produces one sorted CSV output.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import heapq
import gzip
import struct
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Iterator, Set, Union
from collections import defaultdict
from enum import Enum

# Parquet support (optional, gracefully degrade)
try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    HAS_PYARROW = True
except ImportError:
    HAS_PYARROW = False


# =============================================================================
# Constants
# =============================================================================

TYPE_PRIORITY = {
    'timestamp': 60,
    'date': 50,
    'bool': 40,
    'int': 30,
    'float': 20,
    'string': 10,
}

TYPE_HIERARCHY = ['string', 'bool', 'int', 'float', 'date', 'timestamp']

TRUE_VALUES = {'true', 'yes', '1', 't', 'y'}
FALSE_VALUES = {'false', 'no', '0', 'f', 'n'}

PARQUET_MAGIC = b'PAR1'


# =============================================================================
# Enums
# =============================================================================

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


# =============================================================================
# Type parsing
# =============================================================================

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
        return None  # bool is separate
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


TYPE_PARSERS = {
    'string': lambda x: str(x).strip() if x is not None else None,
    'int': parse_int,
    'float': parse_float,
    'bool': parse_bool,
    'date': parse_date,
    'timestamp': parse_timestamp,
}


def infer_type_from_value(value: Any, mode: str) -> Optional[str]:
    if value is None:
        return None

    if mode == 'loose':
        order = ['timestamp', 'date', 'bool', 'int', 'float']
    else:
        order = ['bool', 'int', 'float']  # strict prefers more specific

    for t in order:
        if TYPE_PARSERS[t](value) is not None:
            return t
    return None


# =============================================================================
# Input Format Detection
# =============================================================================

def detect_format_by_magic(filepath: str) -> Optional[InputFormat]:
    """Detect format using magic bytes."""
    try:
        with open(filepath, 'rb') as f:
            magic = f.read(8)
            if magic.startswith(PARQUET_MAGIC):
                return InputFormat.PARQUET
    except (IOError, OSError):
        pass
    return None


def detect_format_by_extension(filepath: str) -> Optional[InputFormat]:
    """Detect format by file extension."""
    path = Path(filepath)
    name = path.name.lower()

    # Handle .gz compression first
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
    """Check if file is gzip compressed."""
    try:
        with open(filepath, 'rb') as f:
            return f.read(2) == b'\x1f\x8b'
    except (IOError, OSError):
        return False


def get_effective_format(filepath: str, explicit_format: Optional[str]) -> InputFormat:
    """Determine effective input format for a file."""
    if explicit_format:
        return InputFormat(explicit_format)

    # Try extension first
    ext_format = detect_format_by_extension(filepath)
    if ext_format:
        return ext_format

    # Try magic bytes
    magic_format = detect_format_by_magic(filepath)
    if magic_format:
        return magic_format

    raise ValueError(f"Cannot determine format for {filepath}: no extension or magic bytes")


# =============================================================================
# Schema Resolution
# =============================================================================

def load_schema(schema_path: str) -> List[Dict]:
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = json.load(f)
    return schema.get('columns', [])


def find_common_type(types: Set[str]) -> str:
    """Find the simplest common type that can hold all observed types."""
    if 'string' in types:
        return 'string'
    if 'timestamp' in types and len(types) > 1:
        # timestamp can hold everything
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
    input_files: List[str],
    explicit_format: Optional[str]
) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Resolve schema across heterogeneous inputs.

    Returns (columns, column_types) where columns is ordered list of {name, type}
    and column_types is name->type mapping.
    """
    if schema_path:
        columns = load_schema(schema_path)
        column_types = {c['name']: c['type'] for c in columns}
        return columns, column_types

    # Collect field information from all inputs
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
                        # Sample values for type inference
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

                            inferred = infer_type_from_value(val, infer_mode)
                            if inferred:
                                field_types.setdefault(key, set()).add(inferred)
                            else:
                                field_types.setdefault(key, set()).add('string')

                        # Only process first few objects for inference
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

                # Validate flat schema
                for field in schema:
                    if pa.types.is_nested(field.type):
                        raise ValueError(f"Nested types not supported: {field.name} in {filepath}")

                for field in schema:
                    col_name = field.name
                    if col_name not in all_fields:
                        all_fields[col_name] = {'files': [], 'types': set()}
                    all_fields[col_name]['files'].append(filepath)

                    # Infer type from Parquet physical type
                    parquet_type = field.type
                    mapped_type = map_parquet_type(parquet_type)
                    field_types.setdefault(col_name, set()).add(mapped_type)

                # Read sample values for type inference
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

    # Apply schema strategy
    columns = []
    column_types = {}

    # Collect all unique column names
    column_names = sorted(all_fields.keys())

    for col_name in column_names:
        # Collect all types from all files
        all_types: Set[str] = set()
        file_type_sets: List[Set[str]] = []

        for fft in file_field_types:
            if col_name in fft:
                all_types.update(fft[col_name])
                file_type_sets.append(fft[col_name])

        if not all_types:
            # No data, default to string
            col_type = 'string'
        elif len(all_types) == 1:
            col_type = all_types.pop()
        else:
            # Multiple types - apply strategy
            if schema_strategy == 'authoritative':
                # Use highest priority type
                max_prio = -1
                col_type = 'string'
                for t in all_types:
                    if TYPE_PRIORITY.get(t, 0) > max_prio:
                        max_prio = TYPE_PRIORITY[t]
                        col_type = t
            elif schema_strategy == 'consensus':
                # Find type majority supports
                type_counts = defaultdict(int)
                for types_set in file_type_sets:
                    if types_set:
                        # Count only single types for consensus
                        if len(types_set) == 1:
                            t = types_set.pop()
                            type_counts[t] += 1
                if type_counts:
                    # Find type with most votes
                    col_type = max(type_counts.keys(), key=lambda t: type_counts[t])
                else:
                    # No clear consensus, use union
                    col_type = find_common_type(all_types)
            else:  # union
                col_type = find_common_type(all_types)

        columns.append({'name': col_name, 'type': col_type})
        column_types[col_name] = col_type

    return columns, column_types


def map_parquet_type(pa_type) -> str:
    """Map Parquet type to our type system."""
    if pa.types.is_boolean(pa_type):
        return 'bool'
    elif pa.types.is_int8(pa_type) or pa.types.is_int16(pa_type) or \
         pa.types.is_int32(pa_type) or pa.types.is_int64(pa_type) or \
         pa.types.is_uint8(pa_type) or pa.types.is_uint16(pa_type) or \
         pa.types.is_uint32(pa_type) or pa.types.is_uint64(pa_type):
        return 'int'
    elif pa.types.is_float32(pa_type) or pa.types.is_float64(pa_type):
        return 'float'
    elif pa.types.is_date(pa_type):
        return 'date'
    elif pa.types.is_timestamp(pa_type):
        return 'timestamp'
    elif pa.types.is_string(pa_type) or pa.types.is_large_string(pa_type) or \
         pa.types.is_utf8(pa_type):
        return 'string'
    else:
        return 'string'


# =============================================================================
# Casting
# =============================================================================

def cast_value(value: Any, target_type: str, error_strategy: str) -> Any:
    """Cast a value to target type. Returns typed value."""
    parser = TYPE_PARSERS.get(target_type)
    if not parser:
        return None

    try:
        result = parser(value)
        if result is not None:
            return result
    except Exception:
        pass

    # Parsing failed
    if error_strategy == 'coerce-null':
        return None
    elif error_strategy == 'keep-string':
        return str(value).strip() if value is not None else None
    else:
        raise ValueError(f"Cannot cast '{value}' to {target_type}")


def format_value(value: Any, target_type: str, null_literal: str = '') -> str:
    if value is None:
        return null_literal
    if target_type in ('timestamp', 'date'):
        return str(value)
    elif target_type in ('int', 'float'):
        return str(value)
    elif target_type == 'bool':
        return '1' if value else '0'
    else:
        return str(value)


# =============================================================================
# Row Processing for Different Formats
# =============================================================================

def process_row(
    row: Dict[str, Any],
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str
) -> List[str]:
    """Process row into ordered string list per schema."""
    result = []
    for col_def in columns:
        col_name = col_def['name']
        col_type = col_def['type']
        value = row.get(col_name)

        # Check for null/empty
        if value is None or value == '':
            result.append(null_literal)
        else:
            try:
                typed = cast_value(value, col_type, error_strategy)
                result.append(format_value(typed, col_type, null_literal))
            except ValueError:
                if error_strategy == 'fail':
                    raise
                result.append(null_literal)

    return result


# =============================================================================
# File Readers
# =============================================================================

def open_compressed(filepath: str, mode='rt', encoding='utf-8', newline=None):
    """Open file with automatic gzip detection."""
    if detect_compression(filepath):
        return gzip.open(filepath, mode, encoding=encoding)
    return open(filepath, mode, encoding=encoding, newline=newline)


def read_csv_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str,
    quotechar: str = '"',
    escapechar: str = ''
) -> Iterator[List[str]]:
    """Read CSV file row by row."""
    try:
        with open_compressed(filepath, newline='') as f:
            reader = csv.DictReader(f, delimiter=',', quotechar=quotechar,
                                   escapechar=escapechar if escapechar else None)
            for row in reader:
                yield process_row(row, columns, column_types,
                                error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_tsv_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str
) -> Iterator[List[str]]:
    """Read TSV file row by row."""
    try:
        with open_compressed(filepath, newline='') as f:
            reader = csv.DictReader(f, delimiter='\t')
            for row in reader:
                # Check for literal tabs in fields (error 5)
                for v in row.values():
                    if '\t' in str(v):
                        raise ValueError("Literal tab character in TSV field")
                yield process_row(row, columns, column_types,
                                error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_jsonl_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str
) -> Iterator[List[str]]:
    """Read JSONL file row by row."""
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

                # Validate flat schema (no nested objects/arrays)
                for key, val in obj.items():
                    if val is not None and isinstance(val, (dict, list)):
                        if error_strategy == 'fail':
                            raise ValueError(f"Nested types not allowed: {key} at {filepath}:{line_num}")
                        sys.stderr.write(f"Warning: Nested types not allowed: {key} at {filepath}:{line_num}\n")
                        # Coerce to None
                        obj[key] = None

                yield process_row(obj, columns, column_types,
                                error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


def read_parquet_rows(
    filepath: str,
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str,
    row_group_bytes: int = 64 * 1024 * 1024
) -> Iterator[List[str]]:
    """Read Parquet file row-group by row."""
    if not HAS_PYARROW:
        raise ImportError("pyarrow required for Parquet support")

    try:
        parquet_file = pq.ParquetFile(filepath)
        schema = parquet_file.schema

        # Validate flat schema
        for field in schema:
            if pa.types.is_nested(field.type):
                raise ValueError(f"Nested types not supported: {field.name} in {filepath}")

        # Stream through row groups
        for rg_idx in range(parquet_file.num_row_groups):
            row_group = parquet_file.read_row_group(rg_idx)
            table = row_group.to_pandas()

            for _, row in table.iterrows():
                row_dict = row.to_dict()
                yield process_row(row_dict, columns, column_types,
                                error_strategy, null_literal)
    except Exception as e:
        if error_strategy == 'fail':
            raise
        sys.stderr.write(f"Warning: Skipping {filepath}: {e}\n")


# =============================================================================
# Unified Row Generator
# =============================================================================

def row_generator(
    filepath: str,
    fmt: InputFormat,
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str,
    quotechar: str = '"',
    escapechar: str = '',
    parquet_row_group_bytes: int = 64 * 1024 * 1024
) -> Iterator[List[str]]:
    """Generate rows from any input format."""
    if fmt == InputFormat.CSV:
        yield from read_csv_rows(filepath, columns, column_types,
                                  error_strategy, null_literal, quotechar, escapechar)
    elif fmt == InputFormat.TSV:
        yield from read_tsv_rows(filepath, columns, column_types,
                                  error_strategy, null_literal)
    elif fmt == InputFormat.JSONL:
        yield from read_jsonl_rows(filepath, columns, column_types,
                                    error_strategy, null_literal)
    elif fmt == InputFormat.PARQUET:
        yield from read_parquet_rows(filepath, columns, column_types,
                                      error_strategy, null_literal, parquet_row_group_bytes)


# =============================================================================
# External Merge Sort
# =============================================================================

class ExternalMergeSort:
    """
    External merge sort for data exceeding memory.
    """

    def __init__(self, memory_limit_mb: int, temp_dir: Optional[str]):
        self.memory_limit_bytes = memory_limit_mb * 1024 * 1024
        self.temp_dir = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.run_files: List[str] = []

    def _make_sort_key(self, row: List[str], key_indices: List[int], desc: bool) -> Tuple:
        """Build sort key tuple from row."""
        vals = [row[i] if i < len(row) else '' for i in key_indices]

        if desc:
            # Descending: non-nulls first, then nulls
            # Use inverted string for correct ordering
            inverted = []
            for v in vals:
                if v == '':
                    inverted.append((0, ''))
                else:
                    inverted.append((1, ''.join(chr(255 - ord(c)) for c in v)))
            return tuple(inverted)
        else:
            # Ascending: nulls first, then non-nulls
            inverted = []
            for v in vals:
                is_null = 1 if v == '' else 0
                inverted.append((is_null, v))
            return tuple(inverted)

    def sort_and_merge(self,
        rows_generator,
        key_indices: List[int],
        desc: bool,
        num_cols: int
    ) -> List[List[str]]:
        """
        Sort data in runs and merge.
        Returns sorted rows (may be split into runs then merged).
        """
        run_mem_limit = self.memory_limit_bytes * 0.8

        # Build sorted runs
        current_run: List[Tuple] = []
        current_mem = 0

        for row in rows_generator:
            # Estimate memory
            mem_est = sum(len(c.encode('utf-8')) for c in row)

            if current_run and current_mem + mem_est > run_mem_limit:
                # Write current run
                self._write_run(current_run, key_indices, desc)
                current_run = []
                current_mem = 0

            current_run.append(tuple(row))
            current_mem += mem_est

        # Last run
        if current_run:
            self._write_run(current_run, key_indices, desc)

        # Merge runs
        return self._merge_runs(key_indices, desc)

    def _write_run(self, rows: List[Tuple], key_indices: List[int], desc: bool):
        """Write a sorted run to temp file."""
        # Sort in-memory
        def sort_key(row_tuple):
            row = list(row_tuple)
            return self._make_sort_key(row, key_indices, desc)

        sorted_rows = sorted(rows, key=sort_key)

        run_file = tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', newline='',
            suffix='.csv', dir=self.temp_dir, delete=False
        )

        writer = csv.writer(run_file, delimiter=',', quotechar='"',
                            quoting=csv.QUOTE_MINIMAL)
        for row in sorted_rows:
            writer.writerow(row)

        run_file.close()
        self.run_files.append(run_file.name)

    def _merge_runs(self, key_indices: List[int], desc: bool) -> List[List[str]]:
        """Merge sorted runs using heap."""
        if not self.run_files:
            return []

        # Open files and read headers
        files = []
        readers = []
        for fpath in self.run_files:
            f = open(fpath, 'r', encoding='utf-8', newline='')
            reader = csv.reader(f, delimiter=',', quotechar='"')
            files.append(f)
            readers.append(reader)

        # Build heap entries
        def make_key(row):
            return self._make_sort_key(row, key_indices, desc)

        heap = []
        for i, reader in enumerate(readers):
            try:
                row = next(reader)
                heap.append((make_key(row), i, row))
            except StopIteration:
                pass

        heapq.heapify(heap)
        result = []

        while heap:
            _, file_idx, row = heapq.heappop(heap)
            result.append(row)

            reader = readers[file_idx]
            try:
                next_row = next(reader)
                heapq.heappush(heap, (make_key(next_row), file_idx, next_row))
            except StopIteration:
                pass

        # Close files
        for f in files:
            f.close()

        self._cleanup()
        return result

    def _cleanup(self):
        """Remove temp files."""
        for f in self.run_files:
            try:
                os.unlink(f)
            except OSError:
                pass
        self.run_files = []


# =============================================================================
# Output
# =============================================================================

def write_output(
    rows: List[List[str]],
    columns: List[Dict],
    output_path: str,
    quotechar: str = '"',
    escapechar: str = '',
    null_literal: str = ''
):
    """Write output CSV."""
    header = [c['name'] for c in columns]

    if output_path == '-':
        writer = csv.writer(sys.stdout, delimiter=',', quotechar=quotechar,
                           escapechar=escapechar if escapechar else None,
                           quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    else:
        # Write atomically to temp then rename
        tmp_path = output_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=',', quotechar=quotechar,
                               escapechar=escapechar if escapechar else None,
                               quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, output_path)


# =============================================================================
# Main
# =============================================================================

def exit_with_error(message: str, exit_code: int = 1):
    """Write error to stderr and exit."""
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
'''
    )

    # Standard args
    parser.add_argument('--output', '-o', required=True,
                        help='Output file (use "-" for stdout)')
    parser.add_argument('--key', '-k', required=True,
                        help='Sort key column(s), comma-separated')
    parser.add_argument('--desc', action='store_true',
                        help='Sort descending')

    # Schema args
    parser.add_argument('--schema', help='JSON schema file')
    parser.add_argument('--infer', choices=['strict', 'loose'],
                        default='strict', help='Inference mode')
    parser.add_argument('--schema-strategy',
                        choices=['authoritative', 'consensus', 'union'],
                        default='authoritative',
                        help='Schema resolution strategy')
    parser.add_argument('--on-type-error',
                        choices=['coerce-null', 'fail', 'keep-string'],
                        default='coerce-null', help='Type error strategy')

    # Memory args
    parser.add_argument('--memory-limit-mb', type=int, default=64,
                        help='Memory limit in MB')
    parser.add_argument('--temp-dir', help='Temp dir for sorting')

    # CSV dialect args
    parser.add_argument('--csv-quotechar', default='"',
                        help='CSV quote char')
    parser.add_argument('--csv-escapechar', default='',
                        help='CSV escape char (empty=disabled)')
    parser.add_argument('--csv-null-literal', default='',
                        help='Null value representation')

    # Input format args
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

    parser.add_argument('input_files', nargs='+',
                        help='Input files')

    args = parser.parse_args()

    # Validate compression specification
    compression = args.compression
    if compression == 'auto':
        compression = None  # Will detect per file

    # Check for compression mismatch
    for filepath in args.input_files:
        if compression == 'none':
            if detect_compression(filepath):
                exit_with_error(f"Compression mismatch: {filepath} is gzip but --compression=none", 5)
        elif compression == 'gzip':
            if not detect_compression(filepath):
                exit_with_error(f"Compression mismatch: {filepath} is not gzip but --compression=gzip", 5)

    # Parse key columns
    key_columns = [k.strip() for k in args.key.split(',')]

    # Validate format compatibility
    explicit_format = None if args.input_format == 'auto' else args.input_format

    # Resolve schema
    try:
        columns, column_types = resolve_schema_across_formats(
            args.infer,
            args.schema_strategy,
            args.schema,
            args.input_files,
            explicit_format
        )
    except ValueError as e:
        exit_with_error(str(e), 6)
    except ImportError as e:
        exit_with_error(str(e), 1)

    # Validate key columns exist
    for kc in key_columns:
        if kc not in column_types:
            exit_with_error(f"Key column '{kc}' not in output schema", 3)

    key_indices = [i for i, c in enumerate(columns) if c['name'] in key_columns]

    # Build unified row generator
    def unified_generator():
        for filepath in args.input_files:
            fmt = get_effective_format(filepath, explicit_format)
            yield from row_generator(
                filepath, fmt, columns, column_types,
                args.on_type_error, args.csv_null_literal,
                args.csv_quotechar, args.csv_escapechar,
                args.parquet_row_group_bytes
            )

    # Pipeline
    sorter = ExternalMergeSort(args.memory_limit_mb, args.temp_dir)

    sorted_rows = sorter.sort_and_merge(
        unified_generator(),
        key_indices,
        args.desc,
        len(columns)
    )

    write_output(
        sorted_rows, columns, args.output,
        args.csv_quotechar, args.csv_escapechar, args.csv_null_literal
    )


if __name__ == '__main__':
    main()
