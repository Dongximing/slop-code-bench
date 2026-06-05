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

# In-memory storage for datasets
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
        except (UnicodeDecodeError, LookupError) as e:
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
            else:
                in_quotes = not in_quotes
                i += 1
                continue
        elif char == delimiter and not in_quotes:
            result.append(current)
            current = ''
            i += 1
            continue
        else:
            current += char

        i += 1

    result.append(current)
    return result


def is_valid_url(url):
    """Check if a URL is valid."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except:
        return False


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
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


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    """Query a stored dataset."""
    start_time = time.time()

    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": "Dataset not found"}), 404

    dataset = datasets[dataset_id]
    rows = dataset["rows"][:100]
    query_ms = (time.time() - start_time) * 1000

    return jsonify({
        "ok": True,
        "columns": dataset["columns"],
        "rows": rows,
        "query_ms": round(query_ms, 1)
    })


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
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
