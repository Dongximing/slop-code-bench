#!/usr/bin/env python3
"""Test the pagination, sorting, and response controls for datagate."""

import csv
import json
import os
import sys
import tempfile
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
import threading

import requests

BASE_URL = 'http://127.0.0.1:8001'


def create_test_csv(content, delimiter=','):
    """Create a temporary CSV file and return a file URL."""
    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='') as f:
        writer = csv.writer(f, delimiter=delimiter)
        for row in content:
            writer.writerow(row)

    # Start a temporary HTTP server
    server_fd, server_path = tempfile.mkstemp()
    os.close(server_fd)
    os.unlink(server_path)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=os.path.dirname(path), **kwargs)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()

    host, port = server.server_address
    filename = os.path.basename(path)
    url = f'http://{host}:{port}/{filename}'

    # Give server time to start
    time.sleep(0.5)

    return url, server


def test_pagination_size():
    """Test _size pagination parameter."""
    print("Testing _size pagination...")
    csv_content = [['name', 'value']] + [[f'name{i}', f'val{i}'] for i in range(50)]
    url, server = create_test_csv(csv_content)
    try:
        # Convert the CSV
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test _size=10
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 10})
        assert r.status_code == 200
        data = r.json()
        assert len(data['rows']) == 10
        assert data.get('total') == 50
        print("  PASSED - _size=10")

        # Test _size larger than available rows
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 1000})
        assert r.status_code == 200
        data = r.json()
        assert len(data['rows']) == 50  # All rows returned
        print("  PASSED - _size larger than available rows")
    finally:
        server.shutdown()


def test_pagination_offset():
    """Test _offset pagination parameter."""
    print("Testing _offset pagination...")
    csv_content = [['name', 'value']] + [[f'name{i}', f'val{i}'] for i in range(50)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test _offset=25 with _size=10
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_offset': 25, '_size': 10})
        assert r.status_code == 200
        data = r.json()
        assert len(data['rows']) == 10
        assert data['rows'][0][0] == 'name25'
        assert data['rows'][-1][0] == 'name34'
        print("  PASSED - _offset=25, _size=10")

        # Test _offset at end
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_offset': 45, '_size': 10})
        assert r.status_code == 200
        data = r.json()
        assert len(data['rows']) == 5
        assert data['rows'][0][0] == 'name45'
        print("  PASSED - _offset at end")
    finally:
        server.shutdown()


def test_pagination_size_offset_combined():
    """Test _size and _offset together."""
    print("Testing _size and _offset combined...")
    csv_content = [['num']] + [[str(i)] for i in range(100)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Get pages
        r1 = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 5, '_offset': 0})
        r2 = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 5, '_offset': 5})
        r3 = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 5, '_offset': 10})

        rows1 = r1.json()['rows']
        rows2 = r2.json()['rows']
        rows3 = r3.json()['rows']

        assert rows1[0][0] == '0'
        assert rows1[-1][0] == '4'
        assert rows2[0][0] == '5'
        assert rows2[-1][0] == '9'
        assert rows3[0][0] == '10'
        assert rows3[-1][0] == '14'
        print("  PASSED - page navigation works correctly")
    finally:
        server.shutdown()


def test_invalid_size():
    """Test invalid _size values."""
    print("Testing invalid _size...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test negative _size
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': '-5'})
        assert r.status_code == 400
        assert 'positive integer' in r.json()['error']
        print("  PASSED - negative _size rejected")

        # Test zero _size
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': '0'})
        assert r.status_code == 400
        assert 'positive integer' in r.json()['error']
        print("  PASSED - zero _size rejected")

        # Test non-numeric _size
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 'abc'})
        assert r.status_code == 400
        assert 'positive integer' in r.json()['error']
        print("  PASSED - non-numeric _size rejected")
    finally:
        server.shutdown()


