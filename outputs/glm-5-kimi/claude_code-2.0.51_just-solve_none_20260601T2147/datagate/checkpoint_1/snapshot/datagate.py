#!/usr/bin/env python3
"""datagate - A CSV data ingestion and query service."""

import argparse
import csv
import hashlib
import io
import json
import re
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
import urllib.request
import urllib.error
import charset_normalizer


def detect_delimiter(text: str) -> str:
    """Detect CSV delimiter from text content."""
    delimiters = [',', ';', '\t']
    first_line = text.split('\n')[0]

    counts = {d: first_line.count(d) for d in delimiters}

    # Return the delimiter with highest count
    best = max(delimiters, key=lambda d: counts[d])
    return best if counts[best] > 0 else ','


def is_time_like(value: str) -> bool:
    """Check if a value looks like a time (e.g., 08:30, 9:15, 12:00)."""
    if not isinstance(value, str):
        return False
    # Match patterns like HH:MM, H:MM, HH:MM:SS, etc.
    time_pattern = r'^\d{1,2}:\d{2}(:\d{2})?$'
    return bool(re.match(time_pattern, value.strip()))


def parse_value(value: str):
    """Parse a string value into appropriate type."""
    if value == '':
        return ''

    # Keep time-like values as strings
    if is_time_like(value):
        return value

    # Try integer
    try:
        if '.' not in value and 'e' not in value.lower():
            int_val = int(value)
            return int_val
    except ValueError:
        pass

    # Try float
    try:
        float_val = float(value)
        return float_val
    except ValueError:
        pass

    # Return as string
    return value


