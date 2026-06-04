#!/usr/bin/env python3
"""datagate - A CSV conversion and dataset query service."""

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
from urllib.parse import urlparse

import chardet
import requests
from flask import Flask, Response, request

app = Flask(__name__)

# Storage for datasets: id -> data
datasets = {}

# Map source URL to dataset id for determinism
url_to_id = {}


def generate_id(source_url: str) -> str:
    return hashlib.md5(source_url.encode('utf-8')).hexdigest()[:12]


def infer_delimiter(sample: str) -> str:
    """Infer CSV delimiter from sample content."""
    delimiters = [',', '\t', ';']
    sniffer = csv.Sniffer()

    # Try sniffer first
    try:
        dialect = sniffer.sniff(sample)
        return dialect.delimiter
    except csv.Error:
        pass

    # Manual detection
    best_delim = ','
    best_count = 0

    for delim in delimiters:
        count = sample.count(delim)
        if count > best_count:
            best_count = count
            best_delim = delim

    return best_delim


def is_time_like(value: str) -> bool:
    """Check if a string value looks like a time (e.g., 08:30, 9:15, 12:00)."""
    time_pattern = re.compile(r'^\d{1,2}:\d{2}$')
    return bool(time_pattern.match(value.strip()))


def convert_value(value: str):
    """Convert string value to appropriate type."""
    value = value.strip()

    if is_time_like(value):
        return value

    # Try integer
    try:
        return int(value)
    except ValueError:
        pass

    # Try float/decimal
    try:
        return float(value)
    except ValueError:
        pass

    # Return as string
    return value


def parse_csv(content: str, delimiter: str) -> dict:
    """Parse CSV content and return structured data."""
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)

    rows = list(reader)

    if len(rows) < 2:
        raise ValueError("CSV must have at least one header row and one data row")

    columns = rows[0]
    data_rows = rows[1:]

    # Convert each row with type inference
    converted_rows = []
    for row in data_rows:
        converted_row = [convert_value(cell) for cell in row]
        converted_rows.append(converted_row)

    return {
        'columns': columns,
        'rows': converted_rows
    }


def fetch_csv(source_url: str, charset: str = None) -> tuple:
    """Fetch CSV from remote URL and return (raw_content, encoding)."""
    try:
        response = requests.get(source_url, timeout=30)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")
    except Exception as e:
        raise

    content_bytes = response.content

    if not content_bytes.strip():
        raise ValueError("Empty content")

    # Determine encoding
    if charset:
        try:
            content = content_bytes.decode(charset)
            encoding = charset
        except (UnicodeDecodeError, LookupError):
            raise ValueError(f"Invalid charset: {charset}")
    else:
        detected = chardet.detect(content_bytes)
        encoding = detected.get('encoding', 'utf-8')
        try:
            content = content_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # Fallback to UTF-8
            encoding = 'utf-8'
            content = content_bytes.decode('utf-8', errors='replace')

    return content, encoding


def is_tabular(content: str) -> bool:
    """Check if content appears to be tabular data."""
    lines = content.strip().split('\n')
    if len(lines) < 2:
        return False

    # Check that first line (header) has multiple fields
    header = lines[0]
    delimiters = [',', '\t', ';']

    for delim in delimiters:
        if delim in header:
            # Verify data rows have similar structure
            fields_in_header = header.split(delim)
            for line in lines[1:4]:  # Check first few data rows
                if line.strip():
                    fields = line.split(delim)
                    if len(fields) != len(fields_in_header):
                        return False
            return True

    return False


