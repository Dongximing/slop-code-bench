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
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error
import charset_normalizer


def detect_delimiter(text: str) -> str:
    """Detect CSV delimiter from text content."""
    delimiters = [',', ';', '\t']
    first_line = text.split('\n')[0]
    counts = {d: first_line.count(d) for d in delimiters}
    best = max(delimiters, key=lambda d: counts[d])
    return best if counts[best] > 0 else ','


def is_time_like(value: str) -> bool:
    """Check if a value looks like a time (e.g., 08:30, 9:15, 12:00)."""
    return bool(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', value.strip()))


def parse_value(value: str):
    """Parse a string value into appropriate type."""
    if value == '' or is_time_like(value):
        return value

    if '.' not in value and 'e' not in value.lower():
        try:
            return int(value)
        except ValueError:
            pass

    try:
        return float(value)
    except ValueError:
        return value


def generate_dataset_id(source_url: str) -> str:
    """Generate a deterministic dataset ID from source URL."""
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def is_valid_url(url: str) -> bool:
    """Check if URL is valid with http/https scheme."""
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc and parsed.scheme in ('http', 'https'))
    except Exception:
        return False


def validate_charset(charset: str) -> bool:
    """Check if charset is valid and supported."""
    try:
        'test'.encode(charset)
        return True
    except (LookupError, UnicodeError):
        return False


def decode_content(content: bytes, charset: str | None = None) -> str | None:
    """Decode bytes to string using charset or auto-detection. Returns None on failure."""
    if charset:
        try:
            return content.decode(charset)
        except UnicodeDecodeError:
            return None

    result = charset_normalizer.from_bytes(content)
    if result and (best := result.best()):
        return content.decode(best.encoding)

    for fallback in ('utf-8', 'latin-1'):
        try:
            return content.decode(fallback)
        except UnicodeDecodeError:
            continue
    return None


_datasets: dict = {}


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
        if 'source' not in query_params or not query_params['source']:
            self.send_error_response(400, 'Missing required parameter: source')
            return

        source = query_params['source'][0]

        if not is_valid_url(source):
            self.send_error_response(400, 'Invalid URL')
            return

        charset = query_params['charset'][0] if query_params.get('charset') else None
        if charset and not validate_charset(charset):
            self.send_error_response(400, f'Unsupported or malformed charset: {charset}')
            return

        dataset_id = generate_dataset_id(source)

        if dataset_id in _datasets:
            self.send_json_response(200, {
                'ok': True,
                'endpoint': f'/datasets/{dataset_id}'
            })
            return

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

        text = decode_content(content, charset)
        if text is None:
            error_msg = f'Failed to decode with charset: {charset}' if charset else 'Could not detect encoding'
            self.send_error_response(400, error_msg)
            return

        delimiter = detect_delimiter(text)

        try:
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            rows = list(reader)
        except Exception as e:
            self.send_error_response(400, f'Failed to parse CSV: {str(e)}')
            return

        if len(rows) < 2:
            self.send_error_response(400, 'Non-tabular content: requires at least one header row and one data row')
            return

        # Validate consistent column count
        num_cols = len(rows[0])
        if num_cols == 0:
            self.send_error_response(400, 'Non-tabular content: no columns detected')
            return
        if any(len(row) != num_cols for row in rows):
            self.send_error_response(400, 'Non-tabular content: inconsistent column count')
            return

        columns = rows[0]
        data_rows = [[parse_value(cell) for cell in row] for row in rows[1:]]

        _datasets[dataset_id] = {'columns': columns, 'rows': data_rows}

        self.send_json_response(200, {
            'ok': True,
            'endpoint': f'/datasets/{dataset_id}'
        })

    def handle_dataset(self, dataset_id: str):
        """Handle /datasets/<id> endpoint."""
        dataset = _datasets.get(dataset_id)
        if dataset is None:
            self.send_error_response(404, 'Dataset not found')
            return

        start_time = time.time()
        self.send_json_response(200, {
            'ok': True,
            'columns': dataset['columns'],
            'rows': dataset['rows'][:100],
            'query_ms': (time.time() - start_time) * 1000
        })

    def log_message(self, format, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description='datagate - CSV data ingestion and query service')
    subparsers = parser.add_subparsers(dest='command')

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