def test_invalid_offset():
    """Test invalid _offset values."""
    print("Testing invalid _offset...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test negative _offset
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_offset': '-5'})
        assert r.status_code == 400
        assert 'non-negative integer' in r.json()['error']
        print("  PASSED - negative _offset rejected")

        # Test non-numeric _offset
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_offset': 'abc'})
        assert r.status_code == 400
        assert 'non-negative integer' in r.json()['error']
        print("  PASSED - non-numeric _offset rejected")
    finally:
        server.shutdown()


def test_sort_ascending():
    """Test _sort ascending parameter."""
    print("Testing _sort ascending...")
    csv_content = [['name', 'age'], ['Alice', 25], ['Bob', 20], ['Charlie', 30]]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Sort by name ascending
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'name'})
        assert r.status_code == 200
        data = r.json()
        assert data['rows'][0][0] == 'Alice'
        assert data['rows'][1][0] == 'Bob'
        assert data['rows'][2][0] == 'Charlie'
        print("  PASSED - sort by name ascending")

        # Sort by age ascending
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'age'})
        assert r.status_code == 200
        data = r.json()
        assert data['rows'][0][1] == 20  # Bob
        assert data['rows'][1][1] == 25  # Alice
        assert data['rows'][2][1] == 30  # Charlie
        print("  PASSED - sort by age ascending")
    finally:
        server.shutdown()


def test_sort_descending():
    """Test _sort_desc descending parameter."""
    print("Testing _sort_desc descending...")
    csv_content = [['name', 'age'], ['Alice', 25], ['Bob', 20], ['Charlie', 30]]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Sort by name descending
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort_desc': 'name'})
        assert r.status_code == 200
        data = r.json()
        assert data['rows'][0][0] == 'Charlie'
        assert data['rows'][1][0] == 'Bob'
        assert data['rows'][2][0] == 'Alice'
        print("  PASSED - sort by name descending")

        # Sort by age descending
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort_desc': 'age'})
        assert r.status_code == 200
        data = r.json()
        assert data['rows'][0][1] == 30
        assert data['rows'][1][1] == 25
        assert data['rows'][2][1] == 20
        print("  PASSED - sort by age descending")
    finally:
        server.shutdown()


def test_sort_desc_wins():
    """Test that _sort_desc wins when both _sort and _sort_desc are present."""
    print("Testing _sort_desc wins when both present...")
    csv_content = [['name'], ['Charlie'], ['Alice'], ['Bob']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Both present, _sort_desc should win
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'name', '_sort_desc': 'name'})
        assert r.status_code == 200
        data = r.json()
        # Should be descending (Charlie, Bob, Alice)
        assert data['rows'][0][0] == 'Charlie'
        assert data['rows'][1][0] == 'Bob'
        assert data['rows'][2][0] == 'Alice'
        print("  PASSED - _sort_desc wins over _sort")
    finally:
        server.shutdown()


def test_sort_before_pagination():
    """Test that sorting is applied before pagination."""
    print("Testing sorting before pagination...")
    csv_content = [['num']] + [[str(100 - i)] for i in range(50)]  # rows from 100 down to 51
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Sort descending, get first 10
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'num', '_size': 10})
        assert r.status_code == 200
        data = r.json()
        # Should be sorted ascending (51, 52, 53, ...)
        assert data['rows'][0][0] == '51'
        assert data['rows'][-1][0] == '60'

        # Sort descending, get first 10 with _sort_desc
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort_desc': 'num', '_size': 10})
        assert r.status_code == 200
        data = r.json()
        # Should be sorted descending (100, 99, 98, ...)
        assert data['rows'][0][0] == '100'
        assert data['rows'][-1][0] == '91'
        print("  PASSED - sorting applied before pagination")
    finally:
        server.shutdown()


def test_invalid_sort_column():
    """Test invalid sort column."""
    print("Testing invalid sort column...")
    csv_content = [['name'], ['Alice']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test unknown column
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'age'})
        assert r.status_code == 400
        assert 'Unknown column' in r.json()['error']
        print("  PASSED - unknown column rejected")

        # Test empty column name
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': ''})
        assert r.status_code == 400
        assert 'Invalid sort column' in r.json()['error']
        print("  PASSED - empty column rejected")
    finally:
        server.shutdown()


