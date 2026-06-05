#!/usr/bin/env python3
"""
datagate - A CSV to JSON conversion and query service
"""

import argparse
import csv
import hashlib
import io
import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import chardet
import requests
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

# In-memory storage for datasets
datasets: Dict[str, Dict[str, Any]] = {}


def generate_dataset_id(source_url: str) -> str:
    """Generate a deterministic dataset ID from the source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def detect_encoding(content: bytes) -> str:
    """Detect character encoding from content bytes."""
    detected = chardet.detect(content)
    encoding = detected.get('encoding', 'utf-8')

    # Handle None or empty encoding
    if not encoding:
        encoding = 'utf-8'

    # Normalize encoding names
    encoding = encoding.lower().replace('-', '')

    # Map common variations
    encoding_map = {
        'ascii': 'utf-8',  # ASCII is subset of UTF-8
        'iso88591': 'iso-8859-1',
        'iso88592': 'iso-8859-2',
        'iso885915': 'iso-8859-15',
        'windows1252': 'cp1252',
        'windows1251': 'cp1251',
    }

    return encoding_map.get(encoding, encoding)


def decode_content(content: bytes, charset: Optional[str] = None) -> str:
    """Decode bytes to string using specified charset or auto-detection."""
    if charset:
        try:
            return content.decode(charset)
        except (UnicodeDecodeError, LookupError) as e:
            raise ValueError(f"Unsupported or malformed charset: {charset}")
    else:
        encoding = detect_encoding(content)
        try:
            return content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            # Fallback to utf-8 with error handling
            try:
                return content.decode('utf-8', errors='replace')
            except Exception:
                return content.decode('latin-1', errors='replace')


def infer_delimiter(content: str) -> str:
    """Infer the delimiter used in CSV content."""
    # Count potential delimiters in the first few lines
    lines = content.strip().split('\n')[:10]
    if not lines:
        return ','

    delimiters = [',', ';', '\t']
    counts = {d: 0 for d in delimiters}

    for line in lines:
        for d in delimiters:
            counts[d] += line.count(d)

    # Return the delimiter with highest count
    # Prefer comma in case of tie
    max_count = max(counts.values())
    if max_count == 0:
        return ','

    for d in delimiters:
        if counts[d] == max_count:
            return d

    return ','


def is_time_like(value: str) -> bool:
    """Check if a value looks like a time (e.g., 08:30, 9:15, 12:00)."""
    if not value:
        return False
    # Match patterns like 8:30, 08:30, 9:15, 12:00, 23:59
    pattern = r'^[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$'
    return bool(re.match(pattern, value.strip()))


def infer_type(value: str) -> Any:
    """Infer the type of a CSV value and convert it."""
    if not value:
        return ''

    # Check if it's a time-like value first
    if is_time_like(value):
        return value

    # Try integer
    try:
        # Check if it's a pure integer (no leading zeros except for 0 itself)
        if re.match(r'^-?[0-9]+$', value):
            int_val = int(value)
            return int_val
    except ValueError:
        pass

    # Try float/decimal
    try:
        # Check if it's a decimal number
        if re.match(r'^-?[0-9]+\.[0-9]+$', value):
            float_val = float(value)
            return float_val
    except ValueError:
        pass

    # Default to string
    return value


def parse_csv(content: str) -> Tuple[List[str], List[List[Any]]]:
    """Parse CSV content and return columns and rows."""
    delimiter = infer_delimiter(content)

    # Parse CSV
    reader = csv.reader(io.StringIO(content), delimiter=delimiter)

    rows = list(reader)

    # Validate: need at least header and one data row
    if len(rows) < 2:
        raise ValueError("Non-tabular content: requires at least one header row and one data row")

    # First row is header
    columns = rows[0]

    # Validate columns are not empty
    if not columns or all(c.strip() == '' for c in columns):
        raise ValueError("Non-tabular content: empty header row")

    # Process data rows
    data_rows = []
    for row in rows[1:]:
        # Convert each value
        converted_row = [infer_type(v) for v in row]
        data_rows.append(converted_row)

    return columns, data_rows


def fetch_csv(source_url: str, charset: Optional[str] = None) -> Tuple[str, str]:
    """Fetch CSV from source URL and return (content, encoding used)."""
    try:
        response = requests.get(source_url, timeout=30)
        response.raise_for_status()
    except requests.exceptions.MissingSchema:
        raise ValueError(f"Invalid URL: {source_url}")
    except requests.exceptions.InvalidURL:
        raise ValueError(f"Invalid URL: {source_url}")
    except requests.exceptions.ConnectionError:
        raise ConnectionError(f"Source unreachable: {source_url}")
    except requests.exceptions.Timeout:
        raise ConnectionError(f"Source unreachable: {source_url}")
    except requests.exceptions.HTTPError as e:
        raise ConnectionError(f"Remote HTTP error: {e.response.status_code}")
    except requests.exceptions.RequestException as e:
        raise ConnectionError(f"Source unreachable: {source_url}")

    content_bytes = response.content

    try:
        content = decode_content(content_bytes, charset)
    except ValueError:
        raise

    return content


@app.route('/convert', methods=['GET'])
def convert():
    """Convert a remote CSV file to a dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    if not source:
        return jsonify({"ok": False, "error": "Missing required parameter: source"}), 400

    # Validate URL format
    if not source.startswith(('http://', 'https://')):
        return jsonify({"ok": False, "error": f"Invalid URL: {source}"}), 400

    # Generate dataset ID
    dataset_id = generate_dataset_id(source)

    # Check if already processed
    if dataset_id in datasets:
        return jsonify({"ok": True, "endpoint": f"/datasets/{dataset_id}"})

    # Fetch and parse CSV
    try:
        content = fetch_csv(source, charset)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except ConnectionError as e:
        return jsonify({"ok": False, "error": str(e)}), 404

    try:
        columns, rows = parse_csv(content)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # Store dataset
    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows,
        "source": source
    }

    return jsonify({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id: str):
    """Query a stored dataset."""
    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": f"Dataset not found: {dataset_id}"}), 404

    dataset = datasets[dataset_id]

    start_time = time.time()

    # Return at most 100 rows
    rows = dataset["rows"][:100]

    query_ms = (time.time() - start_time) * 1000

    return jsonify({
        "ok": True,
        "columns": dataset["columns"],
        "rows": rows,
        "query_ms": round(query_ms, 1)
    })


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors."""
    return jsonify({"ok": False, "error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    """Handle 405 errors."""
    return jsonify({"ok": False, "error": "Method not allowed"}), 405


@app.after_request
def add_cors_headers(response: Response) -> Response:
    """Add CORS headers to all responses."""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


def main():
    """Main entry point for the datagate service."""
    parser = argparse.ArgumentParser(description='datagate - CSV to JSON conversion service')
    parser.add_argument('command', choices=['start'], help='Command to execute')
    parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        print(f"Starting datagate on {args.address}:{args.port}")
        app.run(host=args.address, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
