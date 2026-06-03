#!/usr/bin/env python3
"""Simple mock server for testing cfgpipe store functionality."""
import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Global state
primary_store = {}
primary_versions = {}
secondary_store = {}
primary_cursor = 0
primary_events = []
lock = threading.Lock()

class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress logging
    
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        
        if parsed.path == '/v1/primary/kv':
            key = query.get('key', [None])[0]
            with lock:
                if key in primary_store:
                    self._send_json({'found': True, 'value': primary_store[key], 'version': primary_versions.get(key, 0)})
                else:
                    self._send_json({'found': False})
        
        elif parsed.path == '/v1/primary/watch':
            cursor = int(query.get('cursor', [0])[0])
            keys = query.get('key', [])
            with lock:
                events_to_send = [e for e in primary_events if e['version'] > cursor] if cursor > 0 else []
                new_cursor = primary_cursor if primary_cursor > cursor else cursor
                self._send_json({'cursor': new_cursor, 'events': events_to_send})
        
        elif parsed.path == '/v1/secondary/kv':
            key = query.get('key', [None])[0]
            with lock:
                if key in secondary_store:
                    self._send_json({'found': True, 'value': secondary_store[key]})
                else:
                    self._send_json({'found': False})
        else:
            self.send_error(404)
    
    def do_POST(self):
        parsed = urlparse(self.path)
        
        if parsed.path == '/v1/secondary/batch-read':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            data = json.loads(body)
            keys = data.get('keys', [])
            
            items = []
            with lock:
                for key in keys:
                    if key in secondary_store:
                        items.append({'key': key, 'status': 'ok', 'value': secondary_store[key]})
                    else:
                        items.append({'key': key, 'status': 'missing'})
            self._send_json({'items': items})
        else:
            self.send_error(404)
    
    def _send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

def run_server(port=8500):
    server = HTTPServer(('localhost', port), MockHandler)
    server.serve_forever()

if __name__ == '__main__':
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8500
    print(f"Starting mock server on port {port}", file=sys.stderr)
    run_server(port)
