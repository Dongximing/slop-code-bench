#!/usr/bin/env python3
"""Datagate - CSV conversion and dataset serving server."""

import argparse
import chardet
import csv
import hashlib
import json
import re
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error


# Storage for datasets - keyed by deterministic ID
datasets = {}


def generate_dataset_id(source_url: str) -> str:
    """Generate a deterministic ID from the source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def detect_charset(content: bytes) -> str:
    """Detect character encoding from content bytes."""
    result = chardet.detect(content)
    return result['encoding'] or 'utf-8'


def infer_delimiter(sample: str) -> str:
    """Infer the CSV delimiter from a sample string."""
    delimiters = {',': 0, ';': 0, '\t': 0}

    lines = sample.split('\n')
    if len(lines) < 2:
        return ','  # Default to comma if not enough lines

    header_line = lines[0]

    for delim in delimiters:
        # Count occurrences in header
        count = header_line.count(delim)
        if count > delimiters[delim]:
            delimiters[delim] = count

    # Return the delimiter with highest count
    return max(delimiters, key=delimiters.get)


def infer_type(value: str):
    """Infer the type of a CSV value.

    - Integers/decimals become JSON numbers
    - Time-like values (e.g., 08:30, 9:15, 12:00) remain text
    - Strings remain text
    """
    value = value.strip()

    # Check if it's a time-like value (contains colon, HH:MM format)
    if ':' in value:
        # Match time patterns like 08:30, 9:15, 12:00
        time_pattern = r'^\d{1,2}:\d{2}$'
        if re.match(time_pattern, value):
            return value  # Keep time as text

    # Try to parse as integer
    try:
        return int(value)
    except ValueError:
        pass

    # Try to parse as float/decimal
    try:
        return float(value)
    except ValueError:
        pass

    # Return as string
    return value


def fetch_and_parse_csv(source_url: str, charset: Optional[str] = None):
    """Fetch CSV from URL and parse it into columns and rows."""
    try:
        request = urllib.request.Request(
            source_url,
            headers={'User-Agent': 'Datagate/1.0'}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
    except urllib.error.URLError as e:
        raise ValueError(f"Source unreachable: {str(e)}")
    except Exception as e:
        raise ValueError(f"Failed to fetch source: {str(e)}")

    # Determine charset
    if charset:
        try:
            text = content.decode(charset)
        except (UnicodeDecodeError, LookupError) as e:
            raise ValueError(f"Invalid charset: {str(e)}")
    else:
        detected_charset = detect_charset(content)
        try:
            text = content.decode(detected_charset)
        except UnicodeDecodeError as e:
            raise ValueError(f"Failed to decode content: {str(e)}")

    # Check if content looks like CSV
    if '\n' not in text and '\r' not in text:
        raise ValueError("Non-tabular content")

    # Infer delimiter from first few lines
    sample = '\n'.join(text.split('\n')[:5])
    delimiter = infer_delimiter(sample)

    # Parse CSV
    lines = text.strip().split('\n')
    if len(lines) < 2:
        raise ValueError("A valid file requires at least one header row and one data row")

    # Use csv.reader for proper parsing
    reader = csv.reader(lines, delimiter=delimiter)

    rows = list(reader)

    if len(rows) < 2:
        raise ValueError("A valid file requires at least one header row and one data row")

    columns = rows[0]
    data_rows = rows[1:]

    # Validate that it's tabular (all rows have same number of columns or fewer)
    if not all(len(row) <= len(columns) for row in data_rows):
        raise ValueError("Non-tabular content - inconsistent column counts")

    # Pad rows to match column count
    for row in data_rows:
        while len(row) < len(columns):
            row.append('')

    # Infer types for each value
    typed_rows = []
    for row in data_rows[:100]:  # Limit to 100 rows for storage
        typed_row = [infer_type(val) for val in row[:len(columns)]]
        typed_rows.append(typed_row)

    return columns, typed_rows


class DatagateHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Datagate."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def send_cors_headers(self):
        """Add CORS headers to response."""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')

    def send_json_response(self, status_code: int, data: dict):
        """Send a JSON response with CORS headers."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed_path = urlparse(self.path)
        path = parsed_path.path
        query_params = parse_qs(parsed_path.query)

        try:
            if path == '/convert':
                self.handle_convert(query_params)
            elif path.startswith('/datasets/'):
                dataset_id = path.split('/')[-1]
                self.handle_dataset(dataset_id)
            else:
                self.send_json_response(404, {
                    'ok': False,
                    'error': 'Not found'
                })
        except Exception as e:
            self.send_json_response(500, {
                'ok': False,
                'error': str(e)
            })

    def handle_convert(self, query_params: dict):
        """Handle /convert endpoint."""
        # Check required source parameter
        if 'source' not in query_params:
            self.send_json_response(400, {
                'ok': False,
                'error': 'Missing source parameter'
            })
            return

        source_url = query_params['source'][0]

        # Validate URL
        try:
            parsed_url = urlparse(source_url)
            if not parsed_url.scheme or not parsed_url.netloc:
                raise ValueError("Invalid URL")
        except Exception:
            self.send_json_response(400, {
                'ok': False,
                'error': 'Invalid URL'
            })
            return

        # Get charset if provided
        charset = query_params['charset'][0] if 'charset' in query_params else None

        # Validate charset if provided
        if charset:
            try:
                # Test if charset is valid by trying to encode/decode
                'test'.encode(charset)
            except (LookupError, TypeError):
                self.send_json_response(400, {
                    'ok': False,
                    'error': 'Unsupported or malformed charset'
                })
                return

        # Generate deterministic ID
        dataset_id = generate_dataset_id(source_url)

        # Check if we already have this dataset
        if dataset_id not in datasets:
            # Fetch and parse CSV
            try:
                columns, rows = fetch_and_parse_csv(source_url, charset)
                datasets[dataset_id] = {
                    'columns': columns,
                    'rows': rows
                }
            except ValueError as e:
                error_msg = str(e)
                if 'unreachable' in error_msg or 'Failed to fetch' in error_msg:
                    self.send_json_response(404, {
                        'ok': False,
                        'error': error_msg
                    })
                else:
                    self.send_json_response(400, {
                        'ok': False,
                        'error': error_msg
                    })
                return

        self.send_json_response(200, {
            'ok': True,
            'endpoint': f'/datasets/{dataset_id}'
        })

    def handle_dataset(self, dataset_id: str):
        """Handle /datasets/<id> endpoint."""
        if dataset_id not in datasets:
            self.send_json_response(404, {
                'ok': False,
                'error': 'Dataset not found'
            })
            return

        dataset = datasets[dataset_id]
        parsed_path = urlparse(self.path)
        query_params = parse_qs(parsed_path.query)
        start_time = time.time()

        # Validate and parse control parameters
        error_response = self.validate_control_params(query_params)
        if error_response:
            self.send_json_response(400, error_response)
            return

        # Extract parameter values (parse_qs returns lists)
        _size = int(query_params.get('_size', ['100'])[0])
        _offset = int(query_params.get('_offset', ['0'])[0])
        _shape = query_params.get('_shape', ['lists'])[0]
        _sort = query_params.get('_sort', [None])[0]
        _sort_desc = query_params.get('_sort_desc', [None])[0]
        _rowid = query_params.get('_rowid', [None])[0]
        _total = query_params.get('_total', [None])[0]

        columns = dataset['columns']
        rows = dataset['rows']

        # Determine sort column (if both _sort and _sort_desc are present, _sort_desc wins)
        if _sort_desc:
            sort_col = _sort_desc
            reverse = True
        elif _sort:
            sort_col = _sort
            reverse = False
        else:
            sort_col = None
            reverse = False

        # Validate sort column exists
        if sort_col:
            if sort_col not in columns:
                self.send_json_response(400, {
                    'ok': False,
                    'error': f"Unknown column: {sort_col}"
                })
                return
            # Validate _sort and _sort_desc are not empty strings
            if (_sort and _sort.strip() == '') or (_sort_desc and _sort_desc.strip() == ''):
                self.send_json_response(400, {
                    'ok': False,
                    'error': 'Empty column name for sorting'
                })
                return

        # Create a copy with original indices for rowid tracking
        # tuples: (original_index, row_data)
        indexed_rows = list(enumerate(rows))

        # Sorting - applied before pagination
        if sort_col:
            col_index = columns.index(sort_col)
            # Sort with safe type handling to handle mixed types
            def sort_key(item):
                val = item[1][col_index]
                t = type(val)
                # Use a tuple (type_priority, value) for cross-type comparison
                if t is int or t is float:
                    return (0, val)
                elif val == '':
                    # Empty strings sort last
                    return (2, '')
                else:
                    # String values sort in middle
                    return (1, str(val))
            # Stable sort using index as secondary key
            indexed_rows = sorted(indexed_rows, key=lambda x: (sort_key(x), x[0]), reverse=reverse)

        total = len(indexed_rows)

        # Apply pagination - only on the data, not the indices
        paginated_indexed = indexed_rows[_offset:_offset + _size]

        # Handle visibility toggles
        include_total = _total != 'hide'
        hide_rowid = _rowid == 'hide'

        # Apply shape transformation (rowid handling is inside this block)
        if _shape == 'objects':
            rows_to_return = []
            for orig_idx, row in paginated_indexed:
                row_obj = {}
                if not hide_rowid:  # Include rowid unless explicitly hidden
                    row_obj['rowid'] = orig_idx + 1  # 1-based source-file row number
                for j, col in enumerate(columns):
                    row_obj[col] = row[j]
                rows_to_return.append(row_obj)
        else:
            rows_to_return = [row for _, row in paginated_indexed]

        # Build response
        response = {
            'ok': True,
            'columns': columns,
            'rows': rows_to_return,
            'query_ms': round((time.time() - start_time) * 1000, 1)
        }

        if include_total:
            response['total'] = total

        self.send_json_response(200, response)

    def validate_control_params(self, query_params: dict) -> dict | None:
        """Validate control parameters and return error response if invalid, None if valid."""
        # Check for repeated parameters
        repeated_params = []
        for param in ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']:
            if param in query_params and len(query_params[param]) > 1:
                repeated_params.append(param)
        if repeated_params:
            return {
                'ok': False,
                'error': f"Repeated parameter: {', '.join(repeated_params)}"
            }

        # Validate _size
        if '_size' in query_params:
            try:
                size = int(query_params['_size'][0])
                if size <= 0:
                    return {'ok': False, 'error': '_size must be a positive integer'}
            except ValueError:
                return {'ok': False, 'error': '_size must be a positive integer'}

        # Validate _offset
        if '_offset' in query_params:
            try:
                offset = int(query_params['_offset'][0])
                if offset < 0:
                    return {'ok': False, 'error': '_offset must be a non-negative integer'}
            except ValueError:
                return {'ok': False, 'error': '_offset must be a non-negative integer'}

        # Validate _shape
        if '_shape' in query_params:
            shape = query_params['_shape'][0]
            if shape not in ['lists', 'objects']:
                return {'ok': False, 'error': '_shape must be lists or objects'}

        # Validate _rowid
        if '_rowid' in query_params:
            rowid = query_params['_rowid'][0]
            if rowid != 'hide':
                return {'ok': False, 'error': '_rowid must be hide'}

        # Validate _total
        if '_total' in query_params:
            total = query_params['_total'][0]
            if total != 'hide':
                return {'ok': False, 'error': '_total must be hide'}

        # Validate _sort and _sort_desc columns
        if '_sort' in query_params or '_sort_desc' in query_params:
            columns = datasets.get(list(datasets.keys())[0] if datasets else {}, {}).get('columns', [])
            # We can't validate columns here because we don't know the dataset yet
            # This will be validated after we get the dataset

        return None


def main():
    parser = argparse.ArgumentParser(description='Datagate - CSV conversion server')
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    start_parser = subparsers.add_parser('start', help='Start the server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command != 'start':
        parser.print_help()
        sys.exit(1)

    server = HTTPServer((args.address, args.port), DatagateHandler)
    print(f'Datagate server starting on {args.address}:{args.port}')
    print('Press Ctrl+C to stop')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down server...')
        server.shutdown()


if __name__ == '__main__':
    main()
