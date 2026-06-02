#!/usr/bin/env python3

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
Alice,28,Boston,55000.25
Charlie,40,Miami,80000.00
Dave,22,Seattle,45000
Eve,33,Denver,58000.50
Frank,27,Austin,52000.75
Grace,29,Phoenix,54000.25
Henry,31,Dallas,57000.10
Ivan,44,Portland,65000.50
Judy,26,Sacramento,48000.25"""


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


def main():
    # Start the CSV server in background
    csv_thread = threading.Thread(target=run_csv_server, daemon=True)
    csv_thread.start()
    time.sleep(1)  # Wait for server to start

    # Start the main server
    server_proc = subprocess.Popen(
        ['source venv/bin/activate && python datagate.py start --port 8001 --address 127.0.0.1'],
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(2)  # Wait for server to start

    try:
        # Run tests
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

        # Test 1: Missing source parameter
        r = requests.get('http://127.0.0.1:8001/convert')
        check(r.status_code == 400 and r.json().get('ok') == False, "Missing source returns 400")

        # Test 2: Invalid URL
        r = requests.get('http://127.0.0.1:8001/convert?source=not-a-url')
        check(r.status_code == 400 and r.json().get('ok') == False, "Invalid URL returns 400")

        # Test 3: Unknown dataset
        r = requests.get('http://127.0.0.1:8001/datasets/unknown')
        check(r.status_code == 404 and r.json().get('ok') == False, "Unknown dataset returns 404")

        # Test 4: Convert valid CSV
        r = requests.get('http://127.0.0.1:8001/convert?source=http://127.0.0.1:8899/test.csv')
        check(r.status_code == 200, "Convert valid CSV returns 200")
        result = r.json()
        check(result.get('ok') == True, "Convert returns ok=true")
        check('endpoint' in result, "Convert returns endpoint")

        # Extract dataset ID
        dataset_id = result['endpoint'].split('/')[-1]
        print(f"  Dataset ID: {dataset_id}")

        # Test 5: Get dataset
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}')
        check(r.status_code == 200, "Get dataset returns 200")
        result = r.json()
        check(result.get('ok') == True, "Get dataset returns ok=true")
        check('columns' in result, "Get dataset returns columns")
        check('rows' in result, "Get dataset returns rows")
        check('query_ms' in result, "Get dataset returns query_ms")
        check(result['columns'] == ['name', 'age', 'city', 'salary'], "Columns are correct")

        # Test 6: Type inference
        rows = result['rows']
        check(len(rows) == 12, "Returns all rows when fewer than 100")
        check(rows[0] == ['John', 25, 'NYC', 50000.5], "Types inferred correctly")

        # Test 7: Deterministic ID for same source
        r = requests.get('http://127.0.0.1:8001/convert?source=http://127.0.0.1:8899/test.csv')
        check(r.json()['endpoint'].split('/')[-1] == dataset_id, "Same source gives same ID")

        # Test 8: Unknown route
        r = requests.get('http://127.0.0.1:8001/unknown')
        check(r.status_code == 404, "Unknown route returns 404")

        # Test 9: CORS headers
        r = requests.get('http://127.0.0.1:8001/convert?source=http://127.0.0.1:8899/test.csv')
        check('Access-Control-Allow-Origin' in r.headers, "CORS headers present")

        # Test 10: Non-CSV content detection
        r = requests.get('http://127.0.0.1:8001/convert?source=https://httpbin.org/json')
        check(r.status_code == 400, "Non-tabular content returns 400")

        print(f"\n{'='*50}")
        print(f"Tests passed: {tests_passed}, Tests failed: {tests_failed}")
        print(f"{'='*50}")

        if tests_failed > 0:
            sys.exit(1)

    finally:
        server_proc.terminate()
        server_proc.wait()


if __name__ == '__main__':
    main()
