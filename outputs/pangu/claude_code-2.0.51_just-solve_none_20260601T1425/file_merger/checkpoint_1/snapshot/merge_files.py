#!/usr/bin/env python3
"""
CSV Merger and Sorter

A command-line tool that ingests multiple CSVs, aligns their schemas,
and produces one sorted CSV output.
"""

import argparse
import csv
import json
import os
import sys
import tempfile
import heapq
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple


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

TRUE_VALUES = {'true', 'yes', '1', 't', 'y'}
FALSE_VALUES = {'false', 'no', '0', 'f', 'n'}


# =============================================================================
# Type parsing
# =============================================================================

def parse_bool(value: str) -> Optional[bool]:
    """Parse boolean value. None for invalid."""
    if not value:
        return None
    lower = value.lower().strip()
    if lower in TRUE_VALUES:
        return True
    if lower in FALSE_VALUES:
        return False
    return None


def parse_int(value: str) -> Optional[int]:
    """Parse integer value. None for invalid."""
    if not value:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_float(value: str) -> Optional[float]:
    """Parse float value. None for invalid."""
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_date(value: str) -> Optional[str]:
    """Parse date value (YYYY-MM-DD). None for invalid."""
    if not value:
        return None
    try:
        parsed = datetime.strptime(value.strip(), '%Y-%m-%d').date()
        return parsed.isoformat()
    except (ValueError, AttributeError):
        return None


def parse_timestamp(value: str) -> Optional[str]:
    """
    Parse timestamp to UTC with Z suffix.
    None for invalid.
    """
    if not value:
        return None

    value = value.strip()
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
            dt = datetime.strptime(value, fmt)
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            continue
    return None


TYPE_PARSERS = {
    'string': lambda x: x.strip() if x else None,
    'int': parse_int,
    'float': parse_float,
    'bool': parse_bool,
    'date': parse_date,
    'timestamp': parse_timestamp,
}


def cast_value(value: str, target_type: str, error_strategy: str) -> Any:
    """
    Cast a string to target type.
    Returns the value in appropriate type (not string representation).
    """
    parser = TYPE_PARSERS.get(target_type)
    if not parser:
        return None

    result = parser(value)
    if result is not None:
        return result

    # Parsing failed
    if error_strategy == 'coerce-null':
        return None
    elif error_strategy == 'keep-string':
        return value.strip() if value else None
    else:  # fail
        raise ValueError(f"Cannot cast '{value}' to {target_type}")


def format_value(value: Any, target_type: str) -> str:
    """Format a typed value back to string representation."""
    if value is None:
        return ''
    if target_type == 'timestamp' or target_type == 'date':
        return str(value)  # Already formatted
    elif target_type in ('int', 'float'):
        return str(value)
    elif target_type == 'bool':
        return '1' if value else '0'
    else:  # string
        return str(value)


# =============================================================================
# Schema Resolution
# =============================================================================

def load_schema(schema_path: str) -> List[Dict]:
    """Load schema from JSON file."""
    with open(schema_path, 'r', encoding='utf-8') as f:
        schema = json.load(f)
    return schema.get('columns', [])


def infer_type_from_value(value: str, mode: str) -> Optional[str]:
    """Infer type from a single value based on mode."""
    if not value:
        return None

    if mode == 'loose':
        # Prefer temporal/numeric types
        for t in ['timestamp', 'date', 'bool', 'int', 'float']:
            if TYPE_PARSERS[t](value) is not None:
                return t
    elif mode == 'strict':
        # Only accept if value cleanly parses
        for t in ['timestamp', 'date', 'bool', 'int', 'float']:
            if TYPE_PARSERS[t](value) is not None:
                return t

    return None


