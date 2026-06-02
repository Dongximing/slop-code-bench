#!/usr/bin/env python3
"""Test column-level filtering functionality with a proper single dataset setup."""

import sys
sys.path.insert(0, '.')

from datagate import app
import json

app.config['TESTING'] = True
client = app.test_client()

csv_content = """name,age,city,salary
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

from unittest.mock import patch, MagicMock

# Setup: Create ONE dataset to use for all tests
with patch('datagate.requests.get') as mock_get:
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = csv_content.encode('utf-8')
    mock_get.return_value = mock_response
    response = client.get('/convert?source=http://example.com/test.csv')
    global dataset_id
    dataset_id = response.get_json()['endpoint'].split('/')[-1]

tests_passed = 0
tests_failed = 0

def check(condition, test_name):
    global tests_passed, tests_failed
    if condition:
        print(f"✓ {test_name}")
        tests_passed += 1
    else:
        print(f"✗ {test_name}")
        tests_failed += 1

print("=== Testing exact comparator ===")

# exact: case-sensitive string equality
response = client.get(f'/datasets/{dataset_id}?name__exact=John')
data = response.get_json()
check(response.status_code == 200, "exact: valid returns 200")
check(data['ok'] == True, "exact: ok=True")
check(len(data['rows']) == 1, "exact: returns 1 row")
check(data['rows'][0][0] == 'John', "exact: returns John")
check(data['total'] == 1, "exact: total=1")

# exact: case-sensitive (should fail)
response = client.get(f'/datasets/{dataset_id}?name__exact=john')
data = response.get_json()
check(len(data['rows']) == 0, "exact: case-sensitive (no match)")
check(data['total'] == 0, "exact: total=0 for no match")

# exact: no match
response = client.get(f'/datasets/{dataset_id}?name__exact=NonExistent')
data = response.get_json()
check(len(data['rows']) == 0, "exact: no match returns empty")
check(data['total'] == 0, "exact: total=0 for no match")

print("\n=== Testing contains comparator ===")

# contains: case-sensitive substring
response = client.get(f'/datasets/{dataset_id}?city__contains=or')
data = response.get_json()
check(response.status_code == 200, "contains: valid returns 200")
check(data['ok'] == True, "contains: ok=True")
check(len(data['rows']) == 1, "contains: matches Portland only")

# contains: case-sensitive
response = client.get(f'/datasets/{dataset_id}?city__contains=CHICAGO')
data = response.get_json()
check(len(data['rows']) == 0, "contains: case-sensitive (no match)")

# contains: substring in middle
response = client.get(f'/datasets/{dataset_id}?name__contains=ha')
data = response.get_json()
check(len(data['rows']) == 1, "contains: substring 'ha' matches Charlie")
check(data['rows'][0][0] == 'Charlie', "contains: matches Charlie")

print("\n=== Testing less comparator ===")

# less: numeric strict less
response = client.get(f'/datasets/{dataset_id}?age__less=30')
data = response.get_json()
check(response.status_code == 200, "less: valid returns 200")
check(data['ok'] == True, "less: ok=True")
check(len(data['rows']) == 6, "less: age < 30 returns 6 rows")
check(data['total'] == 6, "less: total=6")

# less: non-numeric filter value returns 400
response = client.get(f'/datasets/{dataset_id}?age__less=abc')
check(response.status_code == 400, "less: non-numeric filter returns 400")

data = response.get_json()
check(data['ok'] == False, "less: non-numeric ok=False")

# less: non-numeric stored values not matched
response = client.get(f'/datasets/{dataset_id}?name__less=50')
check(response.status_code == 200, "less: non-numeric column returns 200 (no matches)")
check(len(response.get_json()['rows']) == 0, "less: non-numeric column no matches")

# less: strict less (30 should not be included)
response = client.get(f'/datasets/{dataset_id}?age__less=30')
data = response.get_json()
ages = [row[1] for row in data['rows']]
check(all(age < 30 for age in ages), "less: strict less (30 not included)")
check(30 not in ages, "less: 30 not in results")

print("\n=== Testing greater comparator ===")

# greater: numeric strict greater
response = client.get(f'/datasets/{dataset_id}?salary__greater=55000')
data = response.get_json()
check(response.status_code == 200, "greater: valid returns 200")
check(data['ok'] == True, "greater: ok=True")
# Salary > 55000: 60000.75, 70000, 55000.25, 80000, 58000.50, 57000.10, 65000.50 = 7 rows
check(len(data['rows']) == 7, "greater: salary > 55000 returns 7 rows")
check(data['total'] == 7, "greater: total=7")

# greater: non-numeric filter value returns 400
response = client.get(f'/datasets/{dataset_id}?salary__greater=abc')
check(response.status_code == 400, "greater: non-numeric filter returns 400")

# greater: float value
response = client.get(f'/datasets/{dataset_id}?salary__greater=55000.00')
data = response.get_json()
# Greater than 55000.00 (not >=): 60000.75, 70000, 55000.25, 80000, 58000.50, 57000.10, 65000.50 = 7 rows
check(len(data['rows']) == 7, "greater: float comparison works")

print("\n=== Testing multiple filters (AND) ===")

# AND: multiple filters
response = client.get(f'/datasets/{dataset_id}?age__greater=25&age__less=35')
data = response.get_json()
check(response.status_code == 200, "multiple: AND returns 200")
# Age > 25 and < 35: 28, 30, 31, 33, 27, 29, 26 = 7 rows
check(len(data['rows']) == 7, "multiple: AND both conditions returns 7 rows")

# AND: non-matching filters
response = client.get(f'/datasets/{dataset_id}?age__greater=30&name__exact=John')
data = response.get_json()
check(len(data['rows']) == 0, "multiple: AND no match returns empty")

# AND: combining with pagination
response = client.get(f'/datasets/{dataset_id}?age__greater=25&age__less=35&_size=3&_offset=2')
data = response.get_json()
check(len(data['rows']) == 3, "multiple: pagination works with filters")
check(data['total'] == 7, "multiple: total is before pagination")

# AND: combining with sorting
response = client.get(f'/datasets/{dataset_id}?age__greater=25&_sort=age')
data = response.get_json()
ages = [row[1] for row in data['rows']]
check(ages == sorted(ages), "multiple: sorting after filtering works")
check(len(data['rows']) == 10, "multiple: sorted filtered results")

print("\n=== Testing error conditions ===")

# Invalid comparator
response = client.get(f'/datasets/{dataset_id}?name__invalid=test')
check(response.status_code == 400, "error: invalid comparator returns 400")
data = response.get_json()
check(data['ok'] == False, "error: invalid comparator ok=False")
check('Invalid comparator' in data['error'] or 'invalid comparator' in data['error'].lower(), "error: mentions invalid comparator")

# Unknown filter column
response = client.get(f'/datasets/{dataset_id}?nonexistent__exact=test')
check(response.status_code == 400, "error: unknown column returns 400")
data = response.get_json()
check(data['ok'] == False, "error: unknown column ok=False")
check('Unknown filter column' in data['error'] or 'unknown' in data['error'].lower(), "error: mentions unknown column")

# Duplicate filter key
response = client.get(f'/datasets/{dataset_id}?name__exact=John&name__exact=Jane')
check(response.status_code == 400, "error: duplicate filter key returns 400")
data = response.get_json()
check(data['ok'] == False, "error: duplicate ok=False")
check('Duplicate filter key' in data['error'] or 'duplicate' in data['error'].lower(), "error: mentions duplicate")

# Comparator target not numeric (greater with string)
response = client.get(f'/datasets/{dataset_id}?name__greater=abc')
check(response.status_code == 400, "error: non-numeric for greater returns 400")
data = response.get_json()
check(data['ok'] == False, "error: non-numeric ok=False")
check('Comparator target not numeric' in data['error'] or 'numeric' in data['error'].lower(), "error: mentions numeric")

# Comparator target not numeric (less with string)
response = client.get(f'/datasets/{dataset_id}?city__less=abc')
check(response.status_code == 400, "error: non-numeric for less returns 400")
data = response.get_json()
check(data['ok'] == False, "error: less non-numeric ok=False")

print("\n=== Testing filters with _shape=objects ===")
response = client.get(f'/datasets/{dataset_id}?age__greater=30&_shape=objects')
data = response.get_json()
check(response.status_code == 200, "filters+objects: returns 200")
check(isinstance(data['rows'][0], dict), "filters+objects: returns objects")
check('rowid' in data['rows'][0], "filters+objects: has rowid")
check(len(data['rows']) == 5, "filters+objects: returns 5 rows (age > 30)")

print("\n=== Testing filters with _rowid=hide ===")
response = client.get(f'/datasets/{dataset_id}?age__exact=25&_shape=objects&_rowid=hide')
data = response.get_json()
check(response.status_code == 200, "filters+rowid_hide: returns 200")
check('rowid' not in data['rows'][0], "filters+rowid_hide: rowid hidden")

print("\n=== Testing filters with _total=hide ===")
response = client.get(f'/datasets/{dataset_id}?age__greater=30&_total=hide')
data = response.get_json()
check(response.status_code == 200, "filters+total_hide: returns 200")
check('total' not in data, "filters+total_hide: total hidden")

print("\n=== Testing params without __ are ignored as filters ===")
response = client.get(f'/datasets/{dataset_id}?nonfilterparam=value')
data = response.get_json()
check(response.status_code == 200, "non-filter params: ignored (returns 200)")
check(len(data['rows']) == 12, "non-filter params: all rows returned")

print(f"\n{'='*50}")
print(f"Tests passed: {tests_passed}, Tests failed: {tests_failed}")
print(f"{'='*50}")

sys.exit(1 if tests_failed > 0 else 0)
