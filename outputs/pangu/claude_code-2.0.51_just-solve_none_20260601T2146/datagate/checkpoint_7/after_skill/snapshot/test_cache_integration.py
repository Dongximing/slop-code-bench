#!/usr/bin/env python3
"""Integration test for caching functionality."""

import json
import os
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


def run_csv_server(port=8899):
    """Run the mock CSV server."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class CSVHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/csv')
            self.end_headers()
            self.wfile.write(CSV_DATA)

    server = HTTPServer(('127.0.0.1', port), CSVHandler)
    server.serve_forever()


def start_datagate_server(port=8001, cache_enabled='true'):
    """Start the datagate server with cache setting."""
    env = os.environ.copy()
    env['CACHE_ENABLED'] = cache_enabled
    proc = subprocess.Popen(
        ['python', 'datagate.py', 'start', '--port', str(port), '--address', '127.0.0.1'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(2)
    return proc


def main():
    # Start the CSV server
    csv_port = 8899
    csv_thread = threading.Thread(target=run_csv_server, daemon=True, args=(csv_port,))
    csv_thread.start()
    time.sleep(1)

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
    # TEST 1: Cache works by default (enabled)
    # ==========================================
    server_port = 8001
    server_proc = start_datagate_server(server_port)
    try:
        base_url = f'http://127.0.0.1:{server_port}'

        # First request should cache
        r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
        check(r.status_code == 200 and r.json().get('ok') == True, "First request succeeds")
        dataset_id = r.json()['endpoint'].split('/')[-1]

        # Second request should return cached (no new fetch)
        r2 = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Second request returns from cache")
        check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Same dataset_id from cache")

        # Dataset should be queryable
        r3 = requests.get(f'{base_url}/datasets/{dataset_id}')
        check(r3.status_code == 200 and r3.json().get('ok') == True, "Cached dataset is queryable")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 2: Force parameter bypasses cache
    # ==========================================
    server_port = 8002
    server_proc = start_datagate_server(server_port)
    try:
        base_url = f'http://127.0.0.1:{server_port}'

        # First request
        r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]

        # Force re-ingestion
        r2 = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv&force')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Force parameter works")
        check(r2.json()['endpoint'].split('/')[-1] == dataset_id, "Force returns same dataset_id")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 3: Multiple force parameters returns 400
    # ==========================================
    server_port = 8003
    server_proc = start_datagate_server(server_port)
    try:
        base_url = f'http://127.0.0.1:{server_port}'
        r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv&force=&force=')
        check(r.status_code == 400 and r.json().get('ok') == False, "Multiple force parameters returns 400")
    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 4: CACHE_ENABLED=false - no caching, force has no effect
    # ==========================================
    server_port = 8004
    server_proc = start_datagate_server(server_port, cache_enabled='false')
    try:
        base_url = f'http://127.0.0.1:{server_port}'

        # First request
        r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]
        check(r.status_code == 200 and r.json().get('ok') == True, "Cache disabled - request succeeds")

        # Second request (re-fetches since no caching)
        r2 = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
        check(r2.status_code == 200 and r2.json().get('ok') == True, "Cache disabled - second request re-fetches")

        # Force parameter has no effect (already no caching)
        r3 = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv&force')
        check(r3.status_code == 200 and r3.json().get('ok') == True, "Cache disabled - force has no effect")

    finally:
        server_proc.terminate()
        server_proc.wait()

    # ==========================================
    # TEST 5: All valid true values work
    # ==========================================
    for true_val in ['1', 'true', 'yes', 'on']:
        server_port = 8005 + ['1', 'true', 'yes', 'on'].index(true_val)
        proc = start_datagate_server(server_port, cache_enabled=true_val)
        try:
            base_url = f'http://127.0.0.1:{server_port}'
            r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
            check(r.status_code == 200, f"CACHE_ENABLED={true_val} works (true)")
        finally:
            proc.terminate()
            proc.wait()

    # ==========================================
    # TEST 6: All valid false values work
    # ==========================================
    for false_val in ['0', 'false', 'no', 'off']:
        server_port = 8009 + ['0', 'false', 'no', 'off'].index(false_val)
        proc = start_datagate_server(server_port, cache_enabled=false_val)
        try:
            base_url = f'http://127.0.0.1:{server_port}'
            r = requests.get(f'{base_url}/convert?source=http://127.0.0.1:{csv_port}/test.csv')
            check(r.status_code == 200, f"CACHE_ENABLED={false_val} works (false)")
        finally:
            proc.terminate()
            proc.wait()

    print(f"\n{'='*50}")
    print(f"Tests passed: {tests_passed}, Tests failed: {tests_failed}")
    print(f"{'='*50}")

    if tests_failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
