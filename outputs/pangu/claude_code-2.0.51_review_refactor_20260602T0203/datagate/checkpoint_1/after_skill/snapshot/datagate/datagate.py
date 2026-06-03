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
        # Check for exact integer match
        if re.match(r'^-?\d+$', value):
            return int(value)
    except ValueError:
        pass

    # Try float
    try:
        # Check for decimal/float match
        if re.match(r'^-?\d+(\.\d+)?$', value):
            return float(value)
    except ValueError:
        pass

    # Time-like values (HH:MM, H:MM, HH:MM:SS, etc.) remain text
    if re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', value):
        return value

    # String
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
    """
    if dataset_id not in datasets:
        return jsonify({'ok': False, 'error': 'Unknown dataset id'}), 404

    columns, rows = datasets[dataset_id]

    # Limit to at most 100 rows
    limited_rows = rows[:100]

    return jsonify({
        'ok': True,
        'columns': columns,
        'rows': limited_rows,
        'query_ms': 0.0
    })


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
