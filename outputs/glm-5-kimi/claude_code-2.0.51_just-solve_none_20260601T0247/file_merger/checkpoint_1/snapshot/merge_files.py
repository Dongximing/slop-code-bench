#!/usr/bin/env python3
"""
CSV Merger and Sorter

A command-line tool that ingests multiple CSVs, aligns their schemas,
and produces one sorted CSV output.
"""

import argparse
import csv
import heapq
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Tuple,
)


# Type priority mapping (higher number = higher priority)
TYPE_PRIORITY = {
    "string": 1,
    "float": 2,
    "int": 3,
    "bool": 4,
    "date": 5,
    "timestamp": 6,
}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Merge and sort multiple CSV files"
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output file path or '-' for stdout",
    )
    parser.add_argument(
        "--key",
        required=True,
        help="Comma-separated list of key columns for sorting",
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
        "--infer",
        choices=["strict", "loose"],
        default="strict",
        help="Inference mode when schema not provided (default: strict)",
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
        "inputs",
        nargs="+",
        help="Input CSV files",
    )
    return parser.parse_args()


# --- Type Detection and Casting ---


def is_int(value: str) -> bool:
    """Check if value is a valid integer."""
    if not value:
        return False
    try:
        int(value)
        return True
    except ValueError:
        return False


def is_float(value: str) -> bool:
    """Check if value is a valid float."""
    if not value:
        return False
    try:
        float(value)
        return True
    except ValueError:
        return False


def is_bool(value: str) -> bool:
    """Check if value is a valid boolean."""
    if not value:
        return False
    return value.lower() in ("true", "false", "1", "0", "yes", "no")


def is_date(value: str) -> bool:
    """Check if value is a valid date (YYYY-MM-DD)."""
    if not value:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def is_timestamp(value: str) -> bool:
    """Check if value is a valid ISO-8601 timestamp."""
    if not value:
        return False
    try:
        parse_timestamp(value)
        return True
    except ValueError:
        return False


def parse_timestamp(value: str) -> str:
    """Parse timestamp and normalize to UTC with Z suffix."""
    # List of formats to try
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
                # No timezone, treat as UTC
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                # Convert to UTC
                dt = dt.astimezone(timezone.utc)
            # Format as UTC with Z suffix
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    raise ValueError(f"Cannot parse timestamp: {value}")


def cast_value(
    value: str,
    target_type: str,
    on_error: str,
    null_literal: str,
) -> Tuple[Optional[Any], bool]:
    """
    Cast a string value to the target type.

    Returns: (casted_value, success_flag)
    """
    if not value:
        return None, True

    try:
        if target_type == "string":
            return value, True
        elif target_type == "int":
            return int(value), True
        elif target_type == "float":
            return float(value), True
        elif target_type == "bool":
            if value.lower() in ("true", "1", "yes"):
                return "true", True
            elif value.lower() in ("false", "0", "no"):
                return "false", True
            raise ValueError(f"Invalid boolean: {value}")
        elif target_type == "date":
            # Validate and return as-is
            datetime.strptime(value, "%Y-%m-%d")
            return value, True
        elif target_type == "timestamp":
            return parse_timestamp(value), True
        else:
            raise ValueError(f"Unknown type: {target_type}")
    except (ValueError, TypeError) as e:
        if on_error == "coerce-null":
            return None, True
        elif on_error == "fail":
            return None, False
        elif on_error == "keep-string":
            return value, True
        return None, True


def infer_type(value: str, mode: str) -> Optional[str]:
    """
    Infer the type of a value.

    Returns: type name or None if empty
    """
    if not value:
        return None

    # Check types in priority order (highest priority first)
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


