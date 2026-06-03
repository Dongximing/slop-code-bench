#!/usr/bin/env python3
"""Test script for cfgpipe watch mode."""

import json
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error

# Start the mock primary store server
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


class PrimaryStoreHandler(BaseHTTPRequestHandler):
    """Handler for primary store requests."""

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
            params = parse_qs(parsed.query)
            cursor = int(params.get('cursor', [0])[0])
            keys = params.get('key', [])

            with self.lock:
                events_to_send = []
                for event in self.events:
                    if event['version'] > cursor:
                        events_to_send.append(event)
                new_cursor = self.current_version
                response = {'cursor': new_cursor, 'events': events_to_send}

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


class SecondaryStoreHandler(BaseHTTPRequestHandler):
    """Handler for secondary store requests."""

    data = {}
    lock = threading.Lock()

    def log_message(self, format, *args):
        """Suppress log messages."""
        pass

    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/v1/secondary/kv':
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

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)

        if parsed.path == '/v1/secondary/batch-read':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            request_data = json.loads(body)

            keys = request_data.get('keys', [])
            items = []

            with self.lock:
                for key in keys:
                    if key in self.data:
                        items.append({'key': key, 'status': 'ok', 'value': self.data[key]})
                    else:
                        items.append({'key': key, 'status': 'missing'})

            response = {'items': items}

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        elif parsed.path == '/v1/secondary/update':
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode()
            data = json.loads(body)

            key = data.get('key')
            value = data.get('value')

            if key and value is not None:
                with self.lock:
                    self.data[key] = value

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


def run_primary_store(port=8500):
    """Run the mock primary store server."""
    server = HTTPServer(('localhost', port), PrimaryStoreHandler)
    server.serve_forever()


def run_secondary_store(port=6400):
    """Run the mock secondary store server."""
    server = HTTPServer(('localhost', port), SecondaryStoreHandler)
    server.serve_forever()


def update_primary_store(port, key, value):
    """Update a key in the primary store."""
    url = f"http://localhost:{port}/v1/primary/update"
    data = json.dumps({'key': key, 'value': value}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except:
        return False


def update_secondary_store(port, key, value):
    """Update a key in the secondary store."""
    url = f"http://localhost:{port}/v1/secondary/update"
    data = json.dumps({'key': key, 'value': value}).encode()
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status == 200
    except:
        return False


if __name__ == '__main__':
    import sys

    # Start primary store in background
    primary_thread = threading.Thread(target=run_primary_store, args=(8500,), daemon=True)
    primary_thread.start()

    # Start secondary store in background
    secondary_thread = threading.Thread(target=run_secondary_store, args=(6400,), daemon=True)
    secondary_thread.start()

    time.sleep(0.5)  # Let servers start

    # Initialize primary store data
    PrimaryStoreHandler.data['app/host'] = 'prod.example.com'
    PrimaryStoreHandler.data['app/verbose'] = 'false'
    PrimaryStoreHandler.current_version = 10
    PrimaryStoreHandler.events.append({
        'key': 'app/host',
        'value': 'prod.example.com',
        'version': 10
    })

    # Initialize secondary store data
    SecondaryStoreHandler.data['server_host'] = '10.0.0.5'
    SecondaryStoreHandler.data['server_port'] = '9090'

    print("Mock stores running on ports 8500 (primary) and 6400 (secondary)")
    print("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