@app.route('/convert')
def convert():
    """Convert remote CSV to dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    if not source:
        return json_error(400, "Missing 'source' parameter")

    # Validate URL
    try:
        parsed = urlparse(source)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("Invalid URL")
    except Exception:
        return json_error(400, "Invalid URL")

    if charset is not None and charset.strip() == '':
        return json_error(400, "Invalid charset: empty string")

    # Check if we already have this dataset
    if source in url_to_id:
        dataset_id = url_to_id[source]
        return json_success(200, {"ok": True, "endpoint": f"/datasets/{dataset_id}"})

    # Fetch the CSV
    try:
        content, encoding = fetch_csv(source, charset)
    except Exception as e:
        error_msg = str(e)
        # Remote HTTP 404 should return 404
        if "HTTP 404" in error_msg:
            return json_error(404, "Source unreachable")
        # Other errors are bad requests
        return json_error(400, error_msg)

    # Validate it's tabular
    if not is_tabular(content):
        return json_error(400, "Non-tabular content")

    # Infer delimiter and parse
    sample = content[:4096]  # Sample for delimiter detection
    delimiter = infer_delimiter(sample)

    try:
        dataset = parse_csv(content, delimiter)
    except ValueError as e:
        return json_error(400, str(e))

    # Generate deterministic ID
    dataset_id = generate_id(source)

    # Store dataset
    datasets[dataset_id] = dataset
    url_to_id[source] = dataset_id

    return json_success(200, {"ok": True, "endpoint": f"/datasets/{dataset_id}"})


@app.route('/datasets/<dataset_id>')
def get_dataset(dataset_id):
    """Retrieve dataset by ID with pagination, sorting, and response controls."""
    if dataset_id not in datasets:
        return json_error(404, "Dataset not found")

    start_time = time.time()

    dataset = datasets[dataset_id]
    columns = dataset['columns']
    rows = [list(row) for row in dataset['rows']]  # Create mutable copy

    # Parse control parameters
    args = request.args

    # Check for duplicate parameters
    param_names = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in param_names:
        if param in args:
            count = sum(1 for k in args.keys() if k == param)
            if count > 1:
                return json_error(400, f"Repeated parameter: {param}")

    # Validate and get _size
    size = 100
    if '_size' in args:
        size_str = args['_size']
        if not size_str.isdigit() or int(size_str) <= 0:
            return json_error(400, f"Invalid '_size': {size_str}")
        size = int(size_str)

    # Validate and get _offset
    offset = 0
    if '_offset' in args:
        offset_str = args['_offset']
        if not offset_str.isdigit() or int(offset_str) < 0:
            return json_error(400, f"Invalid '_offset': {offset_str}")
        offset = int(offset_str)

    # Validate and get _shape
    shape = 'lists'
    if '_shape' in args:
        shape = args['_shape']
        if shape not in ['lists', 'objects']:
            return json_error(400, f"Invalid '_shape': {shape}")

    # Validate _sort/_sort_desc columns and get sort parameters
    sort_col = args.get('_sort_desc') or args.get('_sort')
    sort_desc = '_sort_desc' in args

    # Validate sort column
    if sort_col is not None:
        if sort_col == '' or sort_col not in columns:
            return json_error(400, f"Unknown column for sorting: {sort_col}")
        col_idx = columns.index(sort_col)
        # Stable sort: for each row, use the sort column value, and for tie-breaking,
        # use the values of all columns in order
        def sort_key(row):
            key = [row[col_idx]]
            for c in columns:
                if c != sort_col:
                    key.append(row[columns.index(c)])
            return tuple(key)
        rows.sort(key=sort_key, reverse=sort_desc)

    total = len(rows)

    # Apply pagination
    paginated_rows = rows[offset:offset + size]

    # Format rows based on shape
    if shape == 'objects':
        formatted_rows = []
        for i, row in enumerate(paginated_rows):
            obj = {}
            for col_idx, col_name in enumerate(columns):
                obj[col_name] = row[col_idx]
            # rowid is 1-based source-file row number
            # Source row number = offset + i + 1 (since offset is 0-based row skip, and i is row index in paginated result)
            obj['rowid'] = offset + i + 1
            formatted_rows.append(obj)
    else:
        formatted_rows = paginated_rows

    # Validate visibility toggles
    show_rowid = True
    if '_rowid' in args:
        rowid_val = args['_rowid']
        if rowid_val != 'hide':
            return json_error(400, f"Invalid '_rowid' value: {rowid_val}")
        show_rowid = False

    show_total = True
    if '_total' in args:
        total_val = args['_total']
        if total_val != 'hide':
            return json_error(400, f"Invalid '_total' value: {total_val}")
        show_total = False

    query_ms = (time.time() - start_time) * 1000

    result = {
        "ok": True,
        "columns": columns,
        "rows": formatted_rows,
        "query_ms": round(query_ms, 1)
    }

    if show_total:
        result['total'] = total

    # If shape=objects and rowid should be hidden, remove it from the response
    if shape == 'objects' and not show_rowid:
        for row_obj in result['rows']:
            del row_obj['rowid']

    return json_success(200, result)


def json_error(status_code: int, message: str) -> Response:
    """Create a JSON error response."""
    response = Response(
        json.dumps({"ok": False, "error": message}),
        status=status_code,
        mimetype='application/json'
    )
    add_cors_headers(response)
    return response


def json_success(status_code: int, data: dict) -> Response:
    """Create a JSON success response."""
    response = Response(
        json.dumps(data),
        status=status_code,
        mimetype='application/json'
    )
    add_cors_headers(response)
    return response


def add_cors_headers(response: Response):
    """Add CORS headers to response."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'


@app.route('/<path:unknown_path>', methods=['GET', 'OPTIONS'])
def catch_all(unknown_path):
    """Handle unknown routes with 404."""
    return json_error(404, "Not found")


@app.before_request
def handle_options():
    """Handle OPTIONS requests for CORS preflight."""
    if request.method == 'OPTIONS':
        response = Response()
        add_cors_headers(response)
        return response


def main():
    parser = argparse.ArgumentParser(description='datagate CSV conversion service')
    parser.add_argument('command', choices=['start'])
    parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        app.run(host=args.address, port=args.port, debug=False)


if __name__ == '__main__':
    main()
