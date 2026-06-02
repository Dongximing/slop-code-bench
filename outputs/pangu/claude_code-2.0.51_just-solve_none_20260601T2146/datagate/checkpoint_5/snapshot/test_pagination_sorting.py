#!/usr/bin/env python3
"""Test pagination, sorting, and response controls."""

import json
import subprocess
import sys
import threading
import time

import requests

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
        # First, convert the CSV to get a dataset ID
        r = requests.get('http://127.0.0.1:8001/convert?source=http://127.0.0.1:8899/test.csv')
        dataset_id = r.json()['endpoint'].split('/')[-1]

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

        print("\n=== Pagination Tests ===")

        # Test _size parameter
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=5')
        result = r.json()
        check(r.status_code == 200, "_size=5 returns 200")
        check(len(result['rows']) == 5, "_size=5 returns 5 rows")
        check('total' in result, "Response includes total")
        check(result['total'] == 12, "Total is correct (12)")

        # Test _offset parameter
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=3&_offset=5')
        result = r.json()
        check(r.status_code == 200, "_size=3&_offset=5 returns 200")
        check(len(result['rows']) == 3, "_size=3&_offset=5 returns 3 rows")
        check(result['rows'][0] == ['Dave', 22, 'Seattle', 45000], "Offset starts at correct row")

        # Test _size larger than available rows
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=1000')
        result = r.json()
        check(len(result['rows']) == 12, "_size=1000 returns all 12 rows")

        # Test invalid _size (not a number)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=abc')
        check(r.status_code == 400, "Invalid _size (abc) returns 400")
        check(r.json().get('ok') == False, "Invalid _size returns ok=false")

        # Test invalid _size (negative)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=-5')
        check(r.status_code == 400, "Invalid _size (negative) returns 400")

        # Test invalid _size (zero)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=0')
        check(r.status_code == 400, "Invalid _size (zero) returns 400")

        # Test invalid _offset (negative)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_offset=-5')
        check(r.status_code == 400, "Invalid _offset (negative) returns 400")

        # Test invalid _offset (not a number)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_offset=abc')
        check(r.status_code == 400, "Invalid _offset (abc) returns 400")

        # Test offset beyond available rows
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_offset=100')
        result = r.json()
        check(len(result['rows']) == 0, "Offset beyond rows returns empty array")

        print("\n=== Sorting Tests ===")

        # Test _sort ascending (name column)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=name')
        result = r.json()
        check(r.status_code == 200, "_sort=name returns 200")
        check(result['rows'][0][0] == 'Alice', "_sort=name ascending works (Alice first)")
        check(result['rows'][-1][0] == 'John', "_sort=name ascending works (John last)")

        # Test _sort_desc descending (name column)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort_desc=name')
        result = r.json()
        check(r.status_code == 200, "_sort_desc=name returns 200")
        check(result['rows'][0][0] == 'John', "_sort_desc=name descending works (John first)")
        check(result['rows'][-1][0] == 'Alice', "_sort_desc=name descending works (Alice last)")

        # Test _sort_desc wins when both _sort and _sort_desc present
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=name&_sort_desc=name')
        result = r.json()
        check(r.status_code == 200, "Both _sort and _sort_desc present")
        check(result['rows'][0][0] == 'John', "_sort_desc wins (John first)")

        # Test _sort on numeric column (age)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=age')
        result = r.json()
        check(result['rows'][0][1] == 22, "_sort=age ascending works (Dave with age 22 first)")
        check(result['rows'][-1][1] == 44, "_sort=age ascending works (Ivan with age 44 last)")

        # Test _sort_desc on numeric column (age)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort_desc=age')
        result = r.json()
        check(result['rows'][0][1] == 44, "_sort_desc=age descending works (Ivan first)")
        check(result['rows'][-1][1] == 22, "_sort_desc=age descending works (Dave last)")

        # Test invalid _sort (unknown column)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=unknown')
        check(r.status_code == 400, "Unknown column in _sort returns 400")
        check('unknown' in r.json().get('error', '').lower(), "Error mentions unknown column")

        # Test invalid _sort_desc (unknown column)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort_desc=unknown')
        check(r.status_code == 400, "Unknown column in _sort_desc returns 400")

        # Test empty _sort column
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=')
        check(r.status_code == 400, "Empty _sort column returns 400")

        # Test empty _sort_desc column
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort_desc=')
        check(r.status_code == 400, "Empty _sort_desc column returns 400")

        print("\n=== Response Shape Tests ===")

        # Test _shape=lists (default)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=lists')
        result = r.json()
        check(r.status_code == 200, "_shape=lists returns 200")
        check(isinstance(result['rows'][0], list), "_shape=lists returns arrays")
        check('rowid' not in result['rows'][0], "_shape=lists does not include rowid")

        # Test _shape=objects
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=objects')
        result = r.json()
        check(r.status_code == 200, "_shape=objects returns 200")
        check(isinstance(result['rows'][0], dict), "_shape=objects returns objects")
        check('rowid' in result['rows'][0], "_shape=objects includes rowid")
        check('name' in result['rows'][0], "_shape=objects includes columns")

        # Test _shape=objects rowid correctness (1-based)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=objects')
        result = r.json()
        check(result['rows'][0]['rowid'] == 1, "First row has rowid=1")
        check(result['rows'][11]['rowid'] == 12, "Last row has rowid=12")

        # Test invalid _shape
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=invalid')
        check(r.status_code == 400, "Invalid _shape returns 400")

        print("\n=== Visibility Toggle Tests ===")

        # Test _rowid=hide with objects shape
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=objects&_rowid=hide')
        result = r.json()
        check(r.status_code == 200, "_rowid=hide with objects returns 200")
        check('rowid' not in result['rows'][0], "_rowid=hide removes rowid")
        check('name' in result['rows'][0], "Columns still present")

        # Test invalid _rowid value
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_rowid=show')
        check(r.status_code == 400, "Invalid _rowid value returns 400")

        # Test _total=hide
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_total=hide')
        result = r.json()
        check(r.status_code == 200, "_total=hide returns 200")
        check('total' not in result, "_total=hide removes total")

        # Test invalid _total value
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_total=show')
        check(r.status_code == 400, "Invalid _total value returns 400")

        print("\n=== Repeated Parameter Tests ===")

        # Test repeated _size parameter
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_size=5&_size=10')
        check(r.status_code == 400, "Repeated _size returns 400")
        check('repeated' in r.json().get('error', '').lower(), "Error mentions repeated parameter")

        # Test repeated _offset parameter
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_offset=5&_offset=10')
        check(r.status_code == 400, "Repeated _offset returns 400")

        # Test repeated _shape parameter
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_shape=lists&_shape=objects')
        check(r.status_code == 400, "Repeated _shape returns 400")

        print("\n=== Combined Parameters Tests ===")

        # Test sorting + pagination
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=name&_size=3&_offset=2')
        result = r.json()
        check(r.status_code == 200, "Combined _sort + _size + _offset returns 200")
        check(len(result['rows']) == 3, "Combined pagination works")
        check(result['rows'][0][0] == 'Charlie', "Sorted + paginated works (Charlie first of 3)")

        # Test sorting + pagination + objects shape
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=name&_size=2&_offset=1&_shape=objects')
        result = r.json()
        check(r.status_code == 200, "Combined all parameters returns 200")
        check(len(result['rows']) == 2, "Combined all works")
        check(result['rows'][0]['name'] == 'Bob', "Object shape with sort+paginate works")

        # Test sorting + pagination + total=hide
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=age&_size=5&_total=hide')
        result = r.json()
        check(r.status_code == 200, "Combined with _total=hide returns 200")
        check('total' not in result, "Total hidden with _total=hide")

        # Test sorting + pagination + rowid=hide (with objects)
        r = requests.get(f'http://127.0.0.1:8001/datasets/{dataset_id}?_sort=name&_size=2&_shape=objects&_rowid=hide')
        result = r.json()
        check(r.status_code == 200, "Combined with _rowid=hide returns 200")
        check('rowid' not in result['rows'][0], "Rowid hidden with _rowid=hide")

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
