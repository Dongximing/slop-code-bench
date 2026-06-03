#!/usr/bin/env python3
"""datagate - CSV ingestion and query service."""

import argparse
import csv
import hashlib
import json
import re
import time
import urllib.request
import urllib.error
from io import StringIO
from urllib.parse import urlparse

import chardet
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

datasets = {}


def generate_dataset_id(source_url):
    """Generate a deterministic dataset ID from source URL using SHA-256."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def detect_delimiter(sample):
    """Detect CSV delimiter from sample text."""
    delimiters = [',', ';', '\t']
    counts = {}

    for delim in delimiters:
        lines = sample.split('\n')[:5]
        if not lines:
            continue

        line_counts = [line.count(delim) for line in lines if line.strip()]
        if not line_counts:
            continue

        if line_counts[0] > 0 and all(c == line_counts[0] for c in line_counts):
            counts[delim] = line_counts[0]

    if counts:
        return max(counts, key=counts.get)

    return ','


def is_time_like(value):
    """Check if a value looks like a time (e.g., '08:30', '9:15', '12:00')."""
    if not isinstance(value, str):
        return False
    return bool(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', value.strip()))


def infer_type(value):
    """Infer the type of a value and convert it appropriately."""
    if value == '':
        return ''

    if is_time_like(value):
        return value

    try:
        return int(value)
    except ValueError:
        pass

    try:
        return float(value)
    except ValueError:
        pass

    return value


def convert_rows(rows):
    """Convert row values with type inference."""
    converted = []
    for row in rows:
        converted_row = [infer_type(val) for val in row]
        converted.append(converted_row)
    return converted


def parse_csv(content_bytes, charset=None):
    """Parse CSV content and return columns and rows."""
    if charset:
        try:
            content = content_bytes.decode(charset)
        except (LookupError, UnicodeDecodeError):
            raise ValueError(f"Unsupported or malformed charset: {charset}")
    else:
        detected = chardet.detect(content_bytes)
        encoding = detected.get('encoding', 'utf-8') or 'utf-8'
        try:
            content = content_bytes.decode(encoding)
        except UnicodeDecodeError:
            content = content_bytes.decode('utf-8', errors='replace')

    delimiter = detect_delimiter(content)

    try:
        reader = csv.reader(StringIO(content), delimiter=delimiter)
        rows = list(reader)
    except Exception as e:
        raise ValueError(f"Failed to parse CSV: {str(e)}")

    if len(rows) < 2:
        raise ValueError("Non-tabular content: requires at least one header row and one data row")

    if not rows[0]:
        raise ValueError("Non-tabular content: empty header row")

    header = rows[0]
    num_cols = len(header)

    valid_rows = []
    for row in rows[1:]:
        if len(row) == num_cols:
            valid_rows.append(row)
        elif len(row) < num_cols:
            valid_rows.append(row + [''] * (num_cols - len(row)))
        else:
            valid_rows.append(row[:num_cols])

    return header, convert_rows(valid_rows)


def is_valid_url(url):
    """Check if URL is valid."""
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc]) and result.scheme in ('http', 'https')
    except Exception:
        return False


def fetch_url(url):
    """Fetch content from URL."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"HTTP error {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise ConnectionError(f"URL error: {e.reason}")
    except Exception as e:
        raise ConnectionError(f"Failed to fetch URL: {str(e)}")


def json_response(data, status=200):
    """Create a JSON response with CORS headers."""
    response = make_response(jsonify(data), status)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


def error_response(message, status=400):
    """Create an error response."""
    return json_response({"ok": False, "error": message}, status)


@app.route('/convert', methods=['GET', 'OPTIONS'])
def convert():
    """Handle CSV conversion endpoint."""
    if request.method == 'OPTIONS':
        return json_response({"ok": True})

    source = request.args.get('source')
    charset = request.args.get('charset')

    if not source:
        return error_response("Missing required parameter: source", 400)

    if not is_valid_url(source):
        return error_response("Invalid URL", 400)

    dataset_id = generate_dataset_id(source)

    if dataset_id in datasets:
        return json_response({"ok": True, "endpoint": f"/datasets/{dataset_id}"})

    try:
        content_bytes = fetch_url(source)
    except ConnectionError as e:
        return error_response(str(e), 404)

    try:
        columns, rows = parse_csv(content_bytes, charset)
    except ValueError as e:
        return error_response(str(e), 400)

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    return json_response({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


@app.route('/datasets/<dataset_id>', methods=['GET', 'OPTIONS'])
def get_dataset(dataset_id):
    """Handle dataset query endpoint."""
    if request.method == 'OPTIONS':
        return json_response({"ok": True})

    if dataset_id not in datasets:
        return error_response("Dataset not found", 404)

    dataset = datasets[dataset_id]
    rows = dataset["rows"][:100]

    start_time = time.perf_counter()
    query_ms = (time.perf_counter() - start_time) * 1000

    return json_response({
        "ok": True,
        "columns": dataset["columns"],
        "rows": rows,
        "query_ms": round(query_ms, 1)
    })


@app.route('/', methods=['GET', 'OPTIONS'])
def index():
    """Handle root endpoint."""
    return json_response({"ok": True, "service": "datagate"})


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return error_response("Not found", 404)


def main():
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(description='datagate - CSV ingestion and query service')
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    start_parser = subparsers.add_parser('start', help='Start the server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        print(f"Starting datagate server on {args.address}:{args.port}")
        app.run(host=args.address, port=args.port)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