def generate_dataset_id(source_url: str) -> str:
    """Generate a deterministic dataset ID from source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


class DatasetStore:
    """In-memory store for datasets."""
    def __init__(self):
        self.datasets = {}

    def store(self, dataset_id: str, columns: list, rows: list):
        """Store a dataset."""
        self.datasets[dataset_id] = {
            'columns': columns,
            'rows': rows
        }

    def get(self, dataset_id: str):
        """Retrieve a dataset."""
        return self.datasets.get(dataset_id)

    def exists(self, dataset_id: str) -> bool:
        """Check if dataset exists."""
        return dataset_id in self.datasets


# Global store
store = DatasetStore()


class DataGateHandler(BaseHTTPRequestHandler):
    """HTTP request handler for datagate."""

    def send_json_response(self, status_code: int, data: dict):
        """Send a JSON response with CORS headers."""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def send_error_response(self, status_code: int, message: str):
        """Send an error JSON response."""
        self.send_json_response(status_code, {'ok': False, 'error': message})

    def do_OPTIONS(self):
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        """Handle GET requests."""
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query_params = parse_qs(parsed_url.query)

        if path == '/convert':
            self.handle_convert(query_params)
        elif path.startswith('/datasets/'):
            dataset_id = path[len('/datasets/'):]
            self.handle_dataset(dataset_id)
        else:
            self.send_error_response(404, 'Not found')

    def handle_convert(self, query_params: dict):
        """Handle /convert endpoint."""
        # Check for source parameter
        if 'source' not in query_params or not query_params['source']:
            self.send_error_response(400, 'Missing required parameter: source')
            return

        source = query_params['source'][0]

        # Validate URL
        try:
            parsed = urlparse(source)
            if not parsed.scheme or not parsed.netloc:
                self.send_error_response(400, 'Invalid URL')
                return
            if parsed.scheme not in ('http', 'https'):
                self.send_error_response(400, 'Invalid URL')
                return
        except Exception:
            self.send_error_response(400, 'Invalid URL')
            return

        # Validate charset before anything else
        if 'charset' in query_params and query_params['charset']:
            charset = query_params['charset'][0]
            try:
                'test'.encode(charset)
            except (LookupError, UnicodeError):
                self.send_error_response(400, f'Unsupported or malformed charset: {charset}')
                return
        else:
            charset = None

        # Generate dataset ID
        dataset_id = generate_dataset_id(source)

        # Check if already exists
        if store.exists(dataset_id):
            self.send_json_response(200, {
                'ok': True,
                'endpoint': f'/datasets/{dataset_id}'
            })
            return

        # Fetch the remote CSV
        try:
            request = urllib.request.Request(source, headers={'User-Agent': 'datagate/1.0'})
            response = urllib.request.urlopen(request, timeout=30)
            content = response.read()
        except urllib.error.HTTPError as e:
            self.send_error_response(404, f'Remote HTTP error: {e.code}')
            return
        except urllib.error.URLError as e:
            self.send_error_response(404, f'Source unreachable: {str(e.reason)}')
            return
        except Exception as e:
            self.send_error_response(404, f'Source unreachable: {str(e)}')
            return

        # Decode bytes using charset or auto-detect
        if charset:
            try:
                text = content.decode(charset)
            except UnicodeDecodeError:
                self.send_error_response(400, f'Failed to decode with charset: {charset}')
                return
        else:
            # Auto-detect encoding
            result = charset_normalizer.from_bytes(content)
            if result:
                best = result.best()
                if best:
                    text = content.decode(best.encoding)
                else:
                    # Fallback to utf-8
                    try:
                        text = content.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            text = content.decode('latin-1')
                        except:
                            self.send_error_response(400, 'Could not detect encoding')
                            return
            else:
                try:
                    text = content.decode('utf-8')
                except UnicodeDecodeError:
                    try:
                        text = content.decode('latin-1')
                    except:
                        self.send_error_response(400, 'Could not detect encoding')
                        return

        # Detect delimiter
        delimiter = detect_delimiter(text)

        # Parse CSV
        try:
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
        except Exception as e:
            self.send_error_response(400, f'Failed to parse CSV: {str(e)}')
            return

        # Validate: need at least header + 1 data row
        if len(rows) < 2:
            self.send_error_response(400, 'Non-tabular content: requires at least one header row and one data row')
            return

        # Check if all rows have same number of columns
        if rows:
            num_cols = len(rows[0])
            if num_cols == 0:
                self.send_error_response(400, 'Non-tabular content: no columns detected')
                return
            for row in rows:
                if len(row) != num_cols:
                    self.send_error_response(400, 'Non-tabular content: inconsistent column count')
                    return

        # Extract columns (header row)
        columns = rows[0]

        # Parse data rows
        data_rows = []
        for row in rows[1:]:
            parsed_row = [parse_value(cell) for cell in row]
            data_rows.append(parsed_row)

        # Store dataset
        store.store(dataset_id, columns, data_rows)

        self.send_json_response(200, {
            'ok': True,
            'endpoint': f'/datasets/{dataset_id}'
        })

    def handle_dataset(self, dataset_id: str):
        """Handle /datasets/<id> endpoint."""
        dataset = store.get(dataset_id)

        if dataset is None:
            self.send_error_response(404, 'Dataset not found')
            return

        start_time = time.time()

        # Get rows (limit to 100)
        rows = dataset['rows'][:100]
        columns = dataset['columns']

        query_ms = (time.time() - start_time) * 1000

        self.send_json_response(200, {
            'ok': True,
            'columns': columns,
            'rows': rows,
            'query_ms': query_ms
        })

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def main():
    parser = argparse.ArgumentParser(description='datagate - CSV data ingestion and query service')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Start command
    start_parser = subparsers.add_parser('start', help='Start the datagate server')
    start_parser.add_argument('--port', type=int, default=8001, help='Port to listen on (default: 8001)')
    start_parser.add_argument('--address', type=str, default='127.0.0.1', help='Address to bind to (default: 127.0.0.1)')

    args = parser.parse_args()

    if args.command == 'start':
        server_address = (args.address, args.port)
        httpd = HTTPServer(server_address, DataGateHandler)
        print(f'datagate running on http://{args.address}:{args.port}')
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\nShutting down...')
            httpd.shutdown()
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
