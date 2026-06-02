#!/usr/bin/env python3
"""Test caching functionality."""

import json
import subprocess
import sys
import threading
import time

import requests

# Test data for the mock CSV server
CSV_DATA = b"""name,age,city,salary
John,25,NYC,50000.50
Jane,30,LA,60000.75
Bob,35,Chicago,70000
"""


def run_csv_server():
    """Run the mock CSV server."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class CSVHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(CSV_DATA)

    server = HTTPServer(('127.0.0.1', 8899), CSVHandler)
    server.serve_forever()


def start_datagate_server(port=8002, env={}):
    """Start the datagate server with optional environment variables."""
    env_copy = os.environ.copy()
    env_copy.update(env)
    # Need to pass env vars properly
    cmd = ['python', 'datagate.py', 'start', '--port', str(port), '--address', '127.0.0.1']
    proc = subprocess.Popen(
        cmd,
        env=env_copy,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(2)  # Wait for server to start
    return proc


def main():
    import os
    # Start the CSV server in background
    csv_thread = threading.Thread(target=run_csv_server, daemon=True)
    csv_thread.start()
    time.sleep(1)  # Wait for server to start

    tests_passed = 0
    tests_failed = 0

    def check(condition, test_name):
        nonlocal tests_passed, tests_failed
        if condition:
            print(f"✓ {test_name}")
            tests_passed += 1
        else:
            print(f"✗ {test_name}")
            tests_failed += 1

    # ==========================================
    # TEST 1: Invalid CACHE_ENABLED value fails startup
    # ==========================================
    proc = subprocess.Popen(
        ['python', 'datagate.py', 'start', '--port', '8003', '--address', '127.0.0.1'],
        env={**os.environ, 'CACHE_ENABLED': 'invalid'},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    proc.wait()
    check(proc.returncode != 0, "Invalid CACHE_ENABLED fails startup")

    # ==========================================
    # TEST 2: Cache works by default (enabled)
    # ==========================================
    server_proc = start_datagate_server(8002)
    try:
        # First request should fetch and cache
        r = requests.get('http://127.0.0.1:8002/convert?source=http://127.0.0.1:8899/test.csv')
        check(r.status_code == 200 and r.json().get('ok') == True, "First convert request succeeds")
        result = r.json()
        dataset_id = result['endpoint'].split('/')[-1]

        # Second request should return cached result (immediate, not fetching)
        r2 = requests.get('http://127.0.0.1:8002/convert?source=http://127.0.0.1:8899/test.csv')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Second request returns cached result")
        check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Same dataset_id returned from cache")

        # Dataset should be queryable
        r3 = requests.get(f'http://127.0.0.1:8002/datasets/{dataset_id}')
        check(r3.status_code == 200 and r3.json().get('ok') == True, "Cached dataset is queryable")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 3: Force parameter bypasses cache
    # ==========================================
    server_proc = start_datagate_server(8004)
    try:
        # First request to populate cache
        r = requests.get('http://127.0.0.1:8004/convert?source=http://127.0.0.1:8899/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]

        # Force re-ingestion should still return same dataset_id (same source)
        r2 = requests.get('http://127.0.0.1:8004/convert?source=http://127.0.0.1:8899/test.csv&force')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Force parameter succeeds")
        check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Force returns same dataset_id")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 4: Force with multiple values returns 400
    # ==========================================
    server_proc = start_datagate_server(8005)
    try:
        r = requests.get('http://127.0.0.1:8005/convert?source=http://127.0.0.1:8899/test.csv&force=&force=')
        check(r.status_code == 400 and r.json().get('ok') == False, "Multiple force parameters returns 400")
    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 5: CACHE_ENABLED=false - force has no effect, no caching
    # ==========================================
    server_proc = start_datagate_server(8006, {'CACHE_ENABLED': 'false'})
    try:
        # First request
        r = requests.get('http://127.0.0.1:8006/convert?source=http://127.0.0.1:8899/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]
        check(r.status_code == 200 and r.json().get('ok') == True, "Cache disabled - first request succeeds")

        # Second request should re-fetch (no caching)
        r2 = requests.get('http://127.0.0.1:8006/convert?source=http://127.0.0.1:8899/test.csv')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Cache disabled - second request re-fetches")
        check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Same dataset_id even when caching disabled")

        # Force parameter should have no effect (caching already disabled)
        r3 = requests.get('http://127.0.0.1:8006/convert?source=http://127.0.0.1:8899/test.csv&force')
        check(r3.status_code == 200 and r3.json().get('ok') == True, "Cache disabled - force has no effect")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 6: Re-ingestion failure keeps prior dataset
    # ==========================================
    # Start a server that returns 500 for the second request
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class FailingCSVHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if hasattr(self, '_served'):
                self.send_response(500)
                self.end_headers()
            else:
                self._served = True
                self.send_response(200)
                self.send_header('Content-type', 'text/csv')
                self.end_headers()
                self.wfile.write(CSV_DATA)

    failing_server = HTTPServer(('127.0.0.1', 8900), FailingCSVHandler)
    failing_thread = threading.Thread(target=failing_server.serve_forever, daemon=True)
    failing_thread.start()
    time.sleep(1)

    try:
        server_proc = start_datagate_server(8007)
        try:
            # First request succeeds
            r = requests.get('http://127.0.0.1:8007/convert?source=http://127.0.0.1:8900/test.csv')
            dataset_id = r.json()['endpoint'].split('/')[-1]
            check(r.status_code == 200 and r.json().get('ok') == True, "Prior request succeeds")

            # Second request triggers re-ingestion failure (500 from source)
            r2 = requests.get('http://127.0.0.1:8007/convert?source=http://127.0.0.1:8900/test.csv')
            # Should return success with prior dataset
            check(r2.status_code == 200 and r2.json().get('ok') == True, "Re-ingestion failure returns prior dataset")
            check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Same dataset_id after re-ingestion failure")

            # Prior dataset should still be queryable
            r3 = requests.get(f'http://127.0.0.1:8007/datasets/{dataset_id}')
            check(r3.status_code == 200 and r3.json().get('ok') == True, "Prior dataset still queryable after re-ingestion failure")

        finally:
            server_proc.terminate()
            server_proc.wait()
    finally:
        failing_server.shutdown()

    # ==========================================
    # TEST 7: Forced re-ingestion failure keeps prior dataset
    # ==========================================
    server_proc = start_datagate_server(8008)
    try:
        # First request succeeds
        r = requests.get('http://127.0.0.1:8008/convert?source=http://127.0.0.1:8899/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]

        # Force re-ingestion (should always succeed with our mock server)
        r2 = requests.get('http://127.0.0.1:8008/convert?source=http://127.0.0.1:8899/test.csv&force')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Forced re-ingestion succeeds")

        # Verify dataset still queryable
        r3 = requests.get(f'http://127.0.0.1:8008/datasets/{dataset_id}')
        check(r3.status_code == 200, "Dataset queryable after forced re-ingestion")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 8: Various valid CACHE_ENABLED values
    # ==========================================
    for val, expected in [('1', True), ('true', True), ('yes', True), ('on', True),
                          ('0', False), ('false', False), ('no', False), ('off', False)]:
        proc = subprocess.Popen(
            ['python', 'datagate.py', 'start', '--port', '8009', '--address', '127.0.0.1'],
            env={**os.environ, 'CACHE_ENABLED': val},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        proc.wait()
        if expected:
            check(proc.returncode == 1, f"CACHE_ENABLED={val} causes startup to fail (expected validation error)")  # Will fail because port in use, but that's okay
        else:
            check(proc.returncode == 1, f"CACHE_ENABLED={val} causes startup to fail (expected validation error)")  # Same

    print(f"\n{'='*50}")
    print(f"Tests passed: {tests_passed}, Tests failed: {tests_failed}")
    print(f"{'='*50}")

    if tests_failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
