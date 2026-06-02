#!/usr/bin/env python3
"""Test the datagate service."""

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


def test_missing_source():
    """Test missing source parameter."""
    print("Testing missing source parameter...")
    r = requests.get(f'{BASE_URL}/convert')
    assert r.status_code == 400
    data = r.json()
    assert data.get('ok') is False
    assert 'Missing' in data.get('error', '')
    print("  PASSED")


def test_invalid_url():
    """Test invalid URL."""
    print("Testing invalid URL...")
    r = requests.get(f'{BASE_URL}/convert', params={'source': 'not-a-url'})
    assert r.status_code == 400
    data = r.json()
    assert data.get('ok') is False
    print("  PASSED")


def test_unreachable_source():
    """Test unreachable or non-existent source."""
    print("Testing unreachable source...")
    r = requests.get(f'{BASE_URL}/convert', params={'source': 'http://127.0.0.1:99999/nonexistent.csv'})
    assert r.status_code == 404
    data = r.json()
    assert data.get('ok') is False
    print("  PASSED")


def test_csv_conversion():
    """Test CSV conversion with default comma delimiter."""
    print("Testing CSV conversion...")
    csv_content = [
        ['name', 'age', 'score'],
        ['Alice', '25', '95.5'],
        ['Bob', '30', '87.2'],
        ['Charlie', '35', '92.0']
    ]

    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        assert r.status_code == 200
        data = r.json()
        assert data.get('ok') is True

        endpoint = data.get('endpoint')
        assert endpoint.startswith('/datasets/')

        # Fetch the dataset
        r = requests.get(f'{BASE_URL}{endpoint}')
        assert r.status_code == 200
        ds = r.json()
        assert ds.get('ok') is True
        assert ds['columns'] == ['name', 'age', 'score']
        assert len(ds['rows']) == 3
        assert ds['rows'][0][0] == 'Alice'
        assert ds['rows'][0][1] == 25  # Integer
        assert ds['rows'][0][2] == 95.5  # Float
        assert ds['rows'][1][0] == 'Bob'
        assert ds['rows'][1][1] == 30
        assert ds['query_ms'] >= 0
        print("  PASSED")
    finally:
        server.shutdown()


def test_deterministic_ids():
    """Test same URL produces same ID."""
    print("Testing deterministic IDs...")
    csv_content = [['col1', 'col2'], ['val1', 'val2']]
    url, server = create_test_csv(csv_content)
    try:
        r1 = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint1 = r1.json()['endpoint']
        time.sleep(0.5)  # Ensure requests are separate

        r2 = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint2 = r2.json()['endpoint']

        assert endpoint1 == endpoint2
        print("  PASSED")
    finally:
        server.shutdown()


def test_semicolon_delimiter():
    """Test semicolon delimiter."""
    print("Testing semicolon delimiter...")
    csv_content = [
        ['col1', 'col2'],
        ['val1', 'val2'],
        ['val3', 'val4']
    ]

    url, server = create_test_csv(csv_content, delimiter=';')
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        assert r.status_code == 200
        data = r.json()
        assert data.get('ok') is True

        endpoint = data['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}')
        ds = r.json()
        assert ds['columns'] == ['col1', 'col2']
        assert len(ds['rows']) == 2
        print("  PASSED")
    finally:
        server.shutdown()


def test_tab_delimiter():
    """Test tab delimiter."""
    print("Testing tab delimiter...")
    csv_content = [
        ['col1', 'col2'],
        ['val1', 'val2'],
        ['val3', 'val4']
    ]

    url, server = create_test_csv(csv_content, delimiter='\t')
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        assert r.status_code == 200
        data = r.json()
        assert data.get('ok') is True
        print("  PASSED")
    finally:
        server.shutdown()


def test_time_values():
    """Test that time-like values remain text."""
    print("Testing time-like values...")
    csv_content = [
        ['name', 'time', 'date'],
        ['Alice', '08:30', '2024-01-15'],
        ['Bob', '9:15', '2024-01-16']
    ]

    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}')
        ds = r.json()

        # Time values should remain strings
        assert isinstance(ds['rows'][0][1], str)
        assert ds['rows'][0][1] == '08:30'
        assert isinstance(ds['rows'][1][1], str)
        assert ds['rows'][1][1] == '9:15'
        print("  PASSED")
    finally:
        server.shutdown()


