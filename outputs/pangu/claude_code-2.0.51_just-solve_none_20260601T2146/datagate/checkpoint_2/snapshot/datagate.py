#!/usr/bin/env python3
"""Datagate - CSV data gateway service."""

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
from flask import Flask, jsonify, request

app = Flask(__name__)

datasets = {}

DELIMITER_CHARS = [',', ';', '\t']


def is_valid_url(url):
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def detect_delimiter(sample):
    try:
        return csv.Sniffer().sniff(sample, delimiters=DELIMITER_CHARS).delimiter
    except Exception:
        return ','


def infer_type(value):
    if not isinstance(value, str):
        return value

    s = value.strip()
    if not s or re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', s):
        return s

    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
        if f == f and f != float('inf') and f != float('-inf'):
            return f
    except ValueError:
        pass
    return s


def detect_charset(content, provided_charset=None):
    if provided_charset:
        try:
            content.decode(provided_charset)
            return provided_charset
        except (LookupError, UnicodeDecodeError):
            raise ValueError(f"Unsupported or malformed charset: {provided_charset}")
    result = chardet.detect(content)
    encoding = result.get('encoding', 'utf-8')
    if encoding is None:
        encoding = 'utf-8'
    return encoding


def fetch_csv(source_url, charset=None):
    try:
        response = requests.get(source_url, timeout=30)
        if response.status_code != 200:
            raise requests.RequestException(f"HTTP {response.status_code}")
    except requests.RequestException as e:
        raise ValueError(f"Source unreachable or remote HTTP error: {e}")

    content = response.content
    encoding = detect_charset(content, charset)
    return content.decode(encoding)


def parse_csv(text):
    if not text.strip():
        raise ValueError("Empty content")

    delimiter = detect_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)

    if len(rows) < 2:
        raise ValueError("Non-tabular content or insufficient data (requires at least header and one data row)")

    columns = rows[0]
    data_rows = rows[1:]

    if not all(len(row) == len(columns) for row in data_rows):
        raise ValueError("Non-tabular content - inconsistent column counts")

    return columns, [[infer_type(cell) for cell in row] for row in data_rows]


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


@app.route('/convert', methods=['GET'])
def convert():
    source = request.args.get('source')
    if not source:
        return jsonify({"ok": False, "error": "Missing source parameter"}), 400
    if not is_valid_url(source):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    try:
        text = fetch_csv(source, request.args.get('charset'))
        columns, rows = parse_csv(text)
    except ValueError as e:
        error_msg = str(e)
        if 'charset' in error_msg.lower() or 'non-tabular' in error_msg.lower() or 'inconsistent' in error_msg.lower():
            return jsonify({"ok": False, "error": error_msg}), 400
        return jsonify({"ok": False, "error": error_msg}), 404 if 'unreachable' in error_msg.lower() else 400

    dataset_id = hashlib.md5(source.encode()).hexdigest()
    datasets[dataset_id] = {'columns': columns, 'rows': rows}
    return jsonify({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": f"Unknown dataset id: {dataset_id}"}), 404

    data = datasets[dataset_id]
    columns = data['columns']
    rows = data['rows']
    start = time.time()

    # Check for duplicate control parameters
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in control_params:
        if param in request.args:
            # Check if parameter appears multiple times
            # Flask's request.args gives the last value if duplicated, so we need to check the query string
            if f'{param}=' in request.query_string.decode('utf-8'):
                query = request.query_string.decode('utf-8')
                # Count occurrences of the parameter
                count = query.count(f'{param}=')
                if count > 1:
                    return jsonify({"ok": False, "error": f"Repeated control parameter: {param}"}), 400

    # Parse and validate _size
    size = 100
    if '_size' in request.args:
        size_str = request.args.get('_size')
        if not size_str.isdigit() or size_str.startswith('0'):
            return jsonify({"ok": False, "error": f"Invalid _size: {size_str}"}), 400
        size = int(size_str)
        if size <= 0:
            return jsonify({"ok": False, "error": f"Invalid _size: {size_str}"}), 400

    # Parse and validate _offset
    offset = 0
    if '_offset' in request.args:
        offset_str = request.args.get('_offset')
        if not offset_str.isdigit():
            return jsonify({"ok": False, "error": f"Invalid _offset: {offset_str}"}), 400
        offset = int(offset_str)
        if offset < 0:
            return jsonify({"ok": False, "error": f"Invalid _offset: {offset_str}"}), 400

    # Parse and validate _sort / _sort_desc
    sort_column = None
    sort_desc = False
    if '_sort' in request.args and '_sort_desc' in request.args:
        # _sort_desc wins if both present
        sort_desc = True
        sort_column = request.args.get('_sort_desc')
    elif '_sort' in request.args:
        sort_column = request.args.get('_sort')
        sort_desc = False
    elif '_sort_desc' in request.args:
        sort_column = request.args.get('_sort_desc')
        sort_desc = True

    if sort_column is not None:
        if sort_column == '':
            return jsonify({"ok": False, "error": "Empty sort column"}), 400
        if sort_column not in columns:
            return jsonify({"ok": False, "error": f"Unknown column: {sort_column}"}), 400

    # Parse and validate _shape
    shape = 'lists'
    if '_shape' in request.args:
        shape = request.args.get('_shape')
        if shape not in ['lists', 'objects']:
            return jsonify({"ok": False, "error": f"Invalid _shape: {shape}"}), 400

    # Parse and validate _rowid toggle
    show_rowid = True
    if '_rowid' in request.args:
        rowid_val = request.args.get('_rowid')
        if rowid_val != 'hide':
            return jsonify({"ok": False, "error": f"Invalid _rowid value: {rowid_val}"}), 400
        show_rowid = False

    # Parse and validate _total toggle
    show_total = True
    if '_total' in request.args:
        total_val = request.args.get('_total')
        if total_val != 'hide':
            return jsonify({"ok": False, "error": f"Invalid _total value: {total_val}"}), 400
        show_total = False

    # Get total count before pagination
    total = len(rows)

    # Track original row numbers (1-based) before sorting
    # This is needed for objects shape to include rowid
    rows_with_id = [(i + 1, row) for i, row in enumerate(rows)]

    # Apply sorting
    if sort_column is not None:
        col_idx = columns.index(sort_column)
        # Stable sort - Python's sort is stable
        rows_with_id = sorted(rows_with_id, key=lambda x: x[1][col_idx], reverse=sort_desc)

    # Apply pagination
    paginated = rows_with_id[offset:offset + size]

    # Format response based on shape
    if shape == 'objects':
        # For objects shape, each row is an object
        result_rows = []
        for rowid, row in paginated:
            row_obj = {}
            # Only include rowid if not hidden
            if show_rowid:
                row_obj['rowid'] = rowid
            for i, col in enumerate(columns):
                row_obj[col] = row[i]
            result_rows.append(row_obj)
    else:
        # lists shape - rows are arrays, no rowid
        result_rows = [row for _, row in paginated]

    # Build response
    response = {
        "ok": True,
        "columns": columns,
        "rows": result_rows
    }

    if show_total:
        response["total"] = total

    response["query_ms"] = (time.time() - start) * 1000

    return jsonify(response)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=['GET'])
def catch_all(path):
    return jsonify({"ok": False, "error": "Not found"}), 404


def main():
    parser = argparse.ArgumentParser(description='Datagate CSV Gateway')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    start_parser = subparsers.add_parser('start', help='Start the server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        app.run(host=args.address, port=args.port, debug=False)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
