#!/usr/bin/env python3
"""
datagate: A service for converting remote CSV files to JSON datasets.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from io import BytesIO, StringIO
from urllib.parse import urlparse

import chardet
import openpyxl
import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)


# Configuration with precedence:
# 1. Built-in defaults
# 2. DATAGATE_CONFIG file
# 3. Direct environment variables


def parse_boolean(value, setting_name):
    """Parse a boolean value strictly."""
    if value is None:
        return None
    s = str(value).lower().strip()
    true_values = {'1', 'true', 'yes', 'on'}
    false_values = {'0', 'false', 'no', 'off'}
    if s in true_values:
        return True
    elif s in false_values:
        return False
    else:
        print(f"Error: Invalid {setting_name} value: {value}")
        print(f"Valid values: true (1, true, yes, on) or false (0, false, no, off)")
        sys.exit(1)


def parse_config_file(filepath):
    """Parse a config file with KEY=VALUE format."""
    config = {}
    try:
        with open(filepath, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                # Skip blank lines and comments
                if not line or line.startswith('#'):
                    continue
                # Parse KEY=VALUE
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    config[key] = value
                else:
                    print(f"Error: Invalid config line {line_num}: {line}")
                    sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Config file not found: {filepath}")
        sys.exit(1)
    except Exception as e:
        print(f"Error reading config file: {e}")
        sys.exit(1)
    return config


def load_config():
    """Load configuration with precedence rules."""
    # Built-in defaults
    config = {
        'MAX_SOURCE_SIZE': None,  # unset means no max
        'ORIGIN_ALLOWLIST': None,
        'REQUIRE_TLS': False,
        'STORAGE_DIR': None,  # implementation-defined
        'CACHE_ENABLED': True,
    }

    # 2. DATAGATE_CONFIG file
    datagate_config_file = os.getenv('DATAGATE_CONFIG')
    if datagate_config_file:
        file_config = parse_config_file(datagate_config_file)
        config.update(file_config)

    # 3. Direct environment variables (override everything)
    env_vars = {
        'MAX_SOURCE_SIZE': os.getenv('MAX_SOURCE_SIZE'),
        'ORIGIN_ALLOWLIST': os.getenv('ORIGIN_ALLOWLIST'),
        'REQUIRE_TLS': os.getenv('REQUIRE_TLS'),
        'STORAGE_DIR': os.getenv('STORAGE_DIR'),
        'CACHE_ENABLED': os.getenv('CACHE_ENABLED'),
    }

    for key, value in env_vars.items():
        if value is not None:
            if key == 'CACHE_ENABLED':
                config[key] = parse_boolean(value, key)
            elif key in ('MAX_SOURCE_SIZE',):
                try:
                    config[key] = int(value)
                except ValueError:
                    print(f"Error: Invalid {key} value: {value}")
                    print(f"{key} must be an integer")
                    sys.exit(1)
            elif key == 'ORIGIN_ALLOWLIST':
                if value:
                    # Parse comma-separated list
                    config[key] = [suffix.strip() for suffix in value.split(',') if suffix.strip()]
                else:
                    config[key] = None
            elif key == 'REQUIRE_TLS':
                config[key] = parse_boolean(value, key)
            else:
                config[key] = value

    # Determine storage directory
    if config['STORAGE_DIR'] is None:
        # Implementation-defined: use a default location
        config['STORAGE_DIR'] = os.path.expanduser('~/.datagate/storage')

    return config


# Load configuration
CONFIG = load_config()

# Extract config values for easy access
MAX_SOURCE_SIZE = CONFIG['MAX_SOURCE_SIZE']
ORIGIN_ALLOWLIST = CONFIG['ORIGIN_ALLOWLIST']
REQUIRE_TLS = CONFIG['REQUIRE_TLS']
STORAGE_DIR = CONFIG['STORAGE_DIR']
CACHE_ENABLED = CONFIG['CACHE_ENABLED']

# Ensure storage directory exists
os.makedirs(STORAGE_DIR, exist_ok=True)


def save_dataset_to_storage(dataset_id, columns, rows):
    """Save a dataset to persistent storage."""
    path = get_dataset_path(dataset_id)
    data = {
        'columns': columns,
        'rows': rows,
    }
    try:
        with open(path, 'w') as f:
            json.dump(data, f)
    except Exception:
        # If we can't save, ignore but log to stderr
        print(f"Warning: Failed to save dataset {dataset_id} to storage", file=sys.stderr)


def load_dataset_from_storage(dataset_id):
    """Load a dataset from persistent storage."""
    path = get_dataset_path(dataset_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r') as f:
            data = json.load(f)
        columns = data.get('columns', [])
        rows = data.get('rows', [])
        # Convert rows back to proper types (int/float from strings)
        typed_rows = []
        for row in rows:
            typed_row = []
            for cell in row:
                typed_row.append(detect_type(cell))
            typed_rows.append(typed_row)
        return columns, typed_rows
    except Exception:
        return None


def load_all_datasets_from_storage():
    """Load all datasets from persistent storage on startup."""
    loaded_datasets = {}
    try:
        for filename in os.listdir(STORAGE_DIR):
            if filename.endswith('.json'):
                dataset_id = filename[:-5]  # Remove .json extension
                dataset = load_dataset_from_storage(dataset_id)
                if dataset is not None:
                    columns, rows = dataset
                    loaded_datasets[dataset_id] = (columns, rows)
    except Exception as e:
        print(f"Warning: Failed to load datasets from storage: {e}", file=sys.stderr)
    return loaded_datasets


# Load existing datasets from storage on startup
datasets = load_all_datasets_from_storage()


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


def detect_file_format(filename_or_url, content=None):
    """Detect file format from filename/URL extension or content."""
    filename = filename_or_url.lower()
    if filename.endswith('.csv'):
        return 'csv'
    elif filename.endswith('.xls'):
        return 'xls'
    elif filename.endswith('.xlsx'):
        return 'xlsx'
    elif content:
        # Check for Excel file signatures
        if content.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'):  # XLS/OLE2 signature
            return 'xls'
        elif content.startswith(b'PK\x03\x04') and filename.endswith('.xlsx'):
            return 'xlsx'
        else:
            # Try to detect as CSV by checking if it's text
            try:
                sample = content[:1024]
                detected = chardet.detect(sample)
                if detected.get('encoding'):
                    sample_decoded = sample.decode(detected.get('encoding'), errors='ignore')
                    # Basic check for CSV-like content
                    if ',' in sample_decoded or ';' in sample_decoded or '\t' in sample_decoded:
                        return 'csv'
            except Exception:
                pass
    return None


def parse_spreadsheet(file_like, format_type):
    """
    Parse spreadsheet file (xls or xlsx) and return (columns, rows).
    Only processes the first worksheet.
    """
    try:
        workbook = openpyxl.load_workbook(file_like, read_only=True, data_only=True)
    except Exception:
        # Try with xlrd for .xls if openpyxl fails
        try:
            import xlrd
            workbook = xlrd.open_workbook(file_like)
            sheet = workbook.sheet_by_index(0)
            if sheet.nrows < 2:
                raise ValueError("Spreadsheet must have at least one header row and one data row")

            columns = [str(sheet.cell_value(0, col)) for col in range(sheet.ncols)]
            rows = []
            for row_idx in range(1, sheet.nrows):
                row = []
                for col in range(sheet.ncols):
                    val = sheet.cell_value(row_idx, col)
                    # Determine type and convert
                    if sheet.cell_type(row_idx, col) == xlrd.XL_CELL_NUMBER:
                        # Check if it's an integer
                        if val == int(val):
                            val = int(val)
                        else:
                            val = float(val)
                    elif sheet.cell_type(row_idx, col) == xlrd.XL_CELL_BOOLEAN:
                        val = bool(val)
                    elif sheet.cell_type(row_idx, col) == xlrd.XL_CELL_EMPTY:
                        val = ''
                    else:
                        val = str(val)
                    row.append(val)
                rows.append(row)
            return columns, rows
        except ImportError:
            return None, None
        except Exception:
            return None, None

    sheet = workbook.active
    if sheet is None:
        raise ValueError("Spreadsheet must have at least one worksheet")

    # Get all rows from the sheet
    all_rows = list(sheet.iter_rows(values_only=True))

    if len(all_rows) < 2:
        raise ValueError("Spreadsheet must have at least one header row and one data row")

    columns = [str(cell) if cell is not None else '' for cell in all_rows[0]]

    rows = []
    for row in all_rows[1:]:
        typed_row = []
        for cell in row:
            if cell is None:
                typed_row.append('')
            elif isinstance(cell, (int, float)):
                # Check for time-like values in float form
                typed_row.append(detect_type(str(cell)))
            else:
                typed_row.append(detect_type(str(cell)))
        rows.append(typed_row)

    return columns, rows


def parse_file_content(content, filename, charset=None):
    """
    Parse file content based on its format and return (columns, rows).
    """
    fmt = detect_file_format(filename, content)

    if fmt == 'csv':
        # Use provided charset or detect
        if charset:
            try:
                decoded = content.decode(charset)
            except (LookupError, UnicodeDecodeError):
                raise ValueError("Unsupported or malformed charset")
        else:
            detected = chardet.detect(content)
            encoding = detected.get('encoding', 'utf-8')
            try:
                decoded = content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                decoded = content.decode('utf-8', errors='ignore')

        # Detect delimiter
        sample = decoded[:1024]
        delimiter = infer_delimiter(sample)
        return parse_csv(decoded, delimiter)

    elif fmt in ('xls', 'xlsx'):
        file_like = BytesIO(content)
        columns, rows = parse_spreadsheet(file_like, fmt)
        if columns is None or rows is None:
            raise ValueError("Unsupported or malformed spreadsheet")
        return columns, rows
    else:
        raise ValueError("Unrecognized format")


def get_dataset_id(source_url):
    """Generate deterministic dataset ID from source URL."""
    return hashlib.md5(source_url.encode('utf-8')).hexdigest()


def get_content_id(content_bytes):
    """Generate deterministic dataset ID from file content bytes."""
    return hashlib.md5(content_bytes).hexdigest()


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
    Convert a remote file to a dataset.
    Supports CSV, .xls, and .xlsx formats.
    Query parameters:
      - source: URL of remote file (required)
      - charset: Character encoding (optional, for CSV only)
      - force: Presence flag to force re-ingestion (optional)
    """
    source = request.args.get('source', '')
    charset = request.args.get('charset')
    force = request.args.get('force')

    if not source:
        return jsonify({'ok': False, 'error': 'Missing source parameter'}), 400

    if not is_valid_url(source):
        return jsonify({'ok': False, 'error': 'Invalid URL'}), 400

    # Check for repeated force parameter
    if 'force' in request.args:
        force_values = request.args.getlist('force')
        if len(force_values) > 1:
            return jsonify({'ok': False, 'error': 'Repeated control parameter: force'}), 400

    dataset_id = get_dataset_id(source)

    # Check cache if enabled and dataset exists
    if CACHE_ENABLED and dataset_id in datasets and force is None:
        # Return cached dataset
        endpoint = f'/datasets/{dataset_id}'
        return jsonify({'ok': True, 'endpoint': endpoint})

    # If force is present, we'll re-ingest (but only if caching is enabled or forced)
    # If caching is disabled, force has no additional effect - we still re-download/parse

    # Fetch remote file
    try:
        response = requests.get(source, timeout=30)
        response.raise_for_status()
        content = response.content
    except requests.RequestException:
        # Re-ingestion failure: keep existing error codes and envelope
        # Keep prior dataset queryable if it existed
        if dataset_id in datasets:
            endpoint = f'/datasets/{dataset_id}'
            return jsonify({'ok': True, 'endpoint': endpoint, 'warning': 'Using cached dataset due to re-ingestion failure'})
        return jsonify({'ok': False, 'error': 'Source unreachable or remote HTTP error'}), 404

    # Check for empty content
    if not content or len(content.strip()) == 0:
        # Re-ingestion failure: keep existing error codes and envelope
        if dataset_id in datasets:
            endpoint = f'/datasets/{dataset_id}'
            return jsonify({'ok': True, 'endpoint': endpoint, 'warning': 'Using cached dataset due to re-ingestion failure'})
        return jsonify({'ok': False, 'error': 'Non-tabular content'}), 400

    # Parse the file
    try:
        columns, rows = parse_file_content(content, source, charset)
    except ValueError as e:
        # Re-ingestion failure: keep existing error codes and envelope
        if dataset_id in datasets:
            endpoint = f'/datasets/{dataset_id}'
            return jsonify({'ok': True, 'endpoint': endpoint, 'warning': f'Using cached dataset due to parsing failure: {str(e)}'})
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception:
        # Re-ingestion failure: keep existing error codes and envelope
        if dataset_id in datasets:
            endpoint = f'/datasets/{dataset_id}'
            return jsonify({'ok': True, 'endpoint': endpoint, 'warning': 'Using cached dataset due to parsing failure'})
        return jsonify({'ok': False, 'error': 'Unrecognized format'}), 400

    # Store dataset (replaces cached dataset if force was used)
    save_dataset(dataset_id, columns, rows)

    endpoint = f'/datasets/{dataset_id}'
    return jsonify({'ok': True, 'endpoint': endpoint})