def test_shape_lists():
    """Test _shape=lists (default)."""
    print("Testing _shape=lists (default)...")
    csv_content = [['name', 'age'], ['Alice', 25], ['Bob', 30]]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Default shape is lists
        r = requests.get(f'{BASE_URL}{endpoint}')
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data['rows'], list)
        assert isinstance(data['rows'][0], list)
        assert data['rows'][0] == ['Alice', 25]
        assert 'rowid' not in data['rows'][0]
        print("  PASSED - default shape is lists")

        # Explicit _shape=lists
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'lists'})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data['rows'][0], list)
        print("  PASSED - explicit _shape=lists")
    finally:
        server.shutdown()


def test_shape_objects():
    """Test _shape=objects."""
    print("Testing _shape=objects...")
    csv_content = [['name', 'age'], ['Alice', 25], ['Bob', 30]]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'objects'})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data['rows'], list)
        assert isinstance(data['rows'][0], dict)
        assert 'rowid' in data['rows'][0]
        assert data['rows'][0]['rowid'] == 1  # 1-based source-file row number
        assert data['rows'][0]['name'] == 'Alice'
        assert data['rows'][0]['age'] == 25
        assert 'rowid' not in data['columns']
        print("  PASSED - _shape=objects includes rowid")
    finally:
        server.shutdown()


def test_shape_objects_with_offset():
    """Test _shape=objects with _offset."""
    print("Testing _shape=objects with _offset...")
    csv_content = [['num']] + [[str(i)] for i in range(10)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'objects', '_offset': 3, '_size': 2})
        assert r.status_code == 200
        data = r.json()
        assert data['rows'][0]['rowid'] == 4  # offset + 1
        assert data['rows'][1]['rowid'] == 5
        assert data['rows'][0]['num'] == '3'
        assert data['rows'][1]['num'] == '4'
        print("  PASSED - rowid is 1-based source-file row number")
    finally:
        server.shutdown()


def test_invalid_shape():
    """Test invalid _shape value."""
    print("Testing invalid _shape...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'invalid'})
        assert r.status_code == 400
        assert 'must be lists or objects' in r.json()['error']
        print("  PASSED - invalid _shape rejected")
    finally:
        server.shutdown()


def test_rowid_hide():
    """Test _rowid=hide toggle."""
    print("Testing _rowid=hide...")
    csv_content = [['name'], ['Alice']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # With _shape=objects, rowid should be hidden
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'objects', '_rowid': 'hide'})
        assert r.status_code == 200
        data = r.json()
        assert 'rowid' not in data['rows'][0]
        assert data['rows'][0]['name'] == 'Alice'
        print("  PASSED - _rowid=hide removes rowid from objects")

        # With _shape=lists, _rowid should have no effect (no rowid in lists anyway)
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_shape': 'lists', '_rowid': 'hide'})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data['rows'][0], list)
        print("  PASSED - _rowid=hide with lists has no effect")
    finally:
        server.shutdown()


def test_rowid_invalid_value():
    """Test invalid _rowid value."""
    print("Testing invalid _rowid value...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        r = requests.get(f'{BASE_URL}{endpoint}', params={'_rowid': 'show'})
        assert r.status_code == 400
        assert 'Invalid _rowid value' in r.json()['error']
        print("  PASSED - invalid _rowid value rejected")
    finally:
        server.shutdown()


def test_total_hide():
    """Test _total=hide toggle."""
    print("Testing _total=hide...")
    csv_content = [['name']] + [[f'name{i}'] for i in range(25)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Default includes total
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 10})
        assert r.status_code == 200
        data = r.json()
        assert 'total' in data
        assert data['total'] == 25
        print("  PASSED - total included by default")

        # With _total=hide
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 10, '_total': 'hide'})
        assert r.status_code == 200
        data = r.json()
        assert 'total' not in data
        print("  PASSED - _total=hide removes total from response")
    finally:
        server.shutdown()


