#!/usr/bin/env python3
"""
datagate - A CSV to JSON conversion and storage service.
"""

import argparse
import csv
import hashlib
import io
import json
import re
import time
from functools import wraps
from urllib.parse import urlparse

import chardet
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# In-memory storage for datasets
datasets = {}


def make_json_response(data, status=200):
    """Create a JSON response with CORS headers."""
    response = jsonify(data)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response, status


def error_response(message, status=400):
    """Create an error response."""
    return make_json_response({'ok': False, 'error': message}, status)


def success_response(data):
    """Create a success response."""
    data['ok'] = True
    return make_json_response(data, 200)


def generate_dataset_id(source_url):
    """Generate a deterministic dataset ID from the source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def is_valid_url(url):
    """Check if the URL is valid."""
    try:
        result = urlparse(url)
        return result.scheme in ('http', 'https') and result.netloc
    except Exception:
        return False


def detect_encoding(content_bytes):
    """Detect the encoding of the content."""
    result = chardet.detect(content_bytes)
    return result['encoding'] or 'utf-8'


def detect_delimiter(sample_text):
    """Detect the CSV delimiter from sample text."""
    # Count potential delimiters in the sample
    delimiters = [',', ';', '\t']
    counts = {}

    for delim in delimiters:
        # Count occurrences in each line
        lines = sample_text.split('\n')[:10]  # Check first 10 lines
        line_counts = [line.count(delim) for line in lines if line.strip()]
        if line_counts:
            # A good delimiter should have consistent counts across lines
            if len(set(line_counts)) == 1 and line_counts[0] > 0:
                counts[delim] = line_counts[0]
            elif line_counts[0] > 0:
                # Allow some variation but require majority
                max_count = max(line_counts)
                min_count = min(line_counts)
                if max_count > 0 and min_count > 0:
                    counts[delim] = sum(line_counts)

    if not counts:
        # Fallback: try comma as default
        return ','

    # Return the delimiter with highest count
    return max(counts, key=counts.get)


def is_time_like(value):
    """Check if a value looks like a time (e.g., '08:30', '9:15', '12:00')."""
    if not isinstance(value, str):
        return False
    # Match time patterns like HH:MM, H:MM, HH:MM:SS, H:MM:SS
    time_pattern = r'^\d{1,2}:\d{2}(:\d{2})?$'
    return bool(re.match(time_pattern, value.strip()))


def infer_type(value):
    """Infer the JSON type for a value."""
    if value == '' or value is None:
        return ''

    # Keep time-like values as strings
    if is_time_like(value):
        return value

    # Try integer
    try:
        int_val = int(value)
        # Check if it's truly an integer (not float notation)
        if '.' not in str(value) and 'e' not in str(value).lower():
            return int_val
    except (ValueError, TypeError):
        pass

    # Try float
    try:
        float_val = float(value)
        return float_val
    except (ValueError, TypeError):
        pass

    # Return as string
    return value


def parse_csv(content_bytes, charset=None):
    """Parse CSV content and return columns and rows."""
    # Decode content
    if charset:
        try:
            content = content_bytes.decode(charset)
        except (UnicodeDecodeError, LookupError, AttributeError) as e:
            raise ValueError(f"Invalid charset '{charset}': {str(e)}")
    else:
        encoding = detect_encoding(content_bytes)
        try:
            content = content_bytes.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # Fallback to utf-8 with error handling
            content = content_bytes.decode('utf-8', errors='replace')

    if not content.strip():
        raise ValueError("Empty content")

    # Detect delimiter
    sample = content[:min(len(content), 8192)]
    delimiter = detect_delimiter(sample)

    # Parse CSV
    try:
        reader = csv.reader(io.StringIO(content), delimiter=delimiter)
        rows = list(reader)
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {str(e)}")

    if len(rows) < 2:
        raise ValueError("CSV must have at least one header row and one data row")

    # Extract columns from first row
    columns = rows[0]

    if not columns or all(col.strip() == '' for col in columns):
        raise ValueError("CSV has no valid headers")

    # Process data rows
    data_rows = []
    for row in rows[1:]:
        # Infer types for each value
        typed_row = []
        for i, value in enumerate(row):
            if i < len(columns):
                typed_row.append(infer_type(value))
        # Ensure row has same number of columns as header
        while len(typed_row) < len(columns):
            typed_row.append('')
        data_rows.append(typed_row[:len(columns)])

    return columns, data_rows


@app.route('/convert', methods=['GET', 'OPTIONS'])
def convert():
    """Convert a remote CSV file to a dataset."""
    if request.method == 'OPTIONS':
        return make_json_response({'ok': True}, 200)

    source = request.args.get('source')
    charset = request.args.get('charset')

    # Validate source parameter
    if not source:
        return error_response("Missing 'source' parameter", 400)

    if not is_valid_url(source):
        return error_response("Invalid URL", 400)

    # Validate charset if provided
    if charset:
        try:
            # Test if charset is valid
            ''.encode(charset)
        except LookupError:
            return error_response(f"Unsupported charset: {charset}", 400)

    # Fetch the CSV
    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        return error_response(f"Source unreachable: {str(e)}", 404)

    # Check content type hints
    content_type = response.headers.get('Content-Type', '')
    if 'json' in content_type.lower() or 'xml' in content_type.lower() or 'html' in content_type.lower():
        return error_response("Non-tabular content", 400)

    # Parse the CSV
    try:
        columns, rows = parse_csv(response.content, charset)
    except ValueError as e:
        return error_response(str(e), 400)
    except Exception as e:
        return error_response(f"Failed to parse CSV: {str(e)}", 400)

    # Generate dataset ID
    dataset_id = generate_dataset_id(source)

    # Store the dataset
    datasets[dataset_id] = {
        'columns': columns,
        'rows': rows,
        'source': source
    }

    return success_response({'endpoint': f'/datasets/{dataset_id}'})


@app.route('/datasets/<dataset_id>', methods=['GET', 'OPTIONS'])
def get_dataset(dataset_id):
    """Get a stored dataset."""
    if request.method == 'OPTIONS':
        return make_json_response({'ok': True}, 200)

    start_time = time.time()

    if dataset_id not in datasets:
        return error_response("Dataset not found", 404)

    dataset = datasets[dataset_id]

    # Calculate query time
    query_ms = (time.time() - start_time) * 1000

    # Limit rows to 100
    rows = dataset['rows'][:100]

    return success_response({
        'columns': dataset['columns'],
        'rows': rows,
        'query_ms': round(query_ms, 1)
    })


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    return error_response("Not found", 404)


@app.errorhandler(500)
def internal_error(e):
    """Handle 500 errors."""
    return error_response("Internal server error", 500)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='datagate - CSV to JSON conversion service')
    subparsers = parser.add_subparsers(dest='command')

    start_parser = subparsers.add_parser('start', help='Start the server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        print(f"Starting datagate on {args.address}:{args.port}")
        app.run(host=args.address, port=args.port)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
