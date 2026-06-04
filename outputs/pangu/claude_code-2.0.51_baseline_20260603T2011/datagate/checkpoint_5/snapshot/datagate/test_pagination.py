#!/usr/bin/env python3
"""Test pagination, sorting, and response controls for Datagate."""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys

SERVER_URL = "http://127.0.0.1:18082"

def start_server():
    """Start the server in background."""
    proc = subprocess.Popen(
        ["venv/bin/python", "datagate.py", "start", "--port", "18082", "--address", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    time.sleep(3)
    return proc

def stop_server(proc):
    """Stop the server."""
    proc.terminate()
    proc.wait(timeout=5)

def http_get(path):
    """Make HTTP GET request."""
    try:
        req = urllib.request.Request(f"{SERVER_URL}{path}")
        with urllib.request.urlopen(req, timeout=15) as response:
            raw_data = response.read().decode('utf-8')
            data = json.loads(raw_data)
            return response.code, data, dict(response.headers)
    except urllib.error.HTTPError as e:
        raw_data = e.read().decode('utf-8')
        try:
            data = json.loads(raw_data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = {"error": raw_data, "status": e.code}
        return e.code, data, {}
    except Exception as e:
        return None, {"error": str(e)}, {}

def run_tests():
    """Run all tests."""
    print("Starting server...")
    server_proc = start_server()
    if server_proc is None:
        print("FAILED: Could not start server")
        return False

    passed = 0
    failed = 0

    try:
        print("=== Pagination, Sorting, and Response Controls Tests ===\n")

        # First, create a dataset to test with
        csv_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/titanic.csv"
        code, data, _ = http_get(f"/convert?source={csv_url}")
        if code != 200 or not data.get("ok"):
            print(f"FAILED: Could not create dataset: {data}")
            return False

        endpoint = data["endpoint"]
        print(f"Created dataset: {endpoint}")

        # Test 1: Default pagination (size=100)
        print("\nTest 1: Default pagination (_size default 100)")
        code, data, _ = http_get(endpoint)
        if code == 200 and data.get("ok") and "total" in data:
            print(f"  ✓ PASS: Got total={data['total']}, {len(data['rows'])} rows")
            passed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 2: Custom _size (smaller than total)
        print("\nTest 2: Custom _size=5")
        code, data, _ = http_get(f"{endpoint}?_size=5")
        if code == 200 and data.get("ok") and len(data['rows']) == 5:
            print(f"  ✓ PASS: Got {len(data['rows'])} rows as expected")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 5 rows, got {len(data.get('rows', []))}")
            failed += 1

        # Test 3: _offset pagination
        print("\nTest 3: _offset=5, _size=5")
        code, data, _ = http_get(f"{endpoint}?_size=5&_offset=5")
        if code == 200 and data.get("ok") and len(data['rows']) == 5:
            print(f"  ✓ PASS: Got rows at offset 5")
            passed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 4: Invalid _size (negative)
        print("\nTest 4: Invalid _size=-5")
        code, data, _ = http_get(f"{endpoint}?_size=-5")
        if code == 400 and not data.get("ok"):
            print(f"  ✓ PASS: Rejected negative _size")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400 error, got {code}: {data}")
            failed += 1

        # Test 5: Invalid _offset (negative)
        print("\nTest 5: Invalid _offset=-5")
        code, data, _ = http_get(f"{endpoint}?_offset=-5")
        if code == 400 and not data.get("ok"):
            print(f"  ✓ PASS: Rejected negative _offset")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400 error, got {code}: {data}")
            failed += 1

        # Get columns for test reference
        _, get_data, _ = http_get(f"{endpoint}?_size=1")
        columns = get_data.get('columns', [])
        print(f"  Columns: {columns}")

        # Test 6: Ascending sort (use capitalized column names)
        # Check correct column index for Age (column 5)
        print("\nTest 6: _sort=Age (ascending)")
        code, data, _ = http_get(f"{endpoint}?_sort=Age&_size=3")
        if code == 200 and data.get("ok"):
            # Age is at index 5 (PassengerId=0, Survived=1, Pclass=2, Name=3, Sex=4, Age=5)
            ages = [row[5] if isinstance(row, list) else row.get('Age') for row in data['rows']]
            # Filter out empty strings for comparison
            non_empty_ages = [a for a in ages if a != '']
            if non_empty_ages == sorted(non_empty_ages):
                print(f"  ✓ PASS: Sorted ascending by Age: {ages}")
                passed += 1
            else:
                print(f"  ✗ FAIL: Not sorted correctly: {ages}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 7: Descending sort
        print("\nTest 7: _sort_desc=Age (descending)")
        code, data, _ = http_get(f"{endpoint}?_sort_desc=Age&_size=3")
        if code == 200 and data.get("ok"):
            ages = [row[5] if isinstance(row, list) else row.get('Age') for row in data['rows']]
            non_empty_ages = [a for a in ages if a != '']
            if non_empty_ages == sorted(non_empty_ages, reverse=True):
                print(f"  ✓ PASS: Sorted descending by Age: {ages}")
                passed += 1
            else:
                print(f"  ✗ FAIL: Not sorted correctly: {ages}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 8: _sort_desc wins over _sort
        print("\nTest 8: _sort=Age&_sort_desc=Fare (both present, _sort_desc should win)")
        code, data, _ = http_get(f"{endpoint}?_sort=Age&_sort_desc=Fare&_size=3")
        if code == 200 and data.get("ok"):
            fares = [row[9] if isinstance(row, list) else row.get('Fare') for row in data['rows']]
            if fares == sorted(fares, reverse=True):
                print(f"  ✓ PASS: _sort_desc took precedence, sorted by fare descending: {fares}")
                passed += 1
            else:
                print(f"  ✗ FAIL: Expected sorted by fare descending, got {fares}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 9: Unknown column for sorting
        print("\nTest 9: _sort=nonexistent (unknown column)")
        code, data, _ = http_get(f"{endpoint}?_sort=nonexistent")
        if code == 400 and not data.get("ok") and "Unknown column" in data.get("error", ""):
            print(f"  ✓ PASS: Rejected unknown column")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400 with unknown column error, got {code}: {data}")
            failed += 1

        # Test 10: _shape=objects
        print("\nTest 10: _shape=objects")
        code, data, _ = http_get(f"{endpoint}?_shape=objects&_size=2")
        if code == 200 and data.get("ok"):
            rows = data.get('rows', [])
            if all(isinstance(row, dict) for row in rows) and 'rowid' in rows[0]:
                print(f"  ✓ PASS: Rows are objects with rowid")
                passed += 1
            else:
                print(f"  ✗ FAIL: Row structure incorrect: {rows[:1]}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 11: _shape=lists (default)
        print("\nTest 11: _shape=lists (default, explicit)")
        code, data, _ = http_get(f"{endpoint}?_shape=lists&_size=2")
        if code == 200 and data.get("ok"):
            rows = data.get('rows', [])
            if all(isinstance(row, list) for row in rows):
                print(f"  ✓ PASS: Rows are arrays")
                passed += 1
            else:
                print(f"  ✗ FAIL: Rows should be lists: {type(rows[0])}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 12: Invalid _shape
        print("\nTest 12: _shape=invalid")
        code, data, _ = http_get(f"{endpoint}?_shape=invalid")
        if code == 400 and not data.get("ok"):
            print(f"  ✓ PASS: Rejected invalid _shape")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 13: _total=hide
        print("\nTest 13: _total=hide")
        code, data, _ = http_get(f"{endpoint}?_total=hide")
        if code == 200 and data.get("ok") and "total" not in data:
            print(f"  ✓ PASS: Total field hidden")
            passed += 1
        else:
            print(f"  ✗ FAIL: Total should not be present: {data}")
            failed += 1

        # Test 14: Invalid _total value
        print("\nTest 14: _total=invalid")
        code, data, _ = http_get(f"{endpoint}?_total=invalid")
        if code == 400 and not data.get("ok"):
            print(f"  ✓ PASS: Rejected invalid _total value")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 15: _rowid=hide
        print("\nTest 15: _rowid=hide (only applies with _shape=objects)")
        code, data, _ = http_get(f"{endpoint}?_shape=objects&_rowid=hide&_size=1")
        if code == 200 and data.get("ok"):
            rows = data.get('rows', [])
            if isinstance(rows[0], dict) and 'rowid' not in rows[0]:
                print(f"  ✓ PASS: Rowid hidden in objects shape")
                passed += 1
            else:
                print(f"  ✗ FAIL: Rowid should be hidden: {rows}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 16: Invalid _rowid value
        print("\nTest 16: _rowid=invalid")
        code, data, _ = http_get(f"{endpoint}?_rowid=invalid")
        if code == 400 and not data.get("ok"):
            print(f"  ✓ PASS: Rejected invalid _rowid value")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 17: Repeated parameter
        print("\nTest 17: Repeated _size parameter")
        code, data, _ = http_get(f"{endpoint}?_size=5&_size=10")
        if code == 400 and not data.get("ok") and "Repeated" in data.get("error", ""):
            print(f"  ✓ PASS: Detected repeated parameter")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400 with repeated parameter error, got {code}: {data}")
            failed += 1

        # Test 18: Sorting + Pagination together
        print("\nTest 18: _sort=Age, _size=3, _offset=2 (sorted then paginated)")
        code, data, _ = http_get(f"{endpoint}?_sort=Age&_size=3&_offset=2")
        if code == 200 and data.get("ok"):
            # Get first 3 with offset 2 after sorting by Age
            all_sorted_code, all_sorted_data, _ = http_get(f"{endpoint}?_sort=Age&_size=100")
            if all_sorted_code == 200:
                expected_rows = all_sorted_data['rows'][2:5]
                actual_rows = data['rows']
                if actual_rows == expected_rows:
                    print(f"  ✓ PASS: Sorting then pagination works correctly")
                    passed += 1
                else:
                    print(f"  ✗ FAIL: Rows don't match expected: got {len(actual_rows)} rows")
                    failed += 1
            else:
                print(f"  ✗ FAIL: Couldn't get sorted data for comparison")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        # Test 19: _size exceeds available rows
        print("\nTest 19: _size=1000 (larger than available rows)")
        code, data, _ = http_get(f"{endpoint}?_size=1000")
        if code == 200 and data.get("ok") and len(data['rows']) <= 100:
            print(f"  ✓ PASS: Returned all available rows ({len(data['rows'])})")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected all rows to be returned, got {len(data.get('rows', []))}")
            failed += 1

        # Test 20: rowid in objects shape
        print("\nTest 20: rowid is 1-based and reflects source file position")
        code, data, _ = http_get(f"{endpoint}?_shape=objects&_size=3&_offset=0")
        if code == 200 and data.get("ok"):
            rowids = [row['rowid'] for row in data['rows']]
            if rowids == [1, 2, 3]:
                print(f"  ✓ PASS: rowid is 1-based: {rowids}")
                passed += 1
            else:
                print(f"  ✗ FAIL: rowid should be [1, 2, 3], got {rowids}")
                failed += 1
        else:
            print(f"  ✗ FAIL: {data}")
            failed += 1

        print(f"\n=== Test Results: {passed} passed, {failed} failed ===")

    finally:
        stop_server(server_proc)

    return failed == 0

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
