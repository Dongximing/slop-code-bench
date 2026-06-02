#!/usr/bin/env python3
"""Comprehensive test suite for datagate."""

import sys
sys.path.insert(0, '.')

from datagate import app, parse_csv, infer_type, detect_charset, is_valid_url
from unittest.mock import patch, MagicMock
import json

app.config['TESTING'] = True
client = app.test_client()

CSV_DATA = """name,age,city,salary
John,25,NYC,50000.50
Jane,30,LA,60000.75
Bob,35,Chicago,70000
Alice,28,Boston,55000.25
Charlie,40,Miami,80000.00
Dave,22,Seattle,45000"""

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


def setup_mock():
    with patch('datagate.requests.get') as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = CSV_DATA.encode('utf-8')
        mock_get.return_value = mock_response
        yield mock_get


print("=== Testing utility functions ===")

check(infer_type('25') == 25, "infer_type: int string")
check(infer_type('50000.50') == 50000.5, "infer_type: float string")
check(infer_type('John') == 'John', "infer_type: string")
check(infer_type('') == '', "infer_type: empty string")
check(infer_type(None) is None, "infer_type: None")

check(is_valid_url('http://example.com') == True, "is_valid_url: http")
check(is_valid_url('https://example.com') == True, "is_valid_url: https")
check(is_valid_url('not-a-url') == False, "is_valid_url: invalid")
check(is_valid_url('ftp://example.com') == False, "is_valid_url: ftp (invalid scheme)")

cols, rows = parse_csv('name,age\nJohn,25\nJane,30')
check(cols == ['name', 'age'], "parse_csv: columns")
check(rows[0] == ['John', 25], "parse_csv: rows 0")
check(rows[1][1] == 30, "parse_csv: rows 1")

encoding = detect_charset(b'test ascii')
check(encoding == 'ascii' or encoding == 'utf-8', "detect_charset: ascii")

print("\n=== Testing convert endpoint ===")
with setup_mock():
    response = client.get('/convert?source=http://example.com/test.csv')
    check(response.status_code == 200, "convert: valid returns 200")
    data = response.get_json()
    check(data['ok'] == True, "convert: ok=True")
    check('endpoint' in data, "convert: has endpoint")
    dataset_id = data['endpoint'].split('/')[-1]
    print(f"  Dataset ID: {dataset_id}")

    response = client.get('/convert')
    check(response.status_code == 400, "convert: missing source returns 400")
    data = response.get_json()
    check(data['ok'] == False, "convert: missing source ok=False")

    response = client.get('/convert?source=not-a-url')
    check(response.status_code == 400, "convert: invalid URL returns 400")

print("\n=== Testing dataset endpoint pagination ===")
with setup_mock():
    response = client.get('/convert?source=http://example.com/test.csv')
    dataset_id = response.get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}')
    check(response.status_code == 200, "get_dataset: default returns 200")
    data = response.get_json()
    check(len(data['rows']) == 6, "get_dataset: default size=100, max 6 rows")
    check('total' in data, "get_dataset: total present")
    check(data['total'] == 6, "get_dataset: total=6")

    response = client.get(f'/datasets/{dataset_id}?_size=2')
    check(response.status_code == 200, "get_dataset: _size=2 returns 200")
    data = response.get_json()
    check(len(data['rows']) == 2, "get_dataset: _size=2 works")

    response = client.get(f'/datasets/{dataset_id}?_size=0')
    check(response.status_code == 400, "get_dataset: _size=0 returns 400")

    response = client.get(f'/datasets/{dataset_id}?_size=abc')
    check(response.status_code == 400, "get_dataset: _size=abc returns 400")

    response = client.get(f'/datasets/{dataset_id}?_size=1000')
    data = response.get_json()
    check(len(data['rows']) == 6, "get_dataset: _size=1000 returns all rows")

