#!/usr/bin/env python3
"""Test caching implementation."""

import os
import subprocess
import sys
import time
import urllib.request
import urllib.error


def http_request(url):
    """Make HTTP request and return (status_code, response_body)."""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode('utf-8')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8')
    except Exception as e:
        return None, str(e)


def test_cache_enabled_default():
    """Test that caching is enabled by default."""
    print("Testing default cache (should be enabled)...")

    # Start test HTTP server
    test_server = subprocess.Popen(
        [sys.executable, "-c", """
import http.server
import socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/test.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(b'name,age,city\\nAlice,30,NYC\\nBob,25,LA\\n')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

with socketserver.TCPServer(('', 8888), Handler) as httpd:
    httpd.serve_forever()
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    time.sleep(2)

    # Start datagate server
    datagate_process = subprocess.Popen(
        [sys.executable, "datagate.py", "start", "--port", "8001"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)

    base_url = "http://127.0.0.1:8001"

    # First request
    status1, body1 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/test.csv")
    print(f"  First request status: {status1}")

    # Second request - should hit cache
    status2, body2 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/test.csv")
    print(f"  Second request status: {status2}")

    # Both should return 200
    passed = status1 == 200 and status2 == 200

    # Cleanup
    test_server.terminate()
    datagate_process.terminate()

    return passed


def test_cache_enabled_false():
    """Test that setting CACHE_ENABLED=false disables caching."""
    print("Testing CACHE_ENABLED=false...")

    # Start test HTTP server
    test_server = subprocess.Popen(
        [sys.executable, "-c", """
import http.server
import socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/test.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(b'name,age,city\\nAlice,30,NYC\\nBob,25,LA\\n')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

with socketserver.TCPServer(('', 8889), Handler) as httpd:
    httpd.serve_forever()
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    time.sleep(2)

    # Start datagate server with CACHE_ENABLED=false
    env = os.environ.copy()
    env['CACHE_ENABLED'] = 'false'
    datagate_process = subprocess.Popen(
        [sys.executable, "datagate.py", "start", "--port", "8002"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env
    )
    time.sleep(2)

    base_url = "http://127.0.0.1:8002"

    # First request
    status1, body1 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8889/test.csv")
    print(f"  First request status: {status1}")

    # Second request - with cache disabled, should still return 200 (re-downloads)
    status2, body2 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8889/test.csv")
    print(f"  Second request status: {status2}")

    # Both should return 200
    passed = status1 == 200 and status2 == 200

    # Cleanup
    test_server.terminate()
    datagate_process.terminate()

    return passed


def test_force_parameter():
    """Test that force parameter forces re-ingestion."""
    print("Testing force parameter...")

    # Start test HTTP server
    test_server = subprocess.Popen(
        [sys.executable, "-c", """
import http.server
import socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/test.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(b'name,age,city\\nAlice,30,NYC\\nBob,25,LA\\n')
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, *args): pass

with socketserver.TCPServer(('', 8890), Handler) as httpd:
    httpd.serve_forever()
"""],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    time.sleep(2)

    # Start datagate server
    datagate_process = subprocess.Popen(
        [sys.executable, "datagate.py", "start", "--port", "8003"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)

    base_url = "http://127.0.0.1:8003"

    # First request
    status1, body1 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8890/test.csv")
    print(f"  First request status: {status1}")

    # Request with force parameter - should still work (re-ingests)
    status2, body2 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8890/test.csv&force")
    print(f"  Force request status: {status2}")

    # Request with invalid force value - should fail
    status3, body3 = http_request(f"{base_url}/convert?source=http://127.0.0.1:8890/test.csv&force=true")
    print(f"  Invalid force request status: {status3}")

    # All should return 200 except the invalid one
    passed = status1 == 200 and status2 == 200 and status3 == 400

    # Cleanup
    test_server.terminate()
    datagate_process.terminate()

    return passed


def test_invalid_cache_value():
    """Test that invalid CACHE_ENABLED value fails at startup."""
    print("Testing invalid CACHE_ENABLED value...")

    env = os.environ.copy()
    env['CACHE_ENABLED'] = 'invalid_value'

    result = subprocess.run(
        [sys.executable, "datagate.py", "start"],
        capture_output=True,
        text=True,
        env=env
    )

    # Should exit with error
    passed = result.returncode != 0 and "Invalid CACHE_ENABLED" in result.stderr
    print(f"  Exit code: {result.returncode}")
    print(f"  Error message: {result.stderr.strip()}")

    return passed


def main():
    tests = [
        ("Default cache enabled", test_cache_enabled_default),
        ("CACHE_ENABLED=false", test_cache_enabled_false),
        ("Force parameter", test_force_parameter),
        ("Invalid CACHE_ENABLED value", test_invalid_cache_value),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            if test_func():
                print(f"✓ {name}")
                passed += 1
            else:
                print(f"✗ {name}")
                failed += 1
        except Exception as e:
            print(f"✗ {name}: {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