@app.route('/upload', methods=['POST'])
def upload():
    """
    Upload a file to create a dataset.
    Accepts multipart form with field 'file' or 'attachment'.
    Supports CSV, .xls, and .xlsx formats.
    """
    # Check for multipart content
    if not request.is_multipart:
        return jsonify({'ok': False, 'error': 'non-multipart upload'}), 415

    # Check for file field
    if 'file' in request.files:
        file_field = request.files['file']
    elif 'attachment' in request.files:
        file_field = request.files['attachment']
    else:
        return jsonify({'ok': False, 'error': 'missing file field'}), 400

    # Get filename and content
    filename = file_field.filename
    if not filename:
        return jsonify({'ok': False, 'error': 'missing file field'}), 400

    content = file_field.read()

    # Check for empty content
    if not content or len(content.strip()) == 0:
        return jsonify({'ok': False, 'error': 'Non-tabular content'}), 400

    # Parse the file
    try:
        columns, rows = parse_file_content(content, filename)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception:
        return jsonify({'ok': False, 'error': 'Unrecognized format'}), 400

    # Store dataset with content-based ID for determinism
    dataset_id = get_content_id(content)
    save_dataset(dataset_id, columns, rows)

    endpoint = f'/datasets/{dataset_id}'
    return jsonify({'ok': True, 'endpoint': endpoint})


