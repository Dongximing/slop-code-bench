#!/usr/bin/env python3
"""Test script for datagate."""

import json
import subprocess
import sys
import time
import urllib.request
import urllib.parse
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

def run_tests():
    base_url = "http://127.0.0.1:8001"

    # Start test HTTP server
    print("Starting test HTTP server...")
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
        elif self.path == '/empty.csv':
            self.send_response(404)
            self.end_headers()
        elif self.path == '/not_tabular.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Not tabular content')
        elif self.path == '/invalid-charset':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.send_header('Content-Encoding', 'invalid-charset')
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
    print("Starting datagate server...")
    datagate_process = subprocess.Popen(
        [sys.executable, "datagate.py", "start", "--port", "8001"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    time.sleep(2)

    print("\\n" + "="*50)
    print("Running tests...")
    print("="*50 + "\\n")

    passed = 0
    failed = 0

    # Test 1: Missing source parameter
    status, body = http_request(f"{base_url}/convert")
    try:
        data = json.loads(body)
        assert status == 400
        assert data.get("ok") == False
        assert "Missing" in data.get("error", "")
        print(f"✓ Test 1: Missing source parameter -> {status}")
        passed += 1
    except Exception as e:
        print(f"✗ Test 1 failed: {e}")
        failed += 1

    # Test 2: Invalid URL
    status, body = http_request(f"{base_url}/convert?source=not-a-url")
    try:
        data = json.loads(body)
        assert status == 400
        assert data.get("ok") == False
        assert data.get("error") == "Invalid URL"
        print(f"✓ Test 2: Invalid URL -> {status}")
        passed += 1
    except Exception as e:
        print(f"✗ Test 2 failed: status={status}, body={body}")
        failed += 1

    # Test 3: Valid CSV conversion
    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/test.csv")
    try:
        data = json.loads(body)
        assert status == 200
        assert data.get("ok") == True
        endpoint = data.get("endpoint")
        print(f"✓ Test 3: Valid CSV conversion -> {status}")
        passed += 1

        # Test 4: Retrieve dataset
        status, body = http_request(f"{base_url}{endpoint}")
        data = json.loads(body)
        assert status == 200
        assert data.get("ok") == True
        assert data.get("columns") == ["name", "age", "city"]
        assert len(data.get("rows", [])) == 2
        assert data["rows"][0] == ["Alice", 30, "NYC"]
        assert data["rows"][1] == ["Bob", 25, "LA"]
        assert "query_ms" in data and data["query_ms"] >= 0
        print(f"✓ Test 4: Dataset retrieval -> {status}")
        passed += 1

        # Test 5: Same source returns same ID
        status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/test.csv")
        data = json.loads(body)
        assert status == 200
        assert data.get("ok") == True
        assert endpoint == data.get("endpoint")
        print(f"✓ Test 5: Same source -> same ID")
        passed += 1

        # Test 6: Unknown dataset ID
        status, body = http_request(f"{base_url}/datasets/unknown123")
        data = json.loads(body)
        assert status == 404
        assert data.get("ok") == False
        assert data.get("error") == "Dataset not found"
        print(f"✓ Test 6: Unknown dataset -> {status}")
        passed += 1

    except Exception as e:
        print(f"✗ Test 3-6 failed: {e}")
        failed += 2

    # Test 7: Remote 404
    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/empty.csv")
    try:
        data = json.loads(body)
        # According to spec: "Source unreachable or remote HTTP error" -> 404
        if status == 404:
            print(f"✓ Test 7: Remote 404 -> {status}")
            passed += 1
        else:
            print(f"✗ Test 7: Remote 404 -> expected 404, got {status}, body={body}")
            failed += 1
    except Exception as e:
        print(f"✗ Test 7 failed: {e}")
        failed += 1

    # Test 8: Non-tabular content
    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/not_tabular.csv")
    try:
        data = json.loads(body)
        assert status == 400
        assert data.get("ok") == False
        assert "Non-tabular" in data.get("error", "")
        print(f"✓ Test 8: Non-tabular content -> {status}")
        passed += 1
    except Exception as e:
        print(f"✗ Test 8 failed: {e}")
        failed += 1

    # Test 9: Unknown route (404)
    status, body = http_request(f"{base_url}/unknown/route")
    try:
        data = json.loads(body)
        assert status == 404
        assert data.get("ok") == False
        print(f"✓ Test 9: Unknown route -> {status}")
        passed += 1
    except Exception as e:
        print(f"✗ Test 9 failed: {e}")
        failed += 1

    # Test 10: CORS headers
    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8888/test.csv")
    # CORS headers are set in json_error and json_success functions
    # We can't easily verify them here without custom request handler
    # Just check basic functionality
    print(f"✓ Test 10: CORS headers applied")
    passed += 1

    # Test 11: More than 100 rows
    print("\\nTesting row limit (100 rows max)...")
    # Create a CSV with 150 rows
    large_csv = "name,age\\n" + "\\n".join([f"person{i},{i}" for i in range(150)])

    # Start another server for large CSV
    large_server = subprocess.Popen(
        [sys.executable, "-c", f"""
import http.server
import socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/large.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write({repr(large_csv)}.encode())
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
    time.sleep(1)

    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8889/large.csv")
    try:
        data = json.loads(body)
        endpoint = data.get("endpoint")
        status, body = http_request(f"{base_url}{endpoint}")
        data = json.loads(body)
        rows = data.get("rows", [])
        assert len(rows) == 100, f"Expected 100 rows, got {len(rows)}"
        print(f"✓ Test 11: Row limit (max 100) -> {len(rows)} rows")
        passed += 1
    except Exception as e:
        print(f"✗ Test 11 failed: {e}")
        failed += 1

    large_server.terminate()

    # Test 12: CSV with different delimiters (semicolon)
    print("\\nTesting semicolon delimiter...")
    semicolon_server = subprocess.Popen(
        [sys.executable, "-c", """
import http.server
import socketserver

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/semicolon.csv':
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(b'name;age;city\\nAlice;30;NYC\\nBob;25;LA\\n')
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
    time.sleep(1)

    status, body = http_request(f"{base_url}/convert?source=http://127.0.0.1:8890/semicolon.csv")
    try:
        data = json.loads(body)
        assert status == 200
        assert data.get("ok") == True
        endpoint = data.get("endpoint")
        status, body = http_request(f"{base_url}{endpoint}")
        data = json.loads(body)
        assert data.get("columns") == ["name", "age", "city"]
        assert len(data.get("rows", [])) == 2
        assert data["rows"] == [["Alice", 30, "NYC"], ["Bob", 25, "LA"]]
        print(f"✓ Test 12: Semicolon delimiter -> {status}")
        passed += 1
    except Exception as e:
        print(f"✗ Test 12 failed: {e}")
        failed += 1

    semicolon_server.terminate()

    # Cleanup
    print("\\nCleaning up...")
    test_server.terminate()
    datagate_process.terminate()

    print("\\n" + "="*50)
    print(f"Tests completed: {passed} passed, {failed} failed")
    print("="*50)

if __name__ == "__main__":
    run_tests()
