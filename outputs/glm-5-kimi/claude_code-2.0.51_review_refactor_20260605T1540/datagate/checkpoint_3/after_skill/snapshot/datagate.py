#!/usr/bin/env python3
"""
datagate - A CSV data ingestion and query service.
"""

import hashlib
import re
import time
from urllib.parse import urlparse

import chardet
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

datasets = {}


def generate_id(source_url):
    """Generate a deterministic ID from the source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def detect_encoding(content_bytes):
    """Detect character encoding from content bytes."""
    result = chardet.detect(content_bytes)
    return result['encoding'] or 'utf-8'


def infer_delimiter(text):
    """Infer the delimiter from CSV content."""
    first_line = text.split('\n')[0] if '\n' in text else text

    delimiters = [',', ';', '\t']
    counts = {}

    for delim in delimiters:
        counts[delim] = first_line.count(delim)

    if all(v == 0 for v in counts.values()):
        return ','

    return max(counts, key=counts.get)


def is_time_like(value):
    """Check if a value looks like a time (e.g., 08:30, 9:15, 12:00)."""
    if not isinstance(value, str):
        return False

    time_pattern = r'^\d{1,2}:\d{2}(:\d{2})?$'
    return bool(re.match(time_pattern, value.strip()))


def infer_type(value):
    """Infer the type of a value and convert it appropriately."""
    if value == '' or value is None:
        return ''

    value_str = str(value).strip()

    if is_time_like(value_str):
        return value_str

    try:
        if '.' not in value_str and 'e' not in value_str.lower():
            return int(value_str)
    except (ValueError, TypeError):
        pass

    try:
        return float(value_str)
    except (ValueError, TypeError):
        pass

    return value_str


def parse_csv(content_bytes, charset=None):
    """Parse CSV content bytes and return columns and rows."""
    if charset:
        try:
            text = content_bytes.decode(charset)
        except (UnicodeDecodeError, LookupError):
            raise ValueError(f"Invalid or unsupported charset: {charset}")
    else:
        encoding = detect_encoding(content_bytes)
        text = content_bytes.decode(encoding, errors='replace')

    delimiter = infer_delimiter(text)

    lines = text.strip().split('\n')
    if len(lines) < 2:
        raise ValueError("CSV must have at least one header row and one data row")

    header = parse_csv_line(lines[0], delimiter)
    columns = [col.strip() for col in header]

    if len(columns) == 0:
        raise ValueError("CSV must have at least one column")

    rows = []
    for line in lines[1:]:
        if line.strip():
            parsed_line = parse_csv_line(line, delimiter)
            while len(parsed_line) < len(columns):
                parsed_line.append('')
            parsed_line = parsed_line[:len(columns)]
            typed_row = [infer_type(val) for val in parsed_line]
            rows.append(typed_row)

    if len(rows) == 0:
        raise ValueError("CSV must have at least one data row")

    return columns, rows


def parse_csv_line(line, delimiter):
    """Parse a single CSV line, handling quoted fields."""
    result = []
    current = ''
    in_quotes = False
    i = 0

    while i < len(line):
        char = line[i]

        if char == '"':
            if in_quotes and i + 1 < len(line) and line[i + 1] == '"':
                current += '"'
                i += 2
                continue
            in_quotes = not in_quotes
            i += 1
            continue
        elif char == delimiter and not in_quotes:
            result.append(current)
            current = ''
            i += 1
            continue

        current += char

        i += 1

    result.append(current)
    return result


def is_valid_url(url):
    """Check if a URL is valid."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except (ValueError, TypeError, UnicodeDecodeError):
        return False


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


