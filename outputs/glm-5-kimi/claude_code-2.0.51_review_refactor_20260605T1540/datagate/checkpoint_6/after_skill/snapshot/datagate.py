#!/usr/bin/env python3
"""
datagate - A CSV data ingestion and query service.
"""

import csv
import hashlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import chardet
import openpyxl
import requests
import xlrd
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

datasets = {}

TRUE_VALUES = {'1', 'true', 'yes', 'on'}
FALSE_VALUES = {'0', 'false', 'no', 'off'}


class Config:
    """Configuration management with precedence: defaults < file < env."""

    def __init__(self):
        self.max_source_size = None
        self.origin_allowlist = None
        self.require_tls = False
        self.storage_dir = Path('/tmp/datagate')
        self.cache_enabled = True

    def load(self):
        """Load configuration from file and environment variables."""
        # Load from file if DATAGATE_CONFIG is set
        config_file = os.environ.get('DATAGATE_CONFIG')
        if config_file:
            self._load_file(config_file)

        # Override with direct environment variables
        self._load_env()

        # Create storage directory if needed
        if self.storage_dir:
            self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _load_file(self, filepath):
        """Load configuration from KEY=VALUE file."""
        try:
            with open(filepath, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    # Skip blank lines and comments
                    if not line or line.startswith('#'):
                        continue

                    if '=' not in line:
                        raise ValueError(f"Invalid config line {line_num}: missing '='")

                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()

                    self._set_config(key, value, f"file {filepath} line {line_num}")
        except FileNotFoundError:
            raise ValueError(f"Config file not found: {filepath}")
        except PermissionError:
            raise ValueError(f"Cannot read config file: {filepath}")

    def _load_env(self):
        """Load configuration from environment variables."""
        if 'MAX_SOURCE_SIZE' in os.environ:
            val = os.environ['MAX_SOURCE_SIZE']
            try:
                self.max_source_size = int(val)
                if self.max_source_size < 0:
                    raise ValueError("MAX_SOURCE_SIZE must be non-negative")
            except ValueError:
                raise ValueError(f"Invalid MAX_SOURCE_SIZE value: '{val}'")

        if 'ORIGIN_ALLOWLIST' in os.environ:
            val = os.environ['ORIGIN_ALLOWLIST']
            if val:
                self.origin_allowlist = [s.strip().lower() for s in val.split(',') if s.strip()]
            else:
                self.origin_allowlist = None

        if 'REQUIRE_TLS' in os.environ:
            val = os.environ['REQUIRE_TLS']
            self.require_tls = self._parse_bool(val, 'REQUIRE_TLS')

        if 'STORAGE_DIR' in os.environ:
            self.storage_dir = Path(os.environ['STORAGE_DIR'])

        if 'CACHE_ENABLED' in os.environ:
            val = os.environ['CACHE_ENABLED']
            self.cache_enabled = self._parse_bool(val, 'CACHE_ENABLED')

    def _set_config(self, key, value, source):
        """Set a configuration value from file."""
        if key == 'MAX_SOURCE_SIZE':
            try:
                self.max_source_size = int(value)
                if self.max_source_size < 0:
                    raise ValueError("must be non-negative")
            except ValueError as e:
                raise ValueError(f"Invalid {key} in {source}: {value} ({e})")

        elif key == 'ORIGIN_ALLOWLIST':
            if value:
                self.origin_allowlist = [s.strip().lower() for s in value.split(',') if s.strip()]
            else:
                self.origin_allowlist = None

        elif key == 'REQUIRE_TLS':
            self.require_tls = self._parse_bool(value, key, source)

        elif key == 'STORAGE_DIR':
            self.storage_dir = Path(value)

        elif key == 'CACHE_ENABLED':
            self.cache_enabled = self._parse_bool(value, key, source)

    def _parse_bool(self, value, name, source=None):
        """Parse a boolean value."""
        lower = value.lower().strip()
        if lower in TRUE_VALUES:
            return True
        if lower in FALSE_VALUES:
            return False
        if source:
            raise ValueError(f"Invalid {name} in {source}: '{value}'")
        else:
            raise ValueError(f"Invalid {name} value: '{value}'")


# Initialize configuration
config = Config()


def init_config():
    """Initialize configuration at startup."""
    try:
        config.load()
        # Load persisted datasets
        load_persisted_datasets()
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)


def load_persisted_datasets():
    """Load datasets from storage directory."""
    if not config.storage_dir:
        return

    datasets_file = config.storage_dir / 'datasets.json'
    if datasets_file.exists():
        try:
            with open(datasets_file, 'r') as f:
                data = json.load(f)
                datasets.update(data)
        except (json.JSONDecodeError, IOError):
            pass