print("\n=== Testing _offset parameter ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test2.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test2.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?_size=2&_offset=2')
    data = response.get_json()
    check(data['rows'][0][0] == 'Bob', "_offset: offset 2 starts at Bob")

    response = client.get(f'/datasets/{dataset_id}?_offset=100')
    data = response.get_json()
    check(len(data['rows']) == 0, "_offset: offset beyond rows returns empty")

print("\n=== Testing sorting parameters ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test3.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test3.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?_sort=name')
    data = response.get_json()
    check(data['rows'][0][0] == 'Alice', "_sort: ascending works (Alice first)")

    response = client.get(f'/datasets/{dataset_id}?_sort_desc=name')
    data = response.get_json()
    check(data['rows'][0][0] == 'John', "_sort_desc: descending works (John first)")

    response = client.get(f'/datasets/{dataset_id}?_sort=name&_sort_desc=name')
    data = response.get_json()
    check(data['rows'][0][0] == 'John', "Both params: _sort_desc wins")

print("\n=== Testing shape parameter ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test4.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test4.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}')
    data = response.get_json()
    check(isinstance(data['rows'][0], list), "_shape=lists: returns lists")

    response = client.get(f'/datasets/{dataset_id}?_shape=objects')
    data = response.get_json()
    check(isinstance(data['rows'][0], dict), "_shape=objects: returns dicts")
    check('rowid' in data['rows'][0], "_shape=objects: has rowid")

    response = client.get(f'/datasets/{dataset_id}?_shape=invalid')
    check(response.status_code == 400, "_shape=invalid: returns 400")

print("\n=== Testing visibility toggles ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test5.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test5.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?_shape=objects&_rowid=hide')
    data = response.get_json()
    check('rowid' not in data['rows'][0], "_rowid=hide: rowid hidden in objects")

    response = client.get(f'/datasets/{dataset_id}?_total=hide')
    data = response.get_json()
    check('total' not in data, "_total=hide: total hidden")

print("\n=== Testing repeated parameter detection ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test6.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test6.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?_size=5&_size=10')
    check(response.status_code == 400, "Repeated _size: returns 400")
    data = response.get_json()
    check('repeated' in data.get('error', '').lower(), "Repeated _size: error mentions 'repeated'")

print("\n=== Testing error handling ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test7.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test7.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?_sort=nonexistent')
    check(response.status_code == 400, "Invalid sort column: returns 400")
    data = response.get_json()
    check('unknown' in data.get('error', '').lower(), "Invalid sort column: error mentions 'unknown'")

    response = client.get(f'/datasets/{dataset_id}?_sort=')
    check(response.status_code == 400, "Empty sort column: returns 400")

print("\n=== Testing filtering (AND) ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?age__greater=25&age__less=35')
    data = response.get_json()
    check(response.status_code == 200, "multiple: AND returns 200")
    check(len(data['rows']) == 7, "multiple: AND both conditions returns 7 rows")

    response = client.get(f'/datasets/{dataset_id}?age__greater=30&name__exact=John')
    data = response.get_json()
    check(len(data['rows']) == 0, "multiple: AND no match returns empty")

    response = client.get(f'/datasets/{dataset_id}?age__greater=25&age__less=35&_size=3&_offset=2')
    data = response.get_json()
    check(len(data['rows']) == 3, "multiple: pagination works with filters")
    check(data['total'] == 7, "multiple: total is before pagination")

print("\n=== Testing error conditions ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?name__invalid=test')
    check(response.status_code == 400, "error: invalid comparator returns 400")

    response = client.get(f'/datasets/{dataset_id}?nonexistent__exact=test')
    check(response.status_code == 400, "error: unknown column returns 400")

    response = client.get(f'/datasets/{dataset_id}?name__exact=John&name__exact=Jane')
    check(response.status_code == 400, "error: duplicate filter key returns 400")

    response = client.get(f'/datasets/{dataset_id}?name__greater=abc')
    check(response.status_code == 400, "error: non-numeric for greater returns 400")

print("\n=== Testing filters with shape and visibility ===")
with setup_mock():
    client.get(f'/convert?source=http://example.com/test.csv')
    dataset_id = client.get(f'/convert?source=http://example.com/test.csv').get_json()['endpoint'].split('/')[-1]

    response = client.get(f'/datasets/{dataset_id}?age__greater=30&_shape=objects')
    check(response.status_code == 200, "filters+objects: returns 200")
    check(isinstance(data['rows'][0], dict), "filters+objects: returns objects")
    check('rowid' in data['rows'][0], "filters+objects: has rowid")

    response = client.get(f'/datasets/{dataset_id}?age__exact=25&_shape=objects&_rowid=hide')
    check(response.status_code == 200, "filters+rowid_hide: returns 200")
    check('rowid' not in data['rows'][0], "filters+rowid_hide: rowid hidden")

    response = client.get(f'/datasets/{dataset_id}?age__greater=30&_total=hide')
    check(response.status_code == 200, "filters+total_hide: returns 200")
    check('total' not in data, "filters+total_hide: total hidden")

print(f"\n{'='*50}")
print(f"Tests passed: {tests_passed}, Tests failed: {tests_failed}")
print(f"{'='*50}")

if tests_failed > 0:
    sys.exit(1)
