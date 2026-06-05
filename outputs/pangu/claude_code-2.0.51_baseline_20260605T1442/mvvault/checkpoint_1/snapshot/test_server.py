#!/usr/bin/env python3
"""Simple test server for mvault testing."""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler


SOURCE_DATA = {
    "episodes": [
        {
            "id": "ep1",
            "published": "2024-01-15T10:30:00",
            "width": 1920,
            "height": 1080,
            "title": "First Episode",
            "description": "A great episode",
            "views": 1000,
            "likes": 100,
            "preview": "abc123"
        },
        {
            "id": "ep2",
            "published": "2024-01-16T10:30:00",
            "width": 1920,
            "height": 1080,
            "title": "Second Episode",
            "description": "Another episode",
            "views": 500,
            "likes": 50,
            "preview": "def456"
        }
    ],
    "streams": [],
    "clips": []
}


class TestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(SOURCE_DATA).encode())

    def log_message(self, format, *args):
        pass  # Suppress logging


if __name__ == "__main__":
    server = HTTPServer(("localhost", 8888), TestHandler)
    print("Test server running on port 8888")
    server.serve_forever()