def save_datasets():
    """Persist datasets to storage directory."""
    if not config.storage_dir:
        return

    datasets_file = config.storage_dir / 'datasets.json'
    try:
        with open(datasets_file, 'w') as f:
            json.dump(datasets, f)
    except IOError:
        pass


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
        except (UnicodeDecodeError, LookupError):
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
            in_quotes = not in_quotes
            i += 1
            continue

        if char == delimiter and not in_quotes:
            result.append(current)
            current = ''
            i += 1
            continue

        current += char
        i += 1

    result.append(current)
    return result


def is_valid_url(url):
    """Check if a URL is valid."""
    try:
        result = urlparse(url)
        return all([result.scheme in ('http', 'https'), result.netloc])
    except (ValueError, TypeError, UnicodeDecodeError):
        return False


def parse_xls(content_bytes):
    """Parse XLS file content and return columns and rows."""
    try:
        workbook = xlrd.open_workbook(file_contents=content_bytes)
        sheet = workbook.sheet_by_index(0)
    except Exception:
        raise ValueError("Failed to parse XLS file")

    if sheet.nrows < 2:
        raise ValueError("Spreadsheet must have at least one header row and one data row")

    columns = [str(sheet.cell_value(0, col)).strip() for col in range(sheet.ncols)]

    if len(columns) == 0:
        raise ValueError("Spreadsheet must have at least one column")

    rows = []
    for row_idx in range(1, sheet.nrows):
        row = []
        for col_idx in range(len(columns)):
            cell_value = sheet.cell_value(row_idx, col_idx)
            row.append(infer_type(cell_value))
        rows.append(row)

    if len(rows) == 0:
        raise ValueError("Spreadsheet must have at least one data row")

    return columns, rows


