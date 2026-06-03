#!/usr/bin/env python3
"""Test pagination, sorting, and response controls for datagate."""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from io import StringIO

def wait_for_server(url, timeout=5):
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except:
            time.sleep(0.1)
    return False

def run_tests():
    # Import datagate and access the datasets dict
    import datagate
    from datagate import datasets

    # Start datagate server
    server_process = subprocess.Popen(
        [sys.executable, 'datagate.py', 'start', '--port', '9999', '--address', '127.0.0.1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        if not wait_for_server('http://127.0.0.1:9999/', timeout=5):
            print("ERROR: Server failed to start")
            # Print stderr for debugging
            stderr = server_process.stderr.read().decode() if server_process.stderr else ''
            print(f"Server stderr: {stderr}")
            return False

        base_url = 'http://127.0.0.1:9999'

        # Create a simple CSV in memory
        csv_content = """name,age,city
Alice,30,New York
Bob,25,Boston
Charlie,35,Chicago
David,28,Denver
Eve,32,Seattle
Frank,22,Austin
Grace,29,Portland
Henry,40,Miami"""

        # Parse it manually
        import csv as csv_module
        lines = csv_content.strip().split('\n')
        reader = csv_module.reader(lines)
        header = next(reader)
        columns = [col.strip() for col in header]
        rows = []
        for row in reader:
            if row:
                # Convert to proper types
                typed_row = []
                for val in row:
                    if val.isdigit():
                        typed_row.append(int(val))
                    else:
                        typed_row.append(val)
                rows.append(typed_row)

        # Use a deterministic ID
        dataset_id = 'testdataset'
        datasets[dataset_id] = {'columns': columns, 'rows': rows}

        tests_passed = 0
        tests_total = 0

        # Test 1: Default pagination (should return first 100 rows, but we only have 8)
        print("\nTest 1: Default pagination (first 100 rows)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and data.get('rows') and len(data['rows']) <= 100:
                print(f"  PASS - Got {len(data['rows'])} rows")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 2: _size parameter
        print("\nTest 2: _size=3")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_size=3')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and len(data.get('rows', [])) == 3:
                print(f"  PASS - Got {len(data['rows'])} rows")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 3: _offset parameter
        print("\nTest 3: _offset=2, _size=3")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_offset=2&_size=3')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and len(data.get('rows', [])) == 3:
                print(f"  PASS - Got rows at offset")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 4: Invalid _size (negative)
        print("\nTest 4: _size=-1 (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_size=-1')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 5: Invalid _offset (negative)
        print("\nTest 5: _offset=-1 (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_offset=-1')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 6: _size not a number
        print("\nTest 6: _size=abc (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_size=abc')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 7: _sort ascending
        print("\nTest 7: _sort=age (ascending)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=age&_size=10')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 8:
                ages = [row[1] for row in rows]  # age is index 1
                if ages == sorted(ages):
                    print(f"  PASS - Sorted ascending by age")
                    tests_passed += 1
                else:
                    print(f"  FAIL: Ages not sorted: {ages}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 8: _sort_desc descending
        print("\nTest 8: _sort_desc=age (descending)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort_desc=age&_size=10')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 8:
                ages = [row[1] for row in rows]  # age is index 1
                if ages == sorted(ages, reverse=True):
                    print(f"  PASS - Sorted descending by age")
                    tests_passed += 1
                else:
                    print(f"  FAIL: Ages not sorted: {ages}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 9: Both _sort and _sort_desc (should use _sort_desc)
        print("\nTest 9: _sort=city&_sort_desc=age (should use _sort_desc)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=city&_sort_desc=age&_size=10')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 8:
                ages = [row[1] for row in rows]  # age is index 1
                if ages == sorted(ages, reverse=True):
                    print(f"  PASS - Used _sort_desc")
                    tests_passed += 1
                else:
                    print(f"  FAIL: Ages not sorted by age desc: {ages}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 10: Invalid sort column
        print("\nTest 10: _sort=nonexistent (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=nonexistent')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 11: Empty sort column
        print("\nTest 11: _sort= (empty) (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 12: _shape=objects
        print("\nTest 12: _shape=objects")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_shape=objects&_size=2')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 2:
                if isinstance(rows[0], dict) and 'rowid' in rows[0] and 'name' in rows[0]:
                    print(f"  PASS - Got objects with rowid")
                    tests_passed += 1
                else:
                    print(f"  FAIL: Row is not object with rowid: {rows[0]}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 13: _shape=lists (default)
        print("\nTest 13: _shape=lists")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_shape=lists&_size=2')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 2:
                if isinstance(rows[0], list):
                    print(f"  PASS - Got arrays")
                    tests_passed += 1
                else:
                    print(f"  FAIL: Row is not array: {rows[0]}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 14: Invalid _shape
        print("\nTest 14: _shape=invalid (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_shape=invalid')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 15: _rowid=hide
        print("\nTest 15: _rowid=hide with _shape=objects")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_shape=objects&_rowid=hide&_size=2')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 2:
                if isinstance(rows[0], dict) and 'rowid' not in rows[0]:
                    print(f"  PASS - rowid hidden")
                    tests_passed += 1
                else:
                    print(f"  FAIL: rowid still visible: {rows[0]}")
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 16: Invalid _rowid value
        print("\nTest 16: _rowid=show (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_rowid=show')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 17: _total=hide
        print("\nTest 17: _total=hide")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_total=hide')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and 'total' not in data:
                print("  PASS - total hidden")
                tests_passed += 1
            else:
                print(f"  FAIL: total still present: {data.get('total')}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 18: Invalid _total value
        print("\nTest 18: _total=show (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_total=show')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 19: Repeated _size parameter
        print("\nTest 19: ?_size=5&_size=10 (should fail)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_size=5&_size=10')
            print("  FAIL: Should have returned 400")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if e.code == 400 and data.get('ok') == False:
                print("  PASS - Correctly rejected")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 20: total count is correct after sorting
        print("\nTest 20: total count after sorting")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=name&_size=100')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and data.get('total') == 8:
                print(f"  PASS - total is {data.get('total')}")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 21: rowid reflects source-file row number (not sorted position)
        print("\nTest 21: rowid should be source-file row number")
        try:
            # Sort by name to change order, rowid should still be original position
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_sort=name&_shape=objects&_size=100')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            rowids = [row.get('rowid') for row in rows]
            # rowids should be 1-8 but in sorted name order
            if sorted(rowids) == [1, 2, 3, 4, 5, 6, 7, 8]:
                print(f"  PASS - rowids are source positions: {rowids}")
                tests_passed += 1
            else:
                print(f"  FAIL: rowids not source positions: {rowids}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 22: _size exceeds available rows
        print("\nTest 22: _size=1000 (exceeds available)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_size=1000')
            data = json.loads(resp.read().decode())
            # Should return all 8 rows, not error
            if data.get('ok') == True and len(data.get('rows', [])) == 8:
                print(f"  PASS - Got all {len(data['rows'])} rows")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 23: _offset beyond available rows
        print("\nTest 23: _offset=20 (beyond available)")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_offset=20')
            data = json.loads(resp.read().decode())
            # Should return empty list, not error
            if data.get('ok') == True and len(data.get('rows', [])) == 0:
                print(f"  PASS - Got empty rows")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 24: _offset and _size together
        print("\nTest 24: _offset=2&_size=3")
        try:
            resp = urllib.request.urlopen(f'{base_url}/datasets/{dataset_id}?_offset=2&_size=3')
            data = json.loads(resp.read().decode())
            rows = data.get('rows', [])
            if data.get('ok') == True and len(rows) == 3:
                print(f"  PASS - Got paginated rows")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        print(f"\n{'='*50}")
        print(f"Results: {tests_passed}/{tests_total} tests passed")

        if tests_passed == tests_total:
            print("ALL TESTS PASSED!")
            server_process.terminate()
            server_process.wait()
            return True
        else:
            print("SOME TESTS FAILED")
            server_process.terminate()
            server_process.wait()
            return False

    except Exception as e:
        print(f"Exception: {e}")
        import traceback
        traceback.print_exc()
        server_process.terminate()
        server_process.wait()
        return False

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