@app.route('/datasets/<dataset_id>/export', methods=['GET'])
def export_dataset(dataset_id):
    """
    Export a dataset as CSV.
    Applies the same filters, sort, and pagination as /datasets/<id>.
    """
    if dataset_id not in datasets:
        return jsonify({'ok': False, 'error': 'Unknown dataset id'}), 404

    columns, rows = datasets[dataset_id]

    # Check for repeated control parameters
    control_params = ['_size', '_offset', '_sort', '_sort_desc']
    for param in control_params:
        if param in request.args:
            values = request.args.getlist(param)
            if len(values) > 1:
                return jsonify({'ok': False, 'error': f'Repeated control parameter: {param}'}), 400

    # Parse and validate _size
    size = None  # None means export all rows
    if '_size' in request.args:
        size_str = request.args.get('_size')
        if not size_str.isdigit() or size_str.startswith('0'):
            return jsonify({'ok': False, 'error': '_size must be a positive integer'}), 400
        size = int(size_str)

    # Parse and validate _offset
    offset = 0
    if '_offset' in request.args:
        offset_str = request.args.get('_offset')
        if not offset_str.isdigit():
            return jsonify({'ok': False, 'error': '_offset must be a non-negative integer'}), 400
        offset = int(offset_str)

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
                pass
        elif comparator == 'greater':
            try:
                filter_val = float(value)
                filtered_rows = [row for row in filtered_rows
                                 if isinstance(row[col_index], (int, float)) and float(row[col_index]) > filter_val]
            except (ValueError, TypeError):
                pass

    # Make a copy of rows for sorting
    sorted_rows = filtered_rows

    # Apply sorting before pagination
    if sort_col is not None:
        col_index = columns.index(sort_col)
        # Stable sort using Python's Timsort (stable)
        sorted_rows.sort(key=lambda row: row[col_index], reverse=sort_desc)

    # Apply pagination
    if offset > len(sorted_rows):
        paginated_rows = []
    else:
        paginated_rows = sorted_rows[offset:offset + size] if size is not None else sorted_rows[offset:]

    # Generate CSV output
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in paginated_rows:
        writer.writerow(row)

    csv_output = output.getvalue()

    # Create response with CSV attachment
    response = send_file(
        BytesIO(csv_output.encode('utf-8')),
        mimetype='text/csv',
        as_attachment=True,
        download_name=f'{dataset_id}.csv'
    )
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename="{dataset_id}.csv"'

    return response


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

    # Parse and validate _offset
    offset = 0
    if '_offset' in request.args:
        offset_str = request.args.get('_offset')
        if not offset_str.isdigit():
            return jsonify({'ok': False, 'error': '_offset must be a non-negative integer'}), 400
        offset = int(offset_str)

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
        result_rows = []
        for i, row in enumerate(paginated_rows):
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