def test_charset_parameter():
    """Test charset parameter."""
    print("Testing charset parameter...")
    csv_content = [['col1', 'col2'], ['val1', 'val2']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url, 'charset': 'utf-8'})
        assert r.status_code == 200
        assert r.json().get('ok') is True
        print("  PASSED")
    finally:
        server.shutdown()


def test_charset_detection():
    """Test automatic charset detection."""
    print("Testing charset detection...")
    fd, path = tempfile.mkstemp(suffix='.csv')
    with os.fdopen(fd, 'w', newline='', encoding='latin-1') as f:
        writer = csv.writer(f)
        writer.writerow(['col1', 'col2'])
        writer.writerow(['café', 'naïve'])

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
    time.sleep(0.5)

    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        # Should succeed with auto-detection
        assert r.status_code == 200
        print("  PASSED")
    finally:
        server.shutdown()


def test_invalid_charset():
    """Test invalid charset parameter."""
    print("Testing invalid charset...")
    csv_content = [['col1', 'col2'], ['val1', 'val2']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url, 'charset': 'invalid-charset'})
        assert r.status_code == 400
        data = r.json()
        assert data.get('ok') is False
        assert 'charset' in data.get('error', '').lower()
        print("  PASSED")
    finally:
        server.shutdown()


def test_row_limit():
    """Test that row limit is 100."""
    print("Testing row limit...")
    csv_content = [['col1', 'col2']] + [[f'val{i}', f'num{i}'] for i in range(150)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}')
        ds = r.json()
        assert len(ds['rows']) == 100
        print("  PASSED")
    finally:
        server.shutdown()


def test_unknown_dataset():
    """Test unknown dataset ID."""
    print("Testing unknown dataset ID...")
    r = requests.get(f'{BASE_URL}/datasets/unknown123')
    assert r.status_code == 404
    data = r.json()
    assert data.get('ok') is False
    print("  PASSED")


def test_non_tabular():
    """Test non-tabular content (only 1 row)."""
    print("Testing non-tabular content...")
    # A single row with no data is not tabular (requires header + data)
    csv_content = [['only_one_row']]  # Just headers, no data

    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        assert r.status_code == 400
        data = r.json()
        assert data.get('ok') is False
        print("  PASSED")
    finally:
        server.shutdown()


def test_single_row_fails():
    """Test CSV with only header row fails."""
    print("Testing single row (only header)...")
    csv_content = [['col1', 'col2']]  # No data rows

    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        assert r.status_code == 400
        data = r.json()
        assert data.get('ok') is False
        assert 'header' in data.get('error', '').lower() or 'row' in data.get('error', '').lower()
        print("  PASSED")
    finally:
        server.shutdown()


def test_cors_headers():
    """Test that CORS headers are present."""
    print("Testing CORS headers...")
    r = requests.get(f'{BASE_URL}/convert')
    assert 'Access-Control-Allow-Origin' in r.headers
    assert r.headers['Access-Control-Allow-Origin'] == '*'
    print("  PASSED")


def test_negative_numbers():
    """Test negative numbers."""
    print("Testing negative numbers...")
    csv_content = [['col1'], ['-10'], ['-3.14']]

    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}')
        ds = r.json()

        # Should be parsed as negative int and negative float
        assert isinstance(ds['rows'][0][0], int)
        assert ds['rows'][0][0] == -10
        assert isinstance(ds['rows'][1][0], float)
        assert ds['rows'][1][0] == -3.14
        print("  PASSED")
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

    # Run all tests
    tests = [
        test_missing_source,
        test_invalid_url,
        test_unreachable_source,
        test_csv_conversion,
        test_deterministic_ids,
        test_semicolon_delimiter,
        test_tab_delimiter,
        test_time_values,
        test_charset_parameter,
        test_charset_detection,
        test_invalid_charset,
        test_row_limit,
        test_unknown_dataset,
        test_non_tabular,
        test_single_row_fails,
        test_cors_headers,
        test_negative_numbers,
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

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
