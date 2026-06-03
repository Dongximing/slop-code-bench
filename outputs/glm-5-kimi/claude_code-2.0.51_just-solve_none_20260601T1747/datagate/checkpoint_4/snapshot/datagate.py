#!/usr/bin/env python3
"""datagate - CSV ingestion and query service."""

import argparse
import csv
import hashlib
import io
import re
import signal
import time
import urllib.request
import urllib.error
from io import StringIO
from urllib.parse import urlparse

import chardet
import openpyxl
import xlrd
from flask import Flask, request, jsonify, make_response, Response

app = Flask(__name__)

datasets = {}

# Valid filter comparators
VALID_COMPARATORS = {'exact', 'contains', 'less', 'greater'}


def generate_dataset_id(source_url):
    """Generate a deterministic dataset ID from source URL using SHA-256."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def generate_dataset_id_from_bytes(content_bytes):
    """Generate a deterministic dataset ID from file content bytes using SHA-256."""
    return hashlib.sha256(content_bytes).hexdigest()[:16]


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


def parse_xlsx(content_bytes):
    """Parse XLSX content and return columns and rows."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content_bytes), read_only=True, data_only=True)
        sheet = workbook.active

        if sheet is None:
            raise ValueError("Non-tabular content: no worksheet found")

        rows = []
        for row in sheet.iter_rows(values_only=True):
            rows.append(list(row))

        workbook.close()

        if len(rows) < 2:
            raise ValueError("Non-tabular content: requires at least one header row and one data row")

        if not rows[0] or all(cell is None for cell in rows[0]):
            raise ValueError("Non-tabular content: empty header row")

        header = [str(cell) if cell is not None else '' for cell in rows[0]]
        num_cols = len(header)

        valid_rows = []
        for row in rows[1:]:
            # Convert None to empty string and ensure correct column count
            processed_row = [str(cell) if cell is not None else '' for cell in row]
            if len(processed_row) < num_cols:
                processed_row = processed_row + [''] * (num_cols - len(processed_row))
            else:
                processed_row = processed_row[:num_cols]
            valid_rows.append(processed_row)

        return header, convert_rows(valid_rows)
    except Exception as e:
        if "Non-tabular content" in str(e):
            raise
        raise ValueError(f"Failed to parse XLSX: {str(e)}")


def parse_xls(content_bytes):
    """Parse XLS content and return columns and rows."""
    try:
        workbook = xlrd.open_workbook(file_contents=content_bytes)
        sheet = workbook.sheet_by_index(0)

        if sheet.nrows < 2:
            raise ValueError("Non-tabular content: requires at least one header row and one data row")

        header = [str(sheet.cell_value(0, col)) for col in range(sheet.ncols)]

        if not header or all(cell == '' for cell in header):
            raise ValueError("Non-tabular content: empty header row")

        num_cols = len(header)
        rows = []
        for row_idx in range(1, sheet.nrows):
            row = []
            for col_idx in range(num_cols):
                cell_value = sheet.cell_value(row_idx, col_idx)
                row.append(str(cell_value) if cell_value != '' else '')
            rows.append(row)

        return header, convert_rows(rows)
    except Exception as e:
        if "Non-tabular content" in str(e):
            raise
        raise ValueError(f"Failed to parse XLS: {str(e)}")


def detect_format(content_bytes, filename=None):
    """Detect file format from content or filename.

    Returns: 'csv', 'xlsx', 'xls', or None if unrecognized.
    """
    # Try to detect from content first
    # XLSX files are ZIP archives with specific magic bytes (PK header)
    if len(content_bytes) >= 4:
        # XLSX magic bytes (ZIP format: PK\x03\x04)
        if content_bytes[:4] == b'PK\x03\x04':
            return 'xlsx'
        # XLS magic bytes (OLE format: \xD0\xCF\x11\xE0)
        if content_bytes[:4] == b'\xD0\xCF\x11\xE0':
            return 'xls'

    # Fall back to filename extension
    if filename:
        filename_lower = filename.lower()
        if filename_lower.endswith('.xlsx'):
            return 'xlsx'
        elif filename_lower.endswith('.xls'):
            return 'xls'
        elif filename_lower.endswith('.csv'):
            return 'csv'

    # Default to CSV for text content
    return 'csv'


