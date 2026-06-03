#!/usr/bin/env python3
"""datagate - CSV conversion and dataset serving service."""

import argparse
import csv
import hashlib
import json
import re
import statistics
import sys
import time
from io import StringIO
from urllib.parse import urlparse

import chardet
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory storage for datasets: {id: {"columns": [...], "rows": [[...], ...]}}
datasets = {}


def is_valid_url(url):
    """Check if the URL is valid."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def detect_encoding_if_needed(content, charset):
    """Detect encoding if not provided, otherwise validate."""
    if charset:
        try:
            # Validate the charset by attempting to decode
            content.decode(charset)
            return charset
        except (LookupError, UnicodeDecodeError):
            return None
    else:
        # Auto-detect encoding
        detection = chardet.detect(content)
        return detection.get('encoding')


def detect_delimiter(sample):
    """Detect CSV delimiter from sample content."""
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample)
        return dialect.delimiter
    except csv.Error:
        return None


def infer_type(value):
    """Infer the type of a value: int, float, or string."""
    if not value or value.strip() == '':
        return value

    # Check for integer
    if re.match(r'^-?\d+$', value):
        return int(value)

    # Check for decimal/float
    if re.match(r'^-?\d+\.\d*$', value):
        try:
            return float(value)
        except ValueError:
            pass
    if re.match(r'^-\d*\.\d+$', value):
        try:
            return float(value)
        except ValueError:
            pass

    # Check for time-like values (e.g., "08:30", "9:15", "12:00")
    # Keep time-like values as strings
    if re.match(r'^\d{1,2}:\d{2}$', value):
        return value

    return value


def parse_csv_content(content, encoding):
    """Parse CSV content and return columns and rows with type inference."""
    # Decode content
    decoded = content.decode(encoding)

    lines = decoded.strip().split('\n')
    if len(lines) < 2:
        return None, None  # Need at least header and one data row

    # Detect the most likely delimiter and count occurrences
    # Only accept if delimiter appears consistently
    delimiters = [',', ';', '\t']
    best_delimiter = None
    best_score = 0

    for delim in delimiters:
        # Count how many lines have this delimiter
        count = 0
        total_delims = 0
        for line in lines:
            delim_count = line.count(delim)
            if delim_count > 0:
                count += 1
                total_delims += delim_count
        # Score: more lines with delimiter is better
        # Also prefer delimiters that appear consistently (same count per line)
        if count > 0:
            # Calculate consistency: std dev of delimiter counts
            import statistics
            delim_counts = [line.count(delim) for line in lines if line.count(delim) > 0]
            if delim_counts:
                consistency = statistics.stdev(delim_counts) if len(delim_counts) > 1 else 0
                score = count * 100 - consistency * len(delim_counts)
                if score > best_score:
                    best_score = score
                    best_delimiter = delim

    if best_delimiter is None:
        return None, None

    # Parse CSV with detected delimiter
    reader = csv.reader(lines, delimiter=best_delimiter)

    # Get header
    try:
        header = next(reader)
    except StopIteration:
        return None, None

    if not header:
        return None, None

    columns = [col.strip() for col in header]
    num_columns = len(columns)

    # Reject single-column "CSV" as non-tabular (typically random text)
    if num_columns < 2:
        return None, None

    # Get data rows
    rows = []
    has_data_row = False
    for row in reader:
        if row:  # Skip empty rows
            # Ensure row has same number of columns as header
            if len(row) != num_columns:
                return None, None

            # Infer types for each value
            typed_row = [infer_type(val) for val in row]
            rows.append(typed_row)
            has_data_row = True

    if not has_data_row:
        return None, None

    return columns, rows


def generate_dataset_id(source_url):
    """Generate deterministic dataset ID from source URL."""
    return hashlib.md5(source_url.encode()).hexdigest()[:12]


def fetch_and_parse_csv(source_url, charset=None):
    """Fetch CSV from URL and parse it."""
    try:
        response = requests.get(source_url, timeout=30)
        if response.status_code != 200:
            return None, f"Source unreachable or remote HTTP error: {response.status_code}"

        content = response.content

        if len(content) == 0:
            return None, "Empty content"

        # Handle encoding
        actual_charset = detect_encoding_if_needed(content, charset)
        if actual_charset is None:
            return None, "Unsupported or malformed charset"

        # Parse CSV
        columns, rows = parse_csv_content(content, actual_charset)
        if columns is None:
            return None, "Non-tabular content or missing header/data rows"

        return {'columns': columns, 'rows': rows}, None

    except requests.RequestException as e:
        return None, f"Source unreachable or remote HTTP error: {str(e)}"
    except Exception as e:
        return None, f"Non-tabular content: {str(e)}"


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = '*'
    return response


@app.route('/convert', methods=['GET'])
def convert():
    """Convert remote CSV to dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    # Validate source parameter
    if not source:
        return jsonify({'ok': False, 'error': 'Missing source parameter'}), 400

    # Validate URL
    if not is_valid_url(source):
        return jsonify({'ok': False, 'error': 'Invalid URL'}), 400

    # Generate deterministic ID
    dataset_id = generate_dataset_id(source)

    # Check if we already have this dataset cached
    if dataset_id in datasets:
        return jsonify({'ok': True, 'endpoint': f'/datasets/{dataset_id}'})

    # Fetch and parse CSV
    result, error = fetch_and_parse_csv(source, charset)

    if error:
        # Determine error type
        if 'Missing source' in error or 'Invalid URL' in error or 'charset' in error or 'Non-tabular' in error:
            return jsonify({'ok': False, 'error': error}), 400
        else:
            return jsonify({'ok': False, 'error': error}), 404

    # Store dataset
    datasets[dataset_id] = result

    return jsonify({'ok': True, 'endpoint': f'/datasets/{dataset_id}'})


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    if dataset_id not in datasets:
        return jsonify({'ok': False, 'error': 'Dataset not found'}), 404

    start_time = time.time()

    dataset = datasets[dataset_id]
    columns = dataset['columns']
    all_rows = dataset['rows']

    # Check for duplicate control parameters
    # We need to check the raw query string since Flask's request.args only gives the last value
    query_string = request.query_string.decode('utf-8') if request.query_string else ''
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in control_params:
        # Count occurrences of the parameter as a distinct query parameter
        # Pattern: either start of string or & followed by param=
        import re
        pattern = r'(?:^|&)' + re.escape(param) + r'='
        matches = re.findall(pattern, query_string)
        if len(matches) > 1:
            return jsonify({'ok': False, 'error': f'Repeated control parameter: {param}'}), 400

    # Parse and validate _size
    size = 100  # default
    if '_size' in request.args:
        size_str = request.args.get('_size')
        if not size_str.isdigit() or int(size_str) <= 0:
            return jsonify({'ok': False, 'error': f'Invalid _size: {size_str}'}), 400
        size = int(size_str)

    # Parse and validate _offset
    offset = 0  # default
    if '_offset' in request.args:
        offset_str = request.args.get('_offset')
        if not offset_str.isdigit() or int(offset_str) < 0:
            return jsonify({'ok': False, 'error': f'Invalid _offset: {offset_str}'}), 400
        offset = int(offset_str)

    # Parse and validate _shape
    shape = 'lists'  # default
    if '_shape' in request.args:
        shape = request.args.get('_shape')
        if shape not in ('lists', 'objects'):
            return jsonify({'ok': False, 'error': f'Invalid _shape: {shape}'}), 400

    # Parse and validate _rowid
    rowid_visibility = 'show'  # default (show rowid in objects shape)
    if '_rowid' in request.args:
        rowid_val = request.args.get('_rowid')
        if rowid_val != 'hide':
            return jsonify({'ok': False, 'error': f'Invalid _rowid value: {rowid_val}'}), 400
        rowid_visibility = 'hide'

    # Parse and validate _total
    total_visibility = 'show'  # default
    if '_total' in request.args:
        total_val = request.args.get('_total')
        if total_val != 'hide':
            return jsonify({'ok': False, 'error': f'Invalid _total value: {total_val}'}), 400
        total_visibility = 'hide'

    # Parse sorting parameters
    sort_column = None
    sort_desc = False

    if '_sort' in request.args and '_sort_desc' in request.args:
        # Both present: _sort_desc wins
        sort_column = request.args.get('_sort_desc')
        sort_desc = True
    elif '_sort' in request.args:
        sort_column = request.args.get('_sort')
        sort_desc = False
    elif '_sort_desc' in request.args:
        sort_column = request.args.get('_sort_desc')
        sort_desc = True

    # Validate sort column if present
    if sort_column is not None:
        if sort_column == '' or sort_column not in columns:
            return jsonify({'ok': False, 'error': f'Invalid sort column: {sort_column}'}), 400

    # Create rows with original row numbers (1-based source-file row number)
    # Each element is (original_rownum, row_data)
    rows_with_ids = [(i + 1, row) for i, row in enumerate(all_rows)]

    # Apply sorting (stable sort)
    if sort_column is not None:
        col_index = columns.index(sort_column)
        # Python's sort is stable, so we sort by the column value
        rows_with_ids.sort(key=lambda x: x[1][col_index], reverse=sort_desc)

    # Get total count before pagination
    total_count = len(rows_with_ids)

    # Apply pagination
    paginated_rows_with_ids = rows_with_ids[offset:offset + size]

    # Format rows based on shape
    if shape == 'lists':
        formatted_rows = [row for _, row in paginated_rows_with_ids]
    else:  # shape == 'objects'
        formatted_rows = []
        for original_rownum, row in paginated_rows_with_ids:
            row_obj = {}
            # Include rowid only if not hidden
            if rowid_visibility == 'show':
                row_obj['rowid'] = original_rownum
            for j, col_name in enumerate(columns):
                row_obj[col_name] = row[j]
            formatted_rows.append(row_obj)

    query_ms = (time.time() - start_time) * 1000

    # Build response
    response = {
        'ok': True,
        'columns': columns,
        'rows': formatted_rows,
        'query_ms': round(query_ms, 1)
    }

    # Add total if not hidden
    if total_visibility == 'show':
        response['total'] = total_count

    return jsonify(response)


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=['GET'])
def catch_all(path):
    """Handle all unknown routes."""
    return jsonify({'ok': False, 'error': 'Not found'}), 404


def main():
    parser = argparse.ArgumentParser(description='datagate - CSV conversion service')
    parser.add_argument('command', help='Command to run')
    parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        app.run(host=args.address, port=args.port, threaded=True)
    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
