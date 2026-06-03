#!/usr/bin/env python3
"""
datagate: A service for converting remote CSV files to JSON datasets.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
from functools import lru_cache
from io import StringIO
from urllib.parse import urlparse

import chardet
import requests
from flask import Flask, jsonify, request
from werkzeug.routing import BaseConverter, ValidationError

app = Flask(__name__)

# In-memory dataset store: maps dataset_id -> (columns, rows)
datasets = {}


def infer_delimiter(sample):
    """Detect CSV delimiter from sample content, supporting , ; and \t."""
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=',;\t')
        return dialect.delimiter
    except csv.Error:
        # If sniffing fails, try to detect by counting occurrences of each delimiter
        commas = sample.count(',')
        semicolons = sample.count(';')
        tabs = sample.count('\t')

        if tabs > commas and tabs > semicolons:
            return '\t'
        elif semicolons > tabs and semicolons > commas:
            return ';'
        else:
            return ','


def detect_type(value):
    """
    Detect type of a value and return Python native type.
    Strings remain text, integers become int, decimals become float.
    Time-like values (HH:MM, H:MM, etc.) remain text.
    """
    if value is None or value == '':
        return ''

    # Try integer
    try:
        if re.match(r'^-?\d+$', value):
            return int(value)
    except ValueError:
        pass

    # Try float
    try:
        if re.match(r'^-?\d+(\.\d+)?$', value):
            return float(value)
    except ValueError:
        pass

    # Time-like values remain text
    if re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', value):
        return value

    return value


def parse_csv(content, delimiter):
    """
    Parse CSV content and return (columns, rows).
    Columns and rows are lists of values with type inference applied.
    """
    reader = csv.reader(StringIO(content), delimiter=delimiter)
    rows = list(reader)

    if len(rows) < 2:
        raise ValueError("CSV must have at least one header row and one data row")

    columns = rows[0]
    data_rows = rows[1:]

    # Apply type inference to each cell while preserving column order
    typed_rows = []
    for row in data_rows:
        typed_row = [detect_type(cell) for cell in row]
        typed_rows.append(typed_row)

    return columns, typed_rows


def get_dataset_id(source_url):
    """Generate deterministic dataset ID from source URL."""
    return hashlib.md5(source_url.encode('utf-8')).hexdigest()


def is_valid_url(url):
    """Validate URL format."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