def parse_content(content_bytes, charset=None, filename=None):
    """Parse content (CSV, XLS, or XLSX) and return columns and rows."""
    file_format = detect_format(content_bytes, filename)

    if file_format == 'xlsx':
        return parse_xlsx(content_bytes)
    elif file_format == 'xls':
        return parse_xls(content_bytes)
    elif file_format == 'csv':
        return parse_csv(content_bytes, charset)
    else:
        raise ValueError("Unrecognized file format")


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


class QueryTimeout(Exception):
    """Raised when a query exceeds the time limit."""
    pass


def _timeout_handler(signum, frame):
    raise QueryTimeout("Query timeout")


def parse_filter_params(columns):
    """Parse and validate filter parameters from request args.

    Filter params are in the form: <column>__<comparator>=<value>
    Control params (starting with '_') are not filters.
    Params without '__' and not starting with '_' are ignored.

    Returns (filters, error_message) where filters is a list of
    (column_name, comparator, value) tuples on success, or
    (None, error_message) on failure.
    """
    filters = []
    seen_keys = set()
    valid_columns = set(columns)

    for key in request.args:
        # Control params (starting with '_') are not filters
        if key.startswith('_'):
            continue

        # Must contain '__' to be a filter
        if '__' not in key:
            continue

        # Check for duplicate filter keys (same key appearing multiple times)
        values = request.args.getlist(key)
        if len(values) > 1:
            return None, f"Duplicate filter key: {key}"

        parts = key.rsplit('__', 1)
        if len(parts) != 2:
            continue

        column, comparator = parts

        # Validate comparator
        if comparator not in VALID_COMPARATORS:
            return None, f"Invalid comparator: {comparator}"

        # Validate column exists
        if column not in valid_columns:
            return None, f"Unknown column: {column}"

        value = values[0]

        # For less/greater, the filter value must be numeric
        if comparator in ('less', 'greater'):
            try:
                float(value)
            except (ValueError, TypeError):
                return None, f"Non-numeric filter value for {comparator} comparator"

        filters.append((column, comparator, value))

    return filters, None


def apply_filters(indexed_rows, columns, filters):
    """Apply filters to indexed rows. All filters are ANDed.

    indexed_rows: list of (original_index, row) tuples
    columns: list of column names
    filters: list of (column_name, comparator, filter_value) tuples

    Returns filtered list of (original_index, row) tuples.
    """
    if not filters:
        return indexed_rows

    col_indices = {}
    for col, _, _ in filters:
        if col not in col_indices:
            col_indices[col] = columns.index(col)

    result = []
    for item in indexed_rows:
        orig_idx, row = item
        match = True
        for col, comparator, filter_value in filters:
            col_idx = col_indices[col]
            stored_value = row[col_idx]

            if comparator == 'exact':
                if str(stored_value) != filter_value:
                    match = False
                    break
            elif comparator == 'contains':
                if filter_value not in str(stored_value):
                    match = False
                    break
            elif comparator == 'less':
                try:
                    stored_num = float(stored_value)
                    filter_num = float(filter_value)
                    if not (stored_num < filter_num):
                        match = False
                        break
                except (ValueError, TypeError):
                    # Non-numeric stored values are not matched
                    match = False
                    break
            elif comparator == 'greater':
                try:
                    stored_num = float(stored_value)
                    filter_num = float(filter_value)
                    if not (stored_num > filter_num):
                        match = False
                        break
                except (ValueError, TypeError):
                    # Non-numeric stored values are not matched
                    match = False
                    break

        if match:
            result.append(item)

    return result


def apply_filters_with_timeout(indexed_rows, columns, filters, timeout_seconds=30):
    """Apply filters with a timeout guard."""
    if not filters:
        return indexed_rows

    old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(timeout_seconds)
    try:
        return apply_filters(indexed_rows, columns, filters)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