def parse_xlsx(content_bytes):
    """Parse XLSX file content and return columns and rows."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content_bytes), data_only=True)
        sheet = workbook.worksheets[0]
    except Exception:
        raise ValueError("Failed to parse XLSX file")

    rows_data = list(sheet.iter_rows(values_only=True))

    if len(rows_data) < 2:
        raise ValueError("Spreadsheet must have at least one header row and one data row")

    columns = [str(col).strip() if col is not None else '' for col in rows_data[0]]

    if len(columns) == 0:
        raise ValueError("Spreadsheet must have at least one column")

    rows = []
    for row_data in rows_data[1:]:
        row = []
        for col_idx in range(len(columns)):
            if col_idx < len(row_data):
                cell_value = row_data[col_idx]
                row.append(infer_type(cell_value))
            else:
                row.append('')
        rows.append(row)

    if len(rows) == 0:
        raise ValueError("Spreadsheet must have at least one data row")

    return columns, rows


def detect_format(content_bytes, filename=None):
    """Detect file format from content or filename."""
    if len(content_bytes) >= 4:
        if content_bytes[:4] == b'PK\x03\x04':
            return 'xlsx'
        if content_bytes[:8] == b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1':
            return 'xls'

    if filename:
        filename_lower = filename.lower()
        if filename_lower.endswith('.xlsx'):
            return 'xlsx'
        if filename_lower.endswith('.xls'):
            return 'xls'
        if filename_lower.endswith('.csv'):
            return 'csv'

    return 'csv'


def parse_content(content_bytes, charset=None, filename=None):
    """Parse content bytes and return columns and rows based on detected format."""
    file_format = detect_format(content_bytes, filename)

    if file_format == 'xls':
        return parse_xls(content_bytes)
    elif file_format == 'xlsx':
        return parse_xlsx(content_bytes)
    else:
        return parse_csv(content_bytes, charset)


def check_origin_allowlist():
    """
    Check if request origin is allowed.
    Returns (allowed, error_response) - one will be None.
    """
    if not config.origin_allowlist:
        return True, None

    referer = request.headers.get('Referer')

    if not referer:
        return False, (jsonify({"ok": False, "error": "Missing Referer header"}), 403)

    try:
        parsed = urlparse(referer)
        hostname = parsed.netloc.lower()
        # Remove port if present
        if ':' in hostname:
            hostname = hostname.split(':')[0]
    except Exception:
        return False, (jsonify({"ok": False, "error": "Invalid Referer header"}), 403)

    # Check if hostname matches any allowed suffix with domain-boundary rules
    for suffix in config.origin_allowlist:
        if hostname == suffix:
            return True, None
        if hostname.endswith('.' + suffix):
            return True, None

    return False, (jsonify({"ok": False, "error": "Origin not allowed"}), 403)


def check_max_size(content_length):
    """
    Check if content size is within limit.
    Returns (allowed, error_response) - one will be None.
    """
    if config.max_source_size is None:
        return True, None

    if content_length > config.max_source_size:
        return False, (jsonify({"ok": False, "error": "File size exceeds maximum allowed"}), 400)

    return True, None


def get_endpoint_url(path):
    """
    Generate endpoint URL based on REQUIRE_TLS setting.
    """
    if config.require_tls:
        host = request.host
        return f"https://{host}{path}"
    else:
        return path


@app.before_request
def before_request():
    """Check origin allowlist before routing."""
    if config.origin_allowlist:
        allowed, error = check_origin_allowlist()
        if not allowed:
            return error


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


@app.route('/convert', methods=['GET'])
def convert():
    """Convert a remote file to a dataset."""
    source = request.args.get('source')
    charset = request.args.get('charset')

    force_values = request.args.getlist('force')
    if len(force_values) > 1:
        return jsonify({"ok": False, "error": "Duplicate 'force' parameter"}), 400
    force = len(force_values) == 1

    if not source:
        return jsonify({"ok": False, "error": "Missing 'source' parameter"}), 400

    if not is_valid_url(source):
        return jsonify({"ok": False, "error": "Invalid URL"}), 400

    dataset_id = generate_id(source)

    if config.cache_enabled and dataset_id in datasets and not force:
        return jsonify({"ok": True, "endpoint": get_endpoint_url(f"/datasets/{dataset_id}")})

    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return jsonify({"ok": False, "error": "Source unreachable or remote HTTP error"}), 404

    content_bytes = response.content

    # Check max size
    allowed, error = check_max_size(len(content_bytes))
    if not allowed:
        return error

    try:
        columns, rows = parse_content(content_bytes, charset, source)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    save_datasets()

    return jsonify({"ok": True, "endpoint": get_endpoint_url(f"/datasets/{dataset_id}")})


@app.route('/upload', methods=['POST'])
def upload():
    """Upload a file and create a dataset."""
    if not request.content_type or 'multipart/form-data' not in request.content_type.lower():
        return jsonify({"ok": False, "error": "Request must be multipart/form-data"}), 415

    file = None
    if 'file' in request.files:
        file = request.files['file']
    elif 'attachment' in request.files:
        file = request.files['attachment']

    if not file:
        return jsonify({"ok": False, "error": "Missing file field"}), 400

    content_bytes = file.read()

    if len(content_bytes) == 0:
        return jsonify({"ok": False, "error": "Empty file"}), 400

    # Check max size
    allowed, error = check_max_size(len(content_bytes))
    if not allowed:
        return error

    dataset_id = hashlib.sha256(content_bytes).hexdigest()[:16]

    if dataset_id in datasets:
        return jsonify({"ok": True, "endpoint": get_endpoint_url(f"/datasets/{dataset_id}")})

    charset = request.form.get('charset')
    filename = file.filename

    try:
        columns, rows = parse_content(content_bytes, charset, filename)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    save_datasets()

    return jsonify({"ok": True, "endpoint": get_endpoint_url(f"/datasets/{dataset_id}")})


def make_error(message):
    """Create a standardized error response."""
    return jsonify({"ok": False, "error": message}), 400


def parse_positive_int(value, param_name, allow_zero=False):
    """Parse a positive integer parameter, return (value, error) tuple."""
    error_msg = f"{param_name} must be a non-negative integer" if allow_zero else f"{param_name} must be a positive integer"

    try:
        int_val = int(value)
        if (allow_zero and int_val < 0) or (not allow_zero and int_val <= 0):
            return None, error_msg
        return int_val, None
    except (ValueError, TypeError):
        return None, error_msg


def validate_control_params(request_args):
    """
    Validate control parameters and return parsed values or error.
    Returns (params_dict, error_response) - one will be None.
    """
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    for param in control_params:
        values = request_args.getlist(param)
        if len(values) > 1:
            return None, make_error(f"Duplicate control parameter: {param}")

    size = 100
    if '_size' in request_args:
        size_str = request_args.get('_size')
        size, err = parse_positive_int(size_str, "_size", allow_zero=False)
        if err:
            return None, make_error(err)

    offset = 0
    if '_offset' in request_args:
        offset_str = request_args.get('_offset')
        offset, err = parse_positive_int(offset_str, "_offset", allow_zero=True)
        if err:
            return None, make_error(err)

    shape = 'lists'
    if '_shape' in request_args:
        shape = request_args.get('_shape')
        if shape not in ('lists', 'objects'):
            return None, make_error("_shape must be 'lists' or 'objects'")

    sort_column = None
    sort_desc = False
    if '_sort_desc' in request_args:
        sort_column = request_args.get('_sort_desc')
        sort_desc = True
    if '_sort' in request_args and not sort_desc:
        sort_column = request_args.get('_sort')

    if sort_column is not None and sort_column == '':
        return None, make_error("_sort cannot be empty")

    if '_sort_desc' in request_args:
        sort_desc_val = request_args.get('_sort_desc')
        if sort_desc_val == '':
            return None, make_error("_sort_desc cannot be empty")

    rowid_hide = False
    if '_rowid' in request_args:
        rowid_val = request_args.get('_rowid')
        if rowid_val != 'hide':
            return None, make_error("_rowid must be 'hide'")
        rowid_hide = True

    total_hide = False
    if '_total' in request_args:
        total_val = request_args.get('_total')
        if total_val != 'hide':
            return None, make_error("_total must be 'hide'")
        total_hide = True

    return {
        'size': size,
        'offset': offset,
        'shape': shape,
        'sort_column': sort_column,
        'sort_desc': sort_desc,
        'rowid_hide': rowid_hide,
        'total_hide': total_hide
    }, None


@app.route('/datasets/<dataset_id>', methods=['GET'])
def get_dataset(dataset_id):
    """Query a stored dataset."""
    start_time = time.time()

    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": "Dataset not found"}), 404

    dataset = datasets[dataset_id]
    columns = dataset["columns"]
    all_rows = dataset["rows"]

    params, error = validate_control_params(request.args)
    if error:
        return error

    if params['sort_column'] is not None and params['sort_column'] not in columns:
        return make_error(f"Unknown column: {params['sort_column']}")

    rows = sort_rows(all_rows[:], columns, params['sort_column'], params['sort_desc'])

    total = len(rows)

    offset = params['offset']
    size = params['size']
    rows = rows[offset:offset + size]

    query_ms = (time.time() - start_time) * 1000

    if params['shape'] == 'objects':
        result_rows = []
        for idx, row in enumerate(rows):
            row_dict = {"rowid": offset + idx + 1}
            for col_idx, col in enumerate(columns):
                row_dict[col] = row[col_idx]
            if params['rowid_hide']:
                del row_dict["rowid"]
            result_rows.append(row_dict)
    else:
        result_rows = rows

    response = {
        "ok": True,
        "columns": columns,
        "rows": result_rows,
        "query_ms": round(query_ms, 1)
    }

    if not params['total_hide']:
        response["total"] = total

    return jsonify(response)


def generate_csv_output(columns, rows):
    """Generate CSV output from columns and rows."""
    output = io.StringIO()
    writer = csv.writer(output, lineterminator='\n')
    writer.writerow(columns)
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


@app.route('/datasets/<dataset_id>/export', methods=['GET'])
def export_dataset(dataset_id):
    """Export a dataset as CSV."""
    if dataset_id not in datasets:
        return jsonify({"ok": False, "error": "Dataset not found"}), 404

    dataset = datasets[dataset_id]
    columns = dataset["columns"]
    all_rows = dataset["rows"]

    params, error = validate_control_params(request.args)
    if error:
        return error

    if params['sort_column'] is not None and params['sort_column'] not in columns:
        return make_error(f"Unknown column: {params['sort_column']}")

    rows = sort_rows(all_rows[:], columns, params['sort_column'], params['sort_desc'])

    offset = params['offset']
    size = params['size']
    rows = rows[offset:offset + size]

    csv_content = generate_csv_output(columns, rows)

    response = Response(
        csv_content,
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename="{dataset_id}.csv"'
        }
    )

    return response


def sort_rows(rows, columns, sort_column, sort_desc=False):
    """Sort rows by a column (stable)."""
    if sort_column is None:
        return rows
    col_index = columns.index(sort_column)
    indexed_rows = list(enumerate(rows))
    if sort_desc:
        indexed_rows.sort(key=lambda x: (x[1][col_index] is None, x[1][col_index] if x[1][col_index] is not None else ''), reverse=True)
    else:
        indexed_rows.sort(key=lambda x: (x[1][col_index] is None, x[1][col_index] if x[1][col_index] is not None else ''))
    return [r[1] for r in indexed_rows]


@app.errorhandler(404)
def not_found(error):
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
        init_config()
        start_server(port=args.port, address=args.address)
    else:
        parser.print_help()