def resolve_type(types: List[Optional[str]], mode: str) -> str:
    """
    Resolve the final type from a list of observed types.

    strict mode: if conflicting types, fall back to string
    loose mode: prefer numeric/temporal if all non-null values parse
    """
    non_null_types = [t for t in types if t is not None]

    if not non_null_types:
        return "string"

    unique_types = set(non_null_types)

    if len(unique_types) == 1:
        return non_null_types[0]

    if mode == "strict":
        # Conflicting types, fall back to string
        return "string"
    else:  # loose mode
        # Try to find common supertype
        # Check if all can be numeric
        numeric_types = {"int", "float"}
        if unique_types.issubset(numeric_types):
            return "float"
        # Check if all can be temporal
        temporal_types = {"date", "timestamp"}
        if unique_types.issubset(temporal_types):
            return "timestamp"
        return "string"


# --- Schema Handling ---


def load_schema(schema_path: str) -> List[Tuple[str, str]]:
    """Load schema from JSON file."""
    with open(schema_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    columns = []
    for col in data["columns"]:
        columns.append((col["name"], col["type"]))

    return columns


def infer_schema_from_files(
    input_files: List[str],
    mode: str,
    quotechar: str,
    escapechar: Optional[str],
) -> List[Tuple[str, str]]:
    """Infer schema from input files."""
    # Collect all headers and type observations
    all_headers = set()
    column_values: Dict[str, List[Optional[str]]] = {}

    for filepath in input_files:
        with open(filepath, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(
                f,
                quotechar=quotechar,
                doublequote=True,
            )

            try:
                header = next(reader)
            except StopIteration:
                continue

            all_headers.update(header)

            for row in reader:
                for i, col_name in enumerate(header):
                    if col_name not in column_values:
                        column_values[col_name] = []

                    if i < len(row):
                        value = row[i]
                        inferred = infer_type(value, mode)
                        column_values[col_name].append(inferred)
                    else:
                        column_values[col_name].append(None)

    # Determine column order (lexicographic)
    sorted_headers = sorted(all_headers)

    # Resolve types
    schema = []
    for col_name in sorted_headers:
        types = column_values.get(col_name, [])
        col_type = resolve_type(types, mode)
        schema.append((col_name, col_type))

    return schema


# --- CSV Reading with Schema Alignment ---


def read_csv_with_schema(
    filepath: str,
    schema: List[Tuple[str, str]],
    on_error: str,
    null_literal: str,
    quotechar: str,
    escapechar: Optional[str],
) -> Generator[Dict[str, Optional[str]], None, None]:
    """Read CSV and align to schema."""
    schema_cols = [col[0] for col in schema]
    schema_types = {col[0]: col[1] for col in schema}

    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(
            f,
            quotechar=quotechar,
            doublequote=True,
        )

        try:
            header = next(reader)
        except StopIteration:
            header = []

        header_index = {name: i for i, name in enumerate(header)}

        for row in reader:
            result = {}
            for col_name in schema_cols:
                if col_name in header_index:
                    idx = header_index[col_name]
                    if idx < len(row):
                        raw_value = row[idx]
                        if raw_value:
                            casted, success = cast_value(
                                raw_value,
                                schema_types[col_name],
                                on_error,
                                null_literal,
                            )
                            if not success:
                                print(
                                    f"Error: Cannot cast '{raw_value}' to "
                                    f"{schema_types[col_name]} in column "
                                    f"'{col_name}'",
                                    file=sys.stderr,
                                )
                                sys.exit(1)
                            result[col_name] = casted
                        else:
                            result[col_name] = None
                    else:
                        result[col_name] = None
                else:
                    result[col_name] = None
            yield result


# --- Sorting ---


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
    row: Dict[str, Optional[str]],
    key_columns: List[str],
    descending: bool,
) -> Tuple:
    """
    Create a sort key for a row.

    Nulls always compare less than non-null values.
    """
    key_parts = []
    for col in key_columns:
        value = row.get(col)
        if value is None:
            # Nulls always sort first (lowest)
            # Use special marker that sorts before anything
            if descending:
                # In descending, nulls should be last (highest)
                key_parts.append((2, 0))  # (is_null, dummy) - high value
            else:
                # In ascending, nulls should be first (lowest)
                key_parts.append((0, ""))  # (is_null, dummy) - low value
        else:
            # Convert to string for consistent comparison
            str_value = str(value)
            if descending:
                # Reverse the comparison for non-null values
                key_parts.append((1, ReverseComparator(str_value)))
            else:
                key_parts.append((1, str_value))

    return tuple(key_parts)


class RowComparator:
    """Comparator for rows that handles null values and stability."""

    def __init__(self, key_columns: List[str], descending: bool):
        self.key_columns = key_columns
        self.descending = descending

    def get_key(self, row_with_order: Tuple[int, Dict]) -> Tuple:
        """Get sort key for a row."""
        order, row = row_with_order
        return (make_sort_key(row, self.key_columns, self.descending), order)


# --- External Merge Sort ---


def external_sort(
    rows: Iterator[Dict],
    key_columns: List[str],
    descending: bool,
    memory_limit_mb: int,
    temp_dir: str,
    schema: List[Tuple[str, str]],
    null_literal: str,
) -> Generator[Dict, None, None]:
    """
    External merge sort for handling large datasets.

    1. Read rows in batches that fit in memory
    2. Sort each batch and write to temp file
    3. Merge sorted temp files using heap
    """
    memory_limit_bytes = memory_limit_mb * 1024 * 1024

    # Estimate row size (rough estimate)
    sample_rows = []
    row_count = 0
    total_size = 0

    # Phase 1: Create sorted chunks
    temp_files = []
    current_batch = []
    current_size = 0
    order = 0

    try:
        for row in rows:
            # Estimate size of row
            row_size = sum(
                len(str(v)) if v is not None else 0 for v in row.values()
            )
            row_size += len(row) * 8  # overhead estimate

            if current_size + row_size > memory_limit_bytes and current_batch:
                # Sort and write current batch
                temp_file = write_sorted_batch(
                    current_batch,
                    key_columns,
                    descending,
                    temp_dir,
                    schema,
                    order - len(current_batch),
                )
                temp_files.append(temp_file)
                current_batch = []
                current_size = 0

            current_batch.append(row)
            current_size += row_size
            order += 1

        # Write remaining batch
        if current_batch:
            temp_file = write_sorted_batch(
                current_batch,
                key_columns,
                descending,
                temp_dir,
                schema,
                order - len(current_batch),
            )
            temp_files.append(temp_file)

        # Phase 2: Merge sorted files
        yield from merge_sorted_files(
            temp_files, key_columns, descending, schema, null_literal
        )

    finally:
        # Clean up temp files
        for tf in temp_files:
            try:
                os.remove(tf)
            except OSError:
                pass


def write_sorted_batch(
    batch: List[Dict],
    key_columns: List[str],
    descending: bool,
    temp_dir: str,
    schema: List[Tuple[str, str]],
    start_order: int,
) -> str:
    """Sort a batch and write to temp file."""
    # Add order for stability
    batch_with_order = [(i + start_order, row) for i, row in enumerate(batch)]

    # Sort
    def sort_key(item):
        order, row = item
        return (make_sort_key(row, key_columns, descending), order)

    batch_with_order.sort(key=sort_key)

    # Write to temp file
    fd, temp_path = tempfile.mkstemp(suffix=".csv", dir=temp_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            # Write order and row
            for order, row in batch_with_order:
                row_data = [str(row.get(col[0], "")) for col in schema]
                writer.writerow([order] + row_data)
    except:
        try:
            os.remove(temp_path)
        except:
            pass
        raise

    return temp_path


def read_sorted_file(
    filepath: str,
    schema: List[Tuple[str, str]],
) -> Generator[Tuple[int, Dict], None, None]:
    """Read rows from a sorted temp file."""
    with open(filepath, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            order = int(row[0])
            row_dict = {}
            for i, col in enumerate(schema):
                if i + 1 < len(row):
                    row_dict[col[0]] = row[i + 1] if row[i + 1] else None
                else:
                    row_dict[col[0]] = None
            yield order, row_dict


def merge_sorted_files(
    temp_files: List[str],
    key_columns: List[str],
    descending: bool,
    schema: List[Tuple[str, str]],
    null_literal: str,
) -> Generator[Dict, None, None]:
    """Merge sorted temp files using heap."""
    if not temp_files:
        return

    if len(temp_files) == 1:
        for _, row in read_sorted_file(temp_files[0], schema):
            yield row
        return

    # Create iterators for each file
    file_iters = []
    for filepath in temp_files:
        file_iters.append(read_sorted_file(filepath, schema))

    # Initialize heap with first element from each file
    heap = []
    file_index = 0

    def get_key(item):
        order, row, fidx = item
        return (make_sort_key(row, key_columns, descending), order)

    for i, it in enumerate(file_iters):
        try:
            order, row = next(it)
            heap.append((order, row, i))
        except StopIteration:
            pass

    # Sort initial heap
    heap.sort(key=get_key)

    while heap:
        order, row, fidx = heap.pop(0)
        yield row

        try:
            next_order, next_row = next(file_iters[fidx])
            # Insert maintaining sorted order (binary search)
            new_item = (next_order, next_row, fidx)
            inserted = False
            for i, existing in enumerate(heap):
                if get_key(new_item) < get_key(existing):
                    heap.insert(i, new_item)
                    inserted = True
                    break
            if not inserted:
                heap.append(new_item)
        except StopIteration:
            pass


# --- Output Writing ---


def write_output(
    rows: Iterator[Dict],
    output_path: str,
    schema: List[Tuple[str, str]],
    null_literal: str,
    quotechar: str,
    escapechar: Optional[str],
) -> None:
    """Write rows to output file or stdout."""
    if output_path == "-":
        output_file = sys.stdout
        need_close = False
    else:
        output_file = open(output_path, "w", encoding="utf-8", newline="")
        need_close = True

    try:
        # Use doublequote for escaping by default (RFC 4180)
        writer = csv.writer(
            output_file,
            quotechar=quotechar,
            doublequote=True,
            lineterminator="\n",
        )

        # Write header
        header = [col[0] for col in schema]
        writer.writerow(header)

        # Write rows
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


# --- Main ---


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Parse key columns
    key_columns = [k.strip() for k in args.key.split(",")]

    # Setup quote/escape characters
    quotechar = args.csv_quotechar
    escapechar = args.csv_escapechar
    if escapechar is None:
        # Default: double the quote character
        doublequote = True
        escapechar = ""
    else:
        doublequote = False

    # Setup temp directory
    temp_dir = args.temp_dir
    cleanup_temp = False
    if temp_dir is None:
        temp_dir = tempfile.mkdtemp(prefix="csv_merge_")
        cleanup_temp = True

    try:
        # Get schema
        if args.schema:
            schema = load_schema(args.schema)
        else:
            schema = infer_schema_from_files(
                args.inputs, args.infer, quotechar, escapechar
            )

        # Validate key columns exist in schema
        schema_cols = set(col[0] for col in schema)
        for key_col in key_columns:
            if key_col not in schema_cols:
                print(
                    f"Error: Key column '{key_col}' not in schema",
                    file=sys.stderr,
                )
                return 1

        # Create row generator
        def row_generator():
            for filepath in args.inputs:
                yield from read_csv_with_schema(
                    filepath,
                    schema,
                    args.on_type_error,
                    args.csv_null_literal,
                    quotechar,
                    escapechar,
                )

        # Sort with external merge if needed
        # Estimate if we need external sort
        total_size = 0
        for filepath in args.inputs:
            try:
                total_size += os.path.getsize(filepath)
            except OSError:
                pass

        memory_bytes = args.memory_limit_mb * 1024 * 1024

        if total_size > memory_bytes * 0.5:  # Use external sort for large data
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
            # In-memory sort for small data
            rows_list = list(row_generator())
            rows_with_order = list(enumerate(rows_list))
            rows_with_order.sort(
                key=lambda x: (
                    make_sort_key(x[1], key_columns, args.desc),
                    x[0],
                )
            )
            sorted_rows = (row for _, row in rows_with_order)

        # Write output
        write_output(
            sorted_rows,
            args.output,
            schema,
            args.csv_null_literal,
            quotechar,
            escapechar,
        )

        return 0

    finally:
        if cleanup_temp:
            try:
                shutil.rmtree(temp_dir)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