def test_total_invalid_value():
    """Test invalid _total value."""
    print("Testing invalid _total value...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        r = requests.get(f'{BASE_URL}{endpoint}', params={'_total': 'show'})
        assert r.status_code == 400
        assert 'Invalid _total value' in r.json()['error']
        print("  PASSED - invalid _total value rejected")
    finally:
        server.shutdown()


def test_repeated_parameter():
    """Test that repeated control parameters return error."""
    print("Testing repeated control parameters...")
    csv_content = [['col'], ['val']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Repeated _size
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': ['10', '20']})
        assert r.status_code == 400
        assert 'Repeated control parameter' in r.json()['error']
        print("  PASSED - repeated _size rejected")

        # Repeated _offset
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_offset': ['0', '5']})
        assert r.status_code == 400
        assert 'Repeated control parameter' in r.json()['error']
        print("  PASSED - repeated _offset rejected")

        # Repeated _sort
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': ['name', 'age']})
        assert r.status_code == 400
        assert 'Repeated control parameter' in r.json()['error']
        print("  PASSED - repeated _sort rejected")
    finally:
        server.shutdown()


def test_pagination_sort_combined():
    """Test combined pagination and sorting."""
    print("Testing combined pagination and sorting...")
    csv_content = [['num']] + [[str(i)] for i in range(20, 0, -1)]  # 20, 19, ..., 1
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Sort descending (already in desc order), then paginate
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort': 'num', '_offset': 5, '_size': 3})
        assert r.status_code == 200
        data = r.json()
        # After sorting ascending: 1, 2, 3, ... 20
        # Offset 5, size 3: 6, 7, 8
        assert len(data['rows']) == 3
        assert data['rows'][0][0] == '6'
        assert data['rows'][1][0] == '7'
        assert data['rows'][2][0] == '8'
        assert data.get('total') == 20
        print("  PASSED - pagination and sorting work together")
    finally:
        server.shutdown()


def test_total_with_pagination():
    """Test that total reflects total rows after sorting, not before."""
    print("Testing total reflects sorted total...")
    csv_content = [['num']] + [[str(i)] for i in range(1, 101)]  # 1 to 100
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Sort descending
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_sort_desc': 'num', '_size': 10})
        assert r.status_code == 200
        data = r.json()
        assert data.get('total') == 100
        assert data['rows'][0][0] == '100'
        assert data['rows'][-1][0] == '91'
        print("  PASSED - total is total rows after sorting")
    finally:
        server.shutdown()


def test_all_controls_together():
    """Test all controls working together."""
    print("Testing all controls together...")
    csv_content = [['name', 'score']] + [
        ['Zach', 90], ['Alice', 95], ['Bob', 85], ['Charlie', 92],
        ['David', 88], ['Eve', 97], ['Frank', 91], ['Grace', 89]
    ]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Complex query: sort desc by score, get page 2 (offset 3, size 3), objects shape, hide rowid and total
        r = requests.get(f'{BASE_URL}{endpoint}', params={
            '_sort_desc': 'score',
            '_offset': 3,
            '_size': 3,
            '_shape': 'objects',
            '_rowid': 'hide',
            '_total': 'hide'
        })
        assert r.status_code == 200
        data = r.json()

        # Scores sorted desc: 97, 95, 92, 91, 90, 89, 88, 85
        # Page 2 (offset 3, size 3): 91, 90, 89
        assert len(data['rows']) == 3
        assert data['rows'][0]['score'] == 91
        assert data['rows'][1]['score'] == 90
        assert data['rows'][2]['score'] == 89
        assert 'rowid' not in data['rows'][0]
        assert 'total' not in data
        print("  PASSED - all controls work together")
    finally:
        server.shutdown()


def main():
    # Wait for server to be ready
    print("Waiting for server...")
    for _ in range(10):
        try:
            requests.get(f'{BASE_URL}/', timeout=1)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.5)

    # Run all control tests
    tests = [
        test_pagination_size,
        test_pagination_offset,
        test_pagination_size_offset_combined,
        test_invalid_size,
        test_invalid_offset,
        test_sort_ascending,
        test_sort_descending,
        test_sort_desc_wins,
        test_sort_before_pagination,
        test_invalid_sort_column,
        test_shape_lists,
        test_shape_objects,
        test_shape_objects_with_offset,
        test_invalid_shape,
        test_rowid_hide,
        test_rowid_invalid_value,
        test_total_hide,
        test_total_invalid_value,
        test_repeated_parameter,
        test_pagination_sort_combined,
        test_total_with_pagination,
        test_all_controls_together,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"  FAILED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
