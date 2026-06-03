#!/usr/bin/env python3
"""Test script for filter implementation."""

import json
import sys
import time
import urllib.error
import urllib.request

# Add venv to path
sys.path.insert(0, '/workspace/venv/lib/python3.12/site-packages')

# Import and setup
from datagate import datasets

# Create test dataset
dataset_id = 'testfilter'
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
        typed_row = []
        for val in row:
            if val.isdigit():
                typed_row.append(int(val))
            else:
                typed_row.append(val)
        rows.append(typed_row)

datasets[dataset_id] = {'columns': columns, 'rows': rows}

# Start Flask server for testing
from datagate import app
import threading

def run_server():
    app.run(port=9998, host='127.0.0.1', threaded=True, use_reloader=False)

server_thread = threading.Thread(target=run_server, daemon=True)
server_thread.start()

time.sleep(2)  # Wait for server to start

base_url = 'http://127.0.0.1:9998'
tests_passed = 0
tests_total = 0

def make_request(url):
    try:
        resp = urllib.request.urlopen(url)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode())
        return {'error_code': e.code, 'data': data}

# Test 1: exact filter
print("Test 1: name__exact=Alice")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__exact=Alice')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 1 and resp['rows'][0][0] == 'Alice':
        print("  PASS")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 2: exact filter (case-sensitive)
print("Test 2: name__exact=alice (case-sensitive, no match)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__exact=alice')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 0:
        print("  PASS - No rows matched (correct case sensitivity)")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 3: contains filter
print("Test 3: city__contains=York")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?city__contains=York')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 1 and resp['rows'][0][2] == 'New York':
        print("  PASS")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 4: contains filter (case-sensitive)
print("Test 4: city__contains=york (case-sensitive, no match)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?city__contains=york')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 0:
        print("  PASS - No rows matched (correct case sensitivity)")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 5: less filter
print("Test 5: age__less=30")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__less=30')
    if resp.get('ok') == True:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age < 30 for age in ages):
            print("  PASS - All ages less than 30")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not all < 30: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 6: greater filter
print("Test 6: age__greater=30")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=30')
    if resp.get('ok') == True:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age > 30 for age in ages):
            print("  PASS - All ages greater than 30")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not all > 30: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 7: multiple filters (ANDed)
print("Test 7: age__greater=25 and age__less=35")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=25&age__less=35')
    if resp.get('ok') == True:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(25 < age < 35 for age in ages):
            print(f"  PASS - Got {len(ages)} rows with ages between 25 and 35")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not in range: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 8: filter with pagination
print("Test 8: age__greater=25 with _size=3")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=25&_size=3')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 3:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age > 25 for age in ages):
            print(f"  PASS - Got paginated rows")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not > 25: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 9: filter with sorting
print("Test 9: name__exact=Alice with _sort=age")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__exact=Alice&_sort=age')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 1:
        print("  PASS - Filter applied before sort")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 10: invalid comparator
print("Test 10: name__invalid=Alice (should fail)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__invalid=Alice')
    if resp.get('error_code') == 400 and 'Invalid comparator' in resp.get('data', {}).get('error', ''):
        print("  PASS - Correctly rejected")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 11: comparator target not numeric
print("Test 11: name__less=value (should fail)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__less=value')
    if resp.get('error_code') == 400 and 'Comparator target not numeric' in resp.get('data', {}).get('error', ''):
        print("  PASS - Correctly rejected")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 12: unknown filter column
print("Test 12: nonexistent__exact=value (should fail)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?nonexistent__exact=value')
    if resp.get('error_code') == 400 and 'Unknown filter column' in resp.get('data', {}).get('error', ''):
        print("  PASS - Correctly rejected")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 13: duplicate filter key
print("Test 13: ?name__exact=Alice&name__exact=Bob (should fail)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name__exact=Alice&name__exact=Bob')
    if resp.get('error_code') == 400 and 'Duplicate filter key' in resp.get('data', {}).get('error', ''):
        print("  PASS - Correctly rejected")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 14: filter with non-numeric column using less
print("Test 14: city__less=50 (city is non-numeric)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?city__less=50')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 0:
        print("  PASS - No rows matched (non-numeric column)")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 15: query timeout
print("Test 15: _timeout parameter (should fail)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?_timeout=1000')
    if resp.get('error_code') == 400 and 'Query timeout' in resp.get('data', {}).get('error', ''):
        print("  PASS - Correctly rejected")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 16: total count after filtering
print("Test 16: total count after filtering")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=30')
    if resp.get('ok') == True and resp.get('total') == 3:  # Eve(32), Charlie(35), Henry(40)
        print(f"  PASS - Total is {resp.get('total')}")
        tests_passed += 1
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 17: filter preserves pagination on filtered results
print("Test 17: age__greater=20 with _offset=2&_size=3")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=20&_offset=2&_size=3')
    if resp.get('ok') == True and len(resp.get('rows', [])) == 3:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age > 20 for age in ages):
            print(f"  PASS - Got paginated filtered rows")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not > 20: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 18: filter + sort interaction
print("Test 18: age__greater=25 with _sort_desc=age (sorted filtered results)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__greater=25&_sort_desc=age')
    if resp.get('ok') == True:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age > 25 for age in ages) and ages == sorted(ages, reverse=True):
            print(f"  PASS - Filtered and sorted correctly")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 19: float comparison for less/greater
print("Test 19: age__less=30.5 (float comparison)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?age__less=30.5')
    if resp.get('ok') == True:
        ages = [row[1] for row in resp.get('rows', [])]
        if all(age < 30.5 for age in ages):
            print(f"  PASS - Float comparison works")
            tests_passed += 1
        else:
            print(f"  FAIL: Ages not < 30.5: {ages}")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

# Test 20: params without __ are ignored (not treated as filters)
print("Test 20: name=Alice (no __, should be ignored)")
try:
    resp = make_request(f'{base_url}/datasets/{dataset_id}?name=Alice')
    if resp.get('ok') == True:
        # Should return all rows since name=Alice is ignored
        if len(resp.get('rows', [])) == 8:
            print("  PASS - Parameter without __ ignored (not a filter)")
            tests_passed += 1
        else:
            print(f"  FAIL: Got {len(resp.get('rows', []))} rows instead of 8")
    else:
        print(f"  FAIL: {resp}")
except Exception as e:
    print(f"  FAIL: {e}")
tests_total += 1

print(f"\n{'='*50}")
print(f"Results: {tests_passed}/{tests_total} tests passed")

if tests_passed == tests_total:
    print("ALL TESTS PASSED!")
    sys.exit(0)
else:
    print("SOME TESTS FAILED")
    sys.exit(1)
