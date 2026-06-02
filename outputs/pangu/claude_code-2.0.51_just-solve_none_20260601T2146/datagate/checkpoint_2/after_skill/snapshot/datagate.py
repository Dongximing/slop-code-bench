#!/usr/bin/env python3
"""Datagate - CSV data gateway service."""

import argparse
import csv
import hashlib
import io
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
        return bool(result.scheme in ('http', 'https') and result.netloc)
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
        return f if f == f and f != float('inf') and f != float('-inf') else s
    except ValueError:
        return s


def detect_charset(content, provided_charset=None):
    if provided_charset:
        try:
            content.decode(provided_charset)
            return provided_charset
        except (LookupError, UnicodeDecodeError):
            raise ValueError(f"Unsupported or malformed charset: {provided_charset}")
    result = chardet.detect(content)
    encoding = result.get('encoding', 'utf-8') or 'utf-8'
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
    query = request.query_string.decode('utf-8')

    def parse_param(name, validate, default=None):
        if f'{name}=' not in query:
            return default
        if query.count(f'{name}=') > 1:
            return {"err": f"Repeated control parameter: {name}"}
        val = request.args.get(name)
        if validate:
            result = validate(val)
            if isinstance(result, dict):
                return result
            return result
        return val

    def int_positive(val_str):
        if not val_str:
            return {"err": f"Invalid _size: {val_str}"}
        if not val_str.isdigit() or val_str.startswith('0'):
            return {"err": f"Invalid _size: {val_str}"}
        val = int(val_str)
        if val <= 0:
            return {"err": f"Invalid _size: {val_str}"}
        return val

    def int_nonnegative(val_str):
        if not val_str:
            return {"err": f"Invalid _offset: {val_str}"}
        if not val_str.isdigit():
            return {"err": f"Invalid _offset: {val_str}"}
        val = int(val_str)
        if val < 0:
            return {"err": f"Invalid _offset: {val_str}"}
        return val

    size_result = parse_param('_size', int_positive, 100)
    if isinstance(size_result, dict):
        return jsonify({"ok": False, "error": size_result["err"]}), 400
    size = size_result

    offset_result = parse_param('_offset', int_nonnegative, 0)
    if isinstance(offset_result, dict):
        return jsonify({"ok": False, "error": offset_result["err"]}), 400
    offset = offset_result

    sort_column = parse_param('_sort', lambda v: v if v not in (None, '') else None)
    sort_desc = parse_param('_sort_desc', lambda v: v if v not in (None, '') else None)
    if sort_desc is not None:
        sort_column = sort_desc
        sort_desc = True
    else:
        sort_desc = False

    if sort_column is not None:
        if sort_column == '':
            return jsonify({"ok": False, "error": "Empty sort column"}), 400
        if sort_column not in columns:
            return jsonify({"ok": False, "error": f"Unknown column: {sort_column}"}), 400

    shape_result = parse_param('_shape', lambda v: v if v in ('lists', 'objects') else {"err": f"Invalid _shape: {v}"}, 'lists')
    if isinstance(shape_result, dict):
        return jsonify({"ok": False, "error": shape_result["err"]}), 400
    shape = shape_result

    rowid_result = parse_param('_rowid', lambda v: v if v == 'hide' else {"err": f"Invalid _rowid value: {v}"})
    if isinstance(rowid_result, dict):
        return jsonify({"ok": False, "error": rowid_result["err"]}), 400
    show_rowid = rowid_result != 'hide'

    total_result = parse_param('_total', lambda v: v if v == 'hide' else {"err": f"Invalid _total value: {v}"})
    if isinstance(total_result, dict):
        return jsonify({"ok": False, "error": total_result["err"]}), 400
    show_total = total_result != 'hide'

    total = len(rows)
    rows_with_id = [(i + 1, row) for i, row in enumerate(rows)]

    if sort_column is not None:
        col_idx = columns.index(sort_column)
        rows_with_id = sorted(rows_with_id, key=lambda x: x[1][col_idx], reverse=sort_desc)

    paginated = rows_with_id[offset:offset + size]

    if shape == 'objects':
        result_rows = []
        for rowid, row in paginated:
            row_obj = {}
            if show_rowid:
                row_obj['rowid'] = rowid
            for i, col in enumerate(columns):
                row_obj[col] = row[i]
            result_rows.append(row_obj)
    else:
        result_rows = [row for _, row in paginated]

    response = {"ok": True, "columns": columns, "rows": result_rows}
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