@app.route('/convert', methods=['GET'])
def convert():
    """Convert a remote CSV file to a dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    if not source:
        return jsonify({"ok": False, "error": "Missing 'source' parameter"}), 400

    if not is_valid_url(source):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    dataset_id = generate_id(source)

    if dataset_id in datasets:
        return jsonify({"ok": True, "endpoint": f"/datasets/{dataset_id}"})

    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return jsonify({"ok": False, "error": f"Source unreachable or remote HTTP error"}), 404

    content_bytes = response.content

    try:
        columns, rows = parse_csv(content_bytes, charset)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    return jsonify({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


def make_error(message):
    """Create a standardized error response."""
    return jsonify({"ok": False, "error": message}), 400


def parse_positive_int(value, param_name, allow_zero=False):
    """Parse a positive integer parameter, return (value, error) tuple."""
    error_msg = f"{param_name} must be a non-negative integer" if allow_zero else f"{param_name} must be a positive integer"

    try:
        int_val = int(value)
        if (allow_zero and int_val < 0) or (not allow_zero and int_val <= 0):
            return None, error_msg
        return int_val, None
    except (ValueError, TypeError):
        return None, error_msg


def validate_control_params(request_args):
    """
    Validate control parameters and return parsed values or error.
    Returns (params_dict, error_response) - one will be None.
    """
    # Check for duplicate control parameters
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in control_params:
        values = request_args.getlist(param)
        if len(values) > 1:
            return None, make_error(f"Duplicate control parameter: {param}")

    # Parse _size (default 100, positive integer)
    size = 100
    if '_size' in request_args:
        size_str = request_args.get('_size')
        size, err = parse_positive_int(size_str, "_size", allow_zero=False)
        if err:
            return None, make_error(err)

    # Parse _offset (default 0, non-negative integer)
    offset = 0
    if '_offset' in request_args:
        offset_str = request_args.get('_offset')
        offset, err = parse_positive_int(offset_str, "_offset", allow_zero=True)
        if err:
            return None, make_error(err)

    # Parse _shape (default 'lists')
    shape = 'lists'
    if '_shape' in request_args:
        shape = request_args.get('_shape')
        if shape not in ('lists', 'objects'):
            return None, make_error("_shape must be 'lists' or 'objects'")

    # Parse _sort and _sort_desc
    sort_column = None
    sort_desc = False
    if '_sort_desc' in request_args:
        sort_column = request_args.get('_sort_desc')
        sort_desc = True
    if '_sort' in request_args and not sort_desc:
        sort_column = request_args.get('_sort')

    # Validate non-empty sort column
    if sort_column is not None and sort_column == '':
        return None, make_error("_sort cannot be empty")

    # Also validate _sort_desc empty
    if '_sort_desc' in request_args:
        sort_desc_val = request_args.get('_sort_desc')
        if sort_desc_val == '':
            return None, make_error("_sort_desc cannot be empty")

    # Parse _rowid
    rowid_hide = False
    if '_rowid' in request_args:
        rowid_val = request_args.get('_rowid')
        if rowid_val != 'hide':
            return None, make_error("_rowid must be 'hide'")
        rowid_hide = True

    # Parse _total
    total_hide = False
    if '_total' in request_args:
        total_val = request_args.get('_total')
        if total_val != 'hide':
            return None, make_error("_total must be 'hide'")
        total_hide = True

    return {
        'size': size,
        'offset': offset,
        'shape': shape,
        'sort_column': sort_column,
        'sort_desc': sort_desc,
        'rowid_hide': rowid_hide,
        'total_hide': total_hide
    }, None


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    """Query a stored dataset."""
    start_time = time.time()

    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": "Dataset not found"}), 404

    dataset = datasets[dataset_id]
    columns = dataset["columns"]
    all_rows = dataset["rows"]

    # Validate and parse control parameters
    params, error = validate_control_params(request.args)
    if error:
        return error

    # Check if sort column is valid
    if params['sort_column'] is not None and params['sort_column'] not in columns:
        return make_error(f"Unknown column: {params['sort_column']}")

    # Apply sorting (stable)
    rows = all_rows[:]
    if params['sort_column'] is not None:
        col_index = columns.index(params['sort_column'])
        # Create indexed rows for stable sort (enumerate preserves original order for equal values)
        indexed_rows = list(enumerate(rows))
        if params['sort_desc']:
            indexed_rows.sort(key=lambda x: (x[1][col_index] is None, x[1][col_index] if x[1][col_index] is not None else ''), reverse=True)
        else:
            indexed_rows.sort(key=lambda x: (x[1][col_index] is None, x[1][col_index] if x[1][col_index] is not None else ''))
        rows = [r[1] for r in indexed_rows]

    # Calculate total before pagination
    total = len(rows)

    # Apply pagination
    offset = params['offset']
    size = params['size']
    rows = rows[offset:offset + size]

    query_ms = (time.time() - start_time) * 1000

    # Build response based on shape
    if params['shape'] == 'objects':
        result_rows = []
        for idx, row in enumerate(rows):
            row_dict = {"rowid": offset + idx + 1}
            for col_idx, col in enumerate(columns):
                row_dict[col] = row[col_idx]
            if params['rowid_hide']:
                del row_dict["rowid"]
            result_rows.append(row_dict)
    else:
        result_rows = rows

    response = {
        "ok": True,
        "columns": columns,
        "rows": result_rows,
        "query_ms": round(query_ms, 1)
    }

    if not params['total_hide']:
        response["total"] = total

    return jsonify(response)


@app.errorhandler(404)
def not_found(error):
    return jsonify({"ok": False, "error": "Not found"}), 404


def start_server(port=8001, address='127.0.0.1'):
    """Start the Flask server."""
    app.run(host=address, port=port)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='datagate - CSV data ingestion and query service')
    subparsers = parser.add_subparsers(dest='command')

    start_parser = subparsers.add_parser('start', help='Start the server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to')

    args = parser.parse_args()

    if args.command == 'start':
        start_server(port=args.port, address=args.address)
    else:
        parser.print_help()