def resolve_schema(
    infer_mode: str,
    schema_path: Optional[str],
    input_files: List[str]
) -> Tuple[List[Dict], Dict[str, str]]:
    """
    Resolve output schema.
    Returns (columns, column_types) where:
      - columns: list of {'name': str, 'type': str}
      - column_types: name -> type dict
    """
    if schema_path:
        # Explicit schema
        columns = load_schema(schema_path)
        column_types = {c['name']: c['type'] for c in columns}
        return columns, column_types

    # Infer from union of headers
    all_columns = {}
    column_type_sets = {}  # name -> set of inferred types

    for filepath in input_files:
        try:
            with open(filepath, 'r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f, delimiter=',')
                for col_name in reader.fieldnames or []:
                    if col_name not in all_columns:
                        all_columns[col_name] = None  # placeholder
                        column_type_sets[col_name] = set()

                    # Sample rows for type inference
                    f.seek(0)
                    next(reader)  # skip header
                    for i, row in enumerate(reader):
                        if i >= 20:  # sample limit
                            break
                        val = row.get(col_name, '')
                        inferred = infer_type_from_value(val, infer_mode)
                        if inferred:
                            column_type_sets[col_name].add(inferred)
                        elif val:  # non-empty but can't parse
                            column_type_sets[col_name].add('string')
        except Exception:
            pass  # Missing or bad file

    # Build final schema
    columns = []
    column_types = {}

    for col_name in sorted(all_columns.keys()):
        types = column_type_sets.get(col_name, set())

        if not types:
            # No data samples - default
            col_type = 'string'
        elif 'string' in types:
            col_type = 'string'
        elif len(types) == 1:
            col_type = types.pop()
        else:
            # Conflict - pick highest priority
            max_prio = -1
            col_type = 'string'
            for t in types:
                if TYPE_PRIORITY.get(t, 0) > max_prio:
                    max_prio = TYPE_PRIORITY[t]
                    col_type = t

        columns.append({'name': col_name, 'type': col_type})
        column_types[col_name] = col_type

    return columns, column_types


# =============================================================================
# Row Processing
# =============================================================================

def process_row(
    row: Dict[str, str],
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
        value = row.get(col_name, '')

        if not value:
            result.append(null_literal)
        else:
            try:
                typed = cast_value(value, col_type, error_strategy)
                result.append(format_value(typed, col_type))
            except ValueError:
                if error_strategy == 'fail':
                    raise
                result.append(null_literal)

    return result


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

    def _emit_sort_key(self, row: List[str], key_indices: List[int],
                       desc: bool) -> Tuple:
        """Build sort key tuple from row."""
        vals = [row[i] if i < len(row) else '' for i in key_indices]

        # Nulls (empty strings) compare less than non-nulls
        # Ascending: null < non-null => (1, val) for non-null, (0, '') for null
        # Descending: non-null > null => (1, val) for non-null, (0, '') for null
        # Actually simpler:
        # - Ascend: (is_null, value) where is_null=True sorts first (smaller)
        # - Descend: (is_null, value) but we want non-null first, so reverse value
        # Better: use (not is_null, -value_for_desc) pattern

        if desc:
            # Descending: non-nulls first, then nulls
            # Key: (has_value, -ord_of_value) - but values are strings
            # Simpler: (not is_null, inverted_value)
            return tuple((0 if v == '' else 1, self._invert_for_desc(v))
                        for v in vals)
        else:
            # Ascending: nulls first, then non-nulls
            # Key: (is_null, value) where is_null=True sorts first
            return tuple((0 if v == '' else 1, v) for v in vals)

    def _invert_for_desc(self, s: str) -> str:
        """Transform string for descending sort (non-lexicographic)."""
        # Use negative lexicographic order transformation
        # For strings, we need a custom approach
        # Use: negative of sorted characters would be complex
        # Instead: use a single char prefix trick with heap merge approach
        return s

    def sort_runs_and_merge(self,
        rows_generator,
        key_indices: List[int],
        desc: bool,
        num_cols: int
    ) -> List[List[str]]:
        """
        Sort data in runs and merge.
        Returns sorted rows.
        """
        run_mem_limit = self.memory_limit_bytes * 0.8

        # Build sorted runs
        current_run: List[Tuple] = []

        for row in rows_generator:
            # Estimate memory
            mem_est = sum(len(c) for c in row)

            if current_run and sum(len(r) for r in current_run) + mem_est > run_mem_limit:
                # Write current run
                self._write_run(current_run, key_indices, desc)
                current_run = []

            current_run.append(tuple(row))

        # Last run
        if current_run:
            self._write_run(current_run, key_indices, desc)
            current_run = []

        # Merge runs
        return self._merge_runs(key_indices, desc)

    def _write_run(self, rows: List[Tuple], key_indices: List[int], desc: bool):
        """Write a sorted run to temp file."""
        # Sort in-memory
        def sort_key(row_tuple):
            row = list(row_tuple)
            vals = [row[i] if i < len(row) else '' for i in key_indices]

            if desc:
                # Descending: non-null first
                # Create key: (not_is_null, inverted_value)
                # For strings, invert by using reversed representation
                inverted = []
                for v in vals:
                    if v == '':
                        inverted.append((0, ''))
                    else:
                        inverted.append((1, ''.join(chr(255 - ord(c)) for c in v)))
                return tuple(inverted)
            else:
                # Ascending: null first
                inverted = []
                for v in vals:
                    is_null = 1 if v == '' else 0
                    inverted.append((is_null, v))
                return tuple(inverted)

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
            if not row:
                return ()
            vals = [row[i] if i < len(row) else '' for i in key_indices]

            if desc:
                inverted = []
                for v in vals:
                    if v == '':
                        inverted.append((0, ''))
                    else:
                        inverted.append((1, ''.join(chr(255 - ord(c)) for c in v)))
                return tuple(inverted)
            else:
                inverted = []
                for v in vals:
                    is_null = 1 if v == '' else 0
                    inverted.append((is_null, v))
                return tuple(inverted)

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
# CSV Processing Pipeline
# =============================================================================

def read_csv_files(
    files: List[str],
    columns: List[Dict],
    column_types: Dict[str, str],
    error_strategy: str,
    null_literal: str
):
    """Generator yielding processed rows."""
    for filepath in files:
        try:
            with open(filepath, 'r', encoding='utf-8', newline='') as f:
                reader = csv.DictReader(f, delimiter=',')
                for row in reader:
                    yield process_row(row, columns, column_types,
                                     error_strategy, null_literal)
        except Exception as e:
            if error_strategy == 'fail':
                raise
            print(f"Warning: Skipping {filepath}: {e}", file=sys.stderr)


def write_output(
    rows: List[List[str]],
    columns: List[Dict],
    output_path: str
):
    """Write output CSV."""
    header = [c['name'] for c in columns]

    if output_path == '-':
        writer = csv.writer(sys.stdout, delimiter=',', quotechar='"',
                           quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
    else:
        with open(output_path, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=',', quotechar='"',
                               quoting=csv.QUOTE_MINIMAL)
            writer.writerow(header)
            for row in rows:
                writer.writerow(row)


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Merge and sort CSV files'
    )

    parser.add_argument('--output', '-o', required=True,
                        help='Output file (use "-" for stdout)')
    parser.add_argument('--key', '-k', required=True,
                        help='Sort key column(s), comma-separated')
    parser.add_argument('--desc', action='store_true',
                        help='Sort descending')
    parser.add_argument('--schema', help='JSON schema file')
    parser.add_argument('--infer', choices=['strict', 'loose'],
                        default='strict', help='Inference mode')
    parser.add_argument('--on-type-error',
                        choices=['coerce-null', 'fail', 'keep-string'],
                        default='coerce-null', help='Type error strategy')
    parser.add_argument('--memory-limit-mb', type=int, default=64,
                        help='Memory limit in MB')
    parser.add_argument('--temp-dir', help='Temp dir for sorting')
    parser.add_argument('--csv-quotechar', default='"',
                        help='CSV quote char')
    parser.add_argument('--csv-escapechar', default='',
                        help='CSV escape char (empty=disabled)')
    parser.add_argument('--csv-null-literal', default='',
                        help='Null value representation')
    parser.add_argument('input_files', nargs='+',
                        help='Input files')

    args = parser.parse_args()

    # Parse key columns
    key_columns = [k.strip() for k in args.key.split(',')]

    # Resolve schema
    columns, column_types = resolve_schema(
        args.infer, args.schema, args.input_files
    )

    # Validate key columns
    for kc in key_columns:
        if kc not in column_types:
            print(f"Error: Key column '{kc}' not in output schema",
                  file=sys.stderr)
            sys.exit(1)

    key_indices = [i for i, c in enumerate(columns) if c['name'] in key_columns]

    # Pipeline
    sorter = ExternalMergeSort(args.memory_limit_mb, args.temp_dir)

    generator = read_csv_files(
        args.input_files, columns, column_types,
        args.on_type_error, args.csv_null_literal
    )

    sorted_rows = sorter.sort_runs_and_merge(
        generator, key_indices, args.desc, len(columns)
    )

    write_output(sorted_rows, columns, args.output)


if __name__ == '__main__':
    main()
