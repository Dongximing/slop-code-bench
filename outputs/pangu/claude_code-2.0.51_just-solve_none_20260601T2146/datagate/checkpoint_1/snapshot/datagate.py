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
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__)

# In-memory storage for datasets: {dataset_id: {columns, rows}}
datasets = {}

# Snifter for delimiter detection
DELIMITER_CHARS = [',', ';', '\t']


def is_valid_url(url):
    """Validate that a string is a proper URL."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except Exception:
        return False


def detect_delimiter(sample):
    """Detect CSV delimiter from sample content."""
    sniffer = csv.Sniffer()
    try:
        dialect = sniffer.sniff(sample, delimiters=DELIMITER_CHARS)
        return dialect.delimiter
    except Exception:
        # Try manual detection
        best_delim = ','
        best_count = 0
        for delim in DELIMITER_CHARS:
            count = sample.count(delim)
            if count > best_count:
                best_count = count
                best_delim = delim
        return best_delim


def infer_type(value):
    """Infer the type of a value: string, int, float. Time-like values remain text."""
    if not isinstance(value, str):
        return value

    s = value.strip()
    if not s:
        return s

    # Check for time-like values (e.g., 08:30, 9:15, 12:00)
    if re.match(r'^\s*\d{1,2}:\d{2}(:\d{2})?\s*$', s):
        return s

    # Try integer
    try:
        return int(s)
    except ValueError:
        pass

    # Try float/decimal
    try:
        # Check if it's a valid number
        f = float(s)
        # Check if it's finite (not inf or nan)
        if not (f != f or f == float('inf') or f == float('-inf')):
            return f
    except ValueError:
        pass

    return s


def detect_charset(content, provided_charset=None):
    """Detect charset if not provided, otherwise validate it."""
    if provided_charset:
        try:
            content.decode(provided_charset)
            return provided_charset
        except (LookupError, UnicodeDecodeError):
            raise ValueError(f"Unsupported or malformed charset: {provided_charset}")
    else:
        result = chardet.detect(content)
        encoding = result.get('encoding', 'utf-8')
        if encoding is None:
            encoding = 'utf-8'
        return encoding


def fetch_csv(source_url, charset=None):
    """Fetch remote CSV file and return decoded text content."""
    try:
        response = requests.get(source_url, timeout=30)
        if response.status_code != 200:
            raise requests.RequestException(f"HTTP {response.status_code}")
    except requests.RequestException as e:
        raise ValueError(f"Source unreachable or remote HTTP error: {e}")

    content = response.content

    try:
        encoding = detect_charset(content, charset)
        text = content.decode(encoding)
    except ValueError as e:
        raise ValueError(str(e))

    return text


def parse_csv(text):
    """Parse CSV text and return columns and rows with type inference."""
    if not text.strip():
        raise ValueError("Empty content")

    # Detect delimiter
    delimiter = detect_delimiter(text)

    # Parse CSV
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)

    if len(rows) < 2:
        raise ValueError("Non-tabular content or insufficient data (requires at least header and one data row)")

    columns = rows[0]
    data_rows = rows[1:]

    # Verify tabular structure - all rows should have same number of columns
    if not all(len(row) == len(columns) for row in data_rows):
        raise ValueError("Non-tabular content - inconsistent column counts")

    # Infer types for each cell
    typed_rows = []
    for row in data_rows:
        typed_row = [infer_type(cell) for cell in row]
        typed_rows.append(typed_row)

    return columns, typed_rows


def generate_dataset_id(source_url):
    """Generate deterministic ID from source URL."""
    return hashlib.md5(source_url.encode('utf-8')).hexdigest()


@app.after_request
def add_cors_headers(response):
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


@app.route('/convert', methods=['GET'])
def convert():
    """Ingest and convert remote CSV to internal dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    if not source:
        return jsonify({"ok": False, "error": "Missing source parameter"}), 400

    if not is_valid_url(source):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    try:
        text = fetch_csv(source, charset)
        columns, rows = parse_csv(text)
    except ValueError as e:
        error_msg = str(e)
        if 'charset' in error_msg.lower():
            return jsonify({"ok": False, "error": error_msg}), 400
        elif 'unreachable' in error_msg.lower() or 'http' in error_msg.lower():
            return jsonify({"ok": False, "error": error_msg}), 404
        elif 'non-tabular' in error_msg.lower() or 'inconsistent' in error_msg.lower():
            return jsonify({"ok": False, "error": error_msg}), 400
        else:
            return jsonify({"ok": False, "error": error_msg}), 400

    dataset_id = generate_dataset_id(source)

    datasets[dataset_id] = {
        'columns': columns,
        'rows': rows
    }

    return jsonify({
        "ok": True,
        "endpoint": f"/datasets/{dataset_id}"
    })


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    """Retrieve a stored dataset by ID."""
    start_time = time.time()

    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": f"Unknown dataset id: {dataset_id}"}), 404

    data = datasets[dataset_id]
    columns = data['columns']
    all_rows = data['rows']

    # Return at most 100 rows
    rows = all_rows[:100]

    query_ms = (time.time() - start_time) * 1000

    return jsonify({
        "ok": True,
        "columns": columns,
        "rows": rows,
        "query_ms": query_ms
    })


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>', methods=['GET'])
def catch_all(path):
    """Handle unknown routes with 404."""
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