@app.route('/convert', methods=['GET', 'OPTIONS'])
def convert():
    """Handle CSV/Excel conversion endpoint."""
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
        columns, rows = parse_content(content_bytes, charset)
    except ValueError as e:
        return error_response(str(e), 400)

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    return json_response({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


@app.route('/upload', methods=['POST', 'OPTIONS'])
def upload():
    """Handle file upload endpoint."""
    if request.method == 'OPTIONS':
        return json_response({"ok": True})

    # Check if the request is multipart/form-data
    content_type = request.content_type or ''
    if not content_type.startswith('multipart/form-data'):
        return error_response("Non-multipart request", 415)

    # Check if we have the 'file' or 'attachment' field
    file = None
    filename = None

    if 'file' in request.files:
        file = request.files['file']
        filename = file.filename
    elif 'attachment' in request.files:
        file = request.files['attachment']
        filename = file.filename

    if file is None:
        return error_response("Missing file field", 400)

    # Read file content
    content_bytes = file.read()

    if len(content_bytes) == 0:
        return error_response("Empty file", 400)

    # Generate deterministic dataset ID from file content
    dataset_id = generate_dataset_id_from_bytes(content_bytes)

    # Check if already exists (re-uploading same bytes)
    if dataset_id in datasets:
        return json_response({"ok": True, "endpoint": f"/datasets/{dataset_id}"})

    # Parse the content (CSV, XLS, or XLSX)
    charset = request.form.get('charset')

    try:
        columns, rows = parse_content(content_bytes, charset, filename)
    except ValueError as e:
        return error_response(str(e), 400)

    datasets[dataset_id] = {
        "columns": columns,
        "rows": rows
    }

    return json_response({"ok": True, "endpoint": f"/datasets/{dataset_id}"})


def parse_control_params():
    """Parse and validate control parameters from request args."""
    control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
    result = {
        'size': 100,
        'offset': 0,
        'shape': 'lists',
        'sort': None,
        'sort_desc': None,
        'rowid': 'show',
        'total': 'show'
    }

    for param in control_params:
        values = request.args.getlist(param)
        if len(values) > 1:
            return None, f"Duplicate parameter: {param}"
        if len(values) == 1:
            value = values[0]

            if param == '_size':
                try:
                    size = int(value)
                    if size <= 0:
                        return None, "_size must be a positive integer"
                    result['size'] = size
                except ValueError:
                    return None, "_size must be a positive integer"

            elif param == '_offset':
                try:
                    offset = int(value)
                    if offset < 0:
                        return None, "_offset must be a non-negative integer"
                    result['offset'] = offset
                except ValueError:
                    return None, "_offset must be a non-negative integer"

            elif param == '_shape':
                if value not in ('lists', 'objects'):
                    return None, "_shape must be 'lists' or 'objects'"
                result['shape'] = value

            elif param == '_sort':
                if not value:
                    return None, "_sort value cannot be empty"
                result['sort'] = value

            elif param == '_sort_desc':
                if not value:
                    return None, "_sort_desc value cannot be empty"
                result['sort_desc'] = value

            elif param == '_rowid':
                if value != 'hide':
                    return None, "_rowid must be 'hide'"
                result['rowid'] = 'hide'

            elif param == '_total':
                if value != 'hide':
                    return None, "_total must be 'hide'"
                result['total'] = 'hide'

    return result, None


@app.route('/datasets/<dataset_id>', methods=['GET', 'OPTIONS'])
def get_dataset(dataset_id):
    """Handle dataset query endpoint."""
    if request.method == 'OPTIONS':
        return json_response({"ok": True})

    if dataset_id not in datasets:
        return error_response("Dataset not found", 404)

    dataset = datasets[dataset_id]
    columns = dataset["columns"]
    rows = dataset["rows"]

    # Parse control parameters
    controls, error = parse_control_params()
    if error:
        return error_response(error, 400)

    # Parse filter parameters
    filters, error = parse_filter_params(columns)
    if error:
        return error_response(error, 400)

    # Validate sort column
    sort_column = controls['sort_desc'] or controls['sort']
    if sort_column and sort_column not in columns:
        return error_response(f"Unknown column: {sort_column}", 400)

    # Create indexed rows for stable sorting
    indexed_rows = list(enumerate(rows))

    # Apply filters (before sorting)
    if filters:
        try:
            indexed_rows = apply_filters_with_timeout(indexed_rows, columns, filters)
        except QueryTimeout:
            return error_response("Query timeout", 400)

    # Total count is after filtering, before pagination
    total = len(indexed_rows)

    # Sorting (after filtering)
    if sort_column:
        col_index = columns.index(sort_column)

        def sort_key(item):
            idx, row = item
            val = row[col_index]
            if val is None:
                return (2, '')
            if val == '':
                return (1, '')
            return (0, val)

        indexed_rows = sorted(indexed_rows, key=sort_key, reverse=bool(controls['sort_desc']))

    # Pagination
    offset = controls['offset']
    size = controls['size']

    if offset >= total:
        paginated_indexed = []
    else:
        paginated_indexed = indexed_rows[offset:offset + size]

    response = {"ok": True}

    if controls['total'] != 'hide':
        response['total'] = total

    response['columns'] = columns

    if controls['shape'] == 'lists':
        response['rows'] = [row for idx, row in paginated_indexed]
    else:
        object_rows = []
        for orig_idx, row in paginated_indexed:
            obj = {}
            if controls['rowid'] != 'hide':
                obj['rowid'] = orig_idx + 1
            for j, col in enumerate(columns):
                obj[col] = row[j]
            object_rows.append(obj)
        response['rows'] = object_rows

    start_time = time.perf_counter()
    query_ms = (time.perf_counter() - start_time) * 1000
    response['query_ms'] = round(query_ms, 1)

    return json_response(response)


@app.route('/datasets/<dataset_id>/export', methods=['GET', 'OPTIONS'])
def export_dataset(dataset_id):
    """Handle dataset export endpoint - returns CSV."""
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response

    if dataset_id not in datasets:
        return error_response("Dataset not found", 404)

    dataset = datasets[dataset_id]
    columns = dataset["columns"]
    rows = dataset["rows"]

    # Parse control parameters (only filter, sort, paginate matter for export)
    controls, error = parse_control_params()
    if error:
        return error_response(error, 400)

    # Parse filter parameters
    filters, error = parse_filter_params(columns)
    if error:
        return error_response(error, 400)

    # Validate sort column
    sort_column = controls['sort_desc'] or controls['sort']
    if sort_column and sort_column not in columns:
        return error_response(f"Unknown column: {sort_column}", 400)

    # Create indexed rows for stable sorting
    indexed_rows = list(enumerate(rows))

    # Apply filters (before sorting) - use direct apply_filters without timeout
    # since signal-based timeout doesn't work in Flask threads
    if filters:
        indexed_rows = apply_filters(indexed_rows, columns, filters)

    # Sorting (after filtering)
    if sort_column:
        col_index = columns.index(sort_column)

        def sort_key(item):
            idx, row = item
            val = row[col_index]
            if val is None:
                return (2, '')
            if val == '':
                return (1, '')
            return (0, val)

        indexed_rows = sorted(indexed_rows, key=sort_key, reverse=bool(controls['sort_desc']))

    # Pagination
    total = len(indexed_rows)
    offset = controls['offset']
    size = controls['size']

    if offset >= total:
        paginated_indexed = []
    else:
        paginated_indexed = indexed_rows[offset:offset + size]

    # Generate CSV
    output = StringIO()
    writer = csv.writer(output)

    # Write header
    writer.writerow(columns)

    # Write data rows (in source column order)
    for idx, row in paginated_indexed:
        writer.writerow(row)

    csv_content = output.getvalue()

    # Create response with proper headers
    response = Response(csv_content, mimetype='text/csv')
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename="{dataset_id}.csv"'
    response.headers['Access-Control-Allow-Origin'] = '*'

    return response


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
