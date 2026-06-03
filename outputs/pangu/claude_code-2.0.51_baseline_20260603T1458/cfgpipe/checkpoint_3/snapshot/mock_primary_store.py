#!/usr/bin/env python3
"""Mock primary store server for testing."""

import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler


class MockPrimaryStoreHandler(BaseHTTPRequestHandler):
    """Handler for mock primary store."""

    # In-memory store
    store = {
        "app/port": "9090",
    }

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        if self.path.startswith('/v1/primary/kv'):
            # Parse query parameters
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            key = params.get('key', [None])[0]
            if key is None:
                self.send_error(400, "Missing key parameter")
                return

            if key in self.store:
                response = {
                    "found": True,
                    "value": self.store[key],
                    "version": 1
                }
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
            else:
                response = {"found": False}
                self.send_response(404)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
        else:
            self.send_error(404, "Not found")


def run_server(port=8500):
    """Run the mock server."""
    server = HTTPServer(('127.0.0.1', port), MockPrimaryStoreHandler)
    print(f"Mock primary store running on http://127.0.0.1:{port}", file=sys.stderr)
    server.serve_forever()


if __name__ == '__main__':
    run_server()