@app.route('/convert', methods=['GET'])
def convert():
    """
    Convert a remote CSV file to a dataset.
    Query parameters:
      - source: URL of remote CSV (required)
      - charset: Character encoding (optional)
    """
    source = request.args.get('source', '')

    if not source:
        return jsonify({'ok': False, 'error': 'Missing source parameter'}), 400

    if not is_valid_url(source):
        return jsonify({'ok': False, 'error': 'Invalid URL'}), 400

    # Fetch remote CSV
    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
        content = response.content
    except requests.RequestException:
        return jsonify({'ok': False, 'error': 'Source unreachable or remote HTTP error'}), 404

    # Check for non-tabular content
    if not content or len(content.strip()) == 0:
        return jsonify({'ok': False, 'error': 'Non-tabular content'}), 400

    # Detect or use provided charset
    charset = request.args.get('charset')
    if charset:
        try:
            decoded = content.decode(charset)
        except (LookupError, UnicodeDecodeError):
            return jsonify({'ok': False, 'error': 'Unsupported or malformed charset'}), 400
    else:
        detected = chardet.detect(content)
        encoding = detected.get('encoding', 'utf-8')
        try:
            decoded = content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return jsonify({'ok': False, 'error': 'Unsupported or malformed charset'}), 400

    # Detect delimiter and parse CSV
    sample = decoded[:1024]  # Sample for delimiter detection
    try:
        delimiter = infer_delimiter(sample)
    except Exception:
        return jsonify({'ok': False, 'error': 'Delimiter detection failed'}), 400

    # Parse the full CSV
    try:
        columns, rows = parse_csv(decoded, delimiter)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception:
        return jsonify({'ok': False, 'error': 'Non-tabular content'}), 400

    # Store dataset
    dataset_id = get_dataset_id(source)
    datasets[dataset_id] = (columns, rows)

    endpoint = f'/datasets/{dataset_id}'
    return jsonify({'ok': True, 'endpoint': endpoint})


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    """
    Return a stored dataset by ID.
    Supports pagination (_size, _offset), sorting (_sort, _sort_desc),
    response shape (_shape), and visibility toggles (_rowid, _total).
    """
    if dataset_id not in datasets:
        return jsonify({'ok': False, 'error': 'Unknown dataset id'}), 404

    columns, rows = datasets[dataset_id]

    # Check for repeated control parameters
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in control_params:
        if param in request.args:
            values = request.args.getlist(param)
            if len(values) > 1:
                return jsonify({'ok': False, 'error': f'Repeated control parameter: {param}'}), 400

    # Parse and validate _size
    size = 100
    if '_size' in request.args:
        size_str = request.args.get('_size')
        if not size_str.isdigit() or size_str.startswith('0'):
            return jsonify({'ok': False, 'error': '_size must be a positive integer'}), 400
        size = int(size_str)
        if size <= 0:
            return jsonify({'ok': False, 'error': '_size must be a positive integer'}), 400

    # Parse and validate _offset
    offset = 0
    if '_offset' in request.args:
        offset_str = request.args.get('_offset')
        if not offset_str.isdigit():
            return jsonify({'ok': False, 'error': '_offset must be a non-negative integer'}), 400
        offset = int(offset_str)
        if offset < 0:
            return jsonify({'ok': False, 'error': '_offset must be a non-negative integer'}), 400

    # Parse and validate _shape
    shape = 'lists'
    if '_shape' in request.args:
        shape = request.args.get('_shape')
        if shape not in ('lists', 'objects'):
            return jsonify({'ok': False, 'error': '_shape must be lists or objects'}), 400

    # Parse and validate _rowid
    show_rowid = True
    if '_rowid' in request.args:
        rowid_val = request.args.get('_rowid')
        if rowid_val != 'hide':
            return jsonify({'ok': False, 'error': 'Invalid _rowid value'}), 400
        show_rowid = False

    # Parse and validate _total
    show_total = True
    if '_total' in request.args:
        total_val = request.args.get('_total')
        if total_val != 'hide':
            return jsonify({'ok': False, 'error': 'Invalid _total value'}), 400
        show_total = False

    # Parse and validate filter parameters
    valid_comparators = {'exact', 'contains', 'less', 'greater'}
    filters = {}  # column -> (comparator, value)
    filter_keys = []

    for key in request.args:
        # Skip control parameters (start with _)
        if key.startswith('_'):
            continue
        # Check if it's a filter (contains __)
        if '__' in key:
            filter_keys.append(key)

    # Check for duplicate filter keys
    if len(filter_keys) != len(set(filter_keys)):
        return jsonify({'ok': False, 'error': 'Duplicate filter key'}), 400

    # Parse each filter
    for key in filter_keys:
        parts = key.split('__', 1)
        if len(parts) != 2:
            continue
        column, comparator = parts

        # Validate comparator
        if comparator not in valid_comparators:
            return jsonify({'ok': False, 'error': f'Invalid comparator: {comparator}'}), 400

        # Check for unknown column
        if column not in columns:
            return jsonify({'ok': False, 'error': f'Unknown filter column: {column}'}), 400

        # For less/greater, validate that value is numeric
        value = request.args.get(key)
        if comparator in ('less', 'greater'):
            try:
                float(value)
            except (ValueError, TypeError):
                return jsonify({'ok': False, 'error': f'Comparator target not numeric: {key}'}), 400

        filters[key] = (column, comparator, value)

    # Parse and validate sorting
    sort_col = None
    sort_desc = False

    if '_sort' in request.args and '_sort_desc' in request.args:
        # _sort_desc wins if both present
        sort_col = request.args.get('_sort_desc')
        sort_desc = True
    elif '_sort' in request.args:
        sort_col = request.args.get('_sort')
        sort_desc = False
    elif '_sort_desc' in request.args:
        sort_col = request.args.get('_sort_desc')
        sort_desc = True

    # Validate sort column
    if sort_col is not None:
        if sort_col == '':
            return jsonify({'ok': False, 'error': 'Invalid sort column'}), 400
        if sort_col not in columns:
            return jsonify({'ok': False, 'error': 'Unknown column'}), 400

    # Apply filtering before sorting
    filtered_rows = list(rows)
    for key, (column, comparator, value) in filters.items():
        col_index = columns.index(column)
        if comparator == 'exact':
            filtered_rows = [row for row in filtered_rows if row[col_index] == value]
        elif comparator == 'contains':
            filtered_rows = [row for row in filtered_rows if str(value) in str(row[col_index])]
        elif comparator == 'less':
            try:
                filter_val = float(value)
                filtered_rows = [row for row in filtered_rows
                                 if isinstance(row[col_index], (int, float)) and float(row[col_index]) < filter_val]
            except (ValueError, TypeError):
                pass  # Already validated, but keep safe
        elif comparator == 'greater':
            try:
                filter_val = float(value)
                filtered_rows = [row for row in filtered_rows
                                 if isinstance(row[col_index], (int, float)) and float(row[col_index]) > filter_val]
            except (ValueError, TypeError):
                pass  # Already validated, but keep safe

    # Make a copy of rows for sorting
    sorted_rows = filtered_rows

    # Apply sorting before pagination
    if sort_col is not None:
        col_index = columns.index(sort_col)
        # Stable sort using Python's Timsort (stable)
        sorted_rows.sort(key=lambda row: row[col_index], reverse=sort_desc)

    # Apply pagination
    total = len(sorted_rows)
    paginated_rows = sorted_rows[offset:offset + size]

    # Format response based on shape
    if shape == 'objects':
        # Each row is an object with column values and rowid (1-based source file row number)
        result_rows = []
        for i, row in enumerate(paginated_rows):
            # rowid is 1-based source-file row number
            # Need to find the original row index in sorted_rows
            original_idx = offset + i
            obj_row = {'rowid': original_idx + 1}  # 1-based
            for j, col in enumerate(columns):
                obj_row[col] = row[j]
            result_rows.append(obj_row)
    else:
        # Default: rows are arrays
        result_rows = paginated_rows

    # Build response
    response = {
        'ok': True,
        'columns': columns,
        'rows': result_rows,
        'query_ms': 0.0
    }

    if show_total:
        response['total'] = total

    return jsonify(response)


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors for unknown routes."""
    return jsonify({'ok': False, 'error': 'Not found'}), 404


def main():
    parser = argparse.ArgumentParser(description='datagate - CSV to JSON dataset service')
    parser.add_argument('command', help='Command (start)')
    parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command != 'start':
        print(f"Unknown command: {args.command}")
        sys.exit(1)

    app.run(host=args.address, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
