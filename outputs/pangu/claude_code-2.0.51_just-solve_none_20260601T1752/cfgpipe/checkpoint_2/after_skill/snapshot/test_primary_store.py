#!/usr/bin/env python3
"""Mock primary store server for testing."""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from urllib.parse import urlparse, parse_qs

# Mock data store
STORE = {
    "config/port": "9090",
    "config/debug": "true",
    "app/timeout": "2.5",
}


class PrimaryStoreHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/v1/primary/kv?"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            key = params.get("key", [None])[0]

            if key is None:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"found": False}).encode())
                return

            if key in STORE:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({
                    "found": True,
                    "value": STORE[key],
                    "version": 1
                }).encode())
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"found": False}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8500), PrimaryStoreHandler)
    print("Mock primary store server running on http://127.0.0.1:8500")
    server.serve_forever()
