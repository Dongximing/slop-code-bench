#!/usr/bin/env python3
"""Mock primary store server for testing."""

import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class PrimaryStoreHandler(BaseHTTPRequestHandler):
    """Handler for primary store requests."""

    # Shared state
    data = {}
    events = []
    current_version = 0
    lock = threading.Lock()

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/v1/primary/kv':
            # Seed lookup
            params = parse_qs(parsed.query)
            key = params.get('key', [None])[0]

            if key:
                with self.lock:
                    if key in self.data:
                        response = {'found': True, 'value': self.data[key]}
                    else:
                        response = {'found': False}

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode())
            else:
                self.send_response(400)
                self.end_headers()

        elif parsed.path == '/v1/primary/watch':
            # Watch endpoint
            params = parse_qs(parsed.query)
            cursor = int(params.get('cursor', [0])[0])
            keys = params.get('key', [])

            with self.lock:
                # Get events after cursor
                events_to_send = []
                for event in self.events:
                    if event['version'] > cursor:
                        events_to_send.append(event)

                # Update cursor to latest version
                new_cursor = self.current_version

                response = {
                    'cursor': new_cursor,
                    'events': events_to_send
                }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/v1/primary/update':
            # Update a key (for testing)
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)

            key = data.get('key')
            value = data.get('value')

            if key and value:
                with self.lock:
                    self.current_version += 1
                    self.data[key] = value
                    self.events.append({
                        'key': key,
                        'value': value,
                        'version': self.current_version
                    })

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True}).encode())
            else:
                self.send_response(400)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()


def run_server(port=8500):
    """Run the mock primary store server."""
    server = HTTPServer(('localhost', port), PrimaryStoreHandler)
    print(f"Primary store server running on port {port}")
    server.serve_forever()


if __name__ == '__main__':
    run_server()