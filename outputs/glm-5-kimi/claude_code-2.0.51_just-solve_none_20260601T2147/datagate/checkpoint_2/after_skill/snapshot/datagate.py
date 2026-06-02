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
    delimiters = [',', ';', '\t']
    first_line = text.split('\n')[0]
    counts = {d: first_line.count(d) for d in delimiters}
    best = max(delimiters, key=lambda d: counts[d])
    return best if counts[best] > 0 else ','


def is_time_like(value: str) -> bool:
    return bool(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', value.strip()))


def parse_value(value: str):
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
    return hashlib.sha256(source_url.encode('utf-8')).hexdigest()[:16]


def is_valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return bool(parsed.scheme and parsed.netloc and parsed.scheme in ('http', 'https'))
    except Exception:
        return False


def validate_charset(charset: str) -> bool:
    try:
        'test'.encode(charset)
        return True
    except (LookupError, UnicodeError):
        return False


def decode_content(content: bytes, charset: str | None = None) -> str | None:
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
    def send_json_response(self, status_code: int, data: dict):
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    def send_error_response(self, status_code: int, message: str):
        self.send_json_response(status_code, {'ok': False, 'error': message})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
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
        dataset = _datasets.get(dataset_id)
        if dataset is None:
            self.send_error_response(404, 'Dataset not found')
            return

        parsed_url = urlparse(self.path)
        query_params = parse_qs(parsed_url.query, keep_blank_values=True)

        control_params = ['_size', '_offset', '_shape', '_sort', '_sort_desc', '_rowid', '_total']
        for param in control_params:
            if param in query_params and len(query_params[param]) > 1:
                self.send_error_response(400, f'Duplicate parameter: {param}')
                return

        columns = dataset['columns']
        rows = dataset['rows']
        total = len(rows)

        size = self._parse_positive_int(query_params, '_size', 100)
        if size is None:
            return

        offset = self._parse_non_negative_int(query_params, '_offset', 0)
        if offset is None:
            return

        shape = query_params['_shape'][0] if '_shape' in query_params else 'lists'
        if shape not in ('lists', 'objects'):
            self.send_error_response(400, '_shape must be "lists" or "objects"')
            return

        hide_rowid = self._parse_hide_flag(query_params, '_rowid')
        if hide_rowid is None:
            return

        hide_total = self._parse_hide_flag(query_params, '_total')
        if hide_total is None:
            return

        column_index = {col: idx for idx, col in enumerate(columns)}

        sort_column = None
        sort_descending = False
        if '_sort_desc' in query_params:
            sort_column = query_params['_sort_desc'][0]
            sort_descending = True
        elif '_sort' in query_params:
            sort_column = query_params['_sort'][0]

        if sort_column is not None:
            if sort_column == '' or sort_column not in column_index:
                self.send_error_response(400, f'Unknown column: {sort_column}')
                return
            col_idx = column_index[sort_column]
            indexed_rows = list(enumerate(rows))
            indexed_rows.sort(key=lambda x: x[1][col_idx], reverse=sort_descending)
            sort_order = [orig_idx for orig_idx, _ in indexed_rows]
            rows = [row for _, row in indexed_rows]
        else:
            sort_order = list(range(len(rows)))

        page_start = offset
        page_end = offset + size
        paginated_rows = rows[page_start:page_end]
        page_orig_indices = sort_order[page_start:page_end]

        if shape == 'objects':
            result_rows = []
            for i, row in enumerate(paginated_rows):
                obj = {}
                if not hide_rowid:
                    obj['rowid'] = page_orig_indices[i] + 1
                for col_idx, col_name in enumerate(columns):
                    obj[col_name] = row[col_idx]
                result_rows.append(obj)
        else:
            result_rows = paginated_rows

        t0 = time.time()
        response = {
            'ok': True,
            'columns': columns,
            'rows': result_rows,
            'query_ms': (time.time() - t0) * 1000,
        }

        if not hide_total:
            response['total'] = total

        self.send_json_response(200, response)

    def _parse_positive_int(self, query_params: dict, param_name: str, default: int) -> int | None:
        if param_name not in query_params:
            return default
        try:
            val = int(query_params[param_name][0])
            if val <= 0:
                self.send_error_response(400, f'{param_name} must be a positive integer')
                return None
            return val
        except ValueError:
            self.send_error_response(400, f'{param_name} must be a positive integer')
            return None

    def _parse_non_negative_int(self, query_params: dict, param_name: str, default: int) -> int | None:
        if param_name not in query_params:
            return default
        try:
            val = int(query_params[param_name][0])
            if val < 0:
                self.send_error_response(400, f'{param_name} must be a non-negative integer')
                return None
            return val
        except ValueError:
            self.send_error_response(400, f'{param_name} must be a non-negative integer')
            return None

    def _parse_hide_flag(self, query_params: dict, param_name: str) -> bool | None:
        if param_name not in query_params:
            return False
        if query_params[param_name][0] != 'hide':
            self.send_error_response(400, f'{param_name} must be "hide"')
            return None
        return True

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
