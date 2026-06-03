#!/usr/bin/env python3
"""Quick test script to check filtering functionality and baseline behavior."""

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

def test_baseline():
    """Test basic functionality without any filters."""
    print("Testing baseline pagination...")
    csv_content = [['num']] + [[str(i)] for i in range(100)]
    url, server = create_test_csv(csv_content)
    try:
        # Convert the CSV
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        print(f"Endpoint: {endpoint}")

        # Test basic pagination
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 5, '_offset': 0})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            print(f"First row: {data['rows'][0]}")
            if len(data['rows']) > 0:
                print(f"First value: {data['rows'][0][0]}")
            if 'total' in data:
                print(f"Total: {data['total']}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_exact():
    """Test exact string filter."""
    print("\nTesting exact filter...")
    csv_content = [['name', 'age'], ['Alice', '25'], ['Bob', '30'], ['Charlie', '35']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Filter by name exact match
        r = requests.get(f'{BASE_URL}{endpoint}', params={'name__exact': 'Alice'})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            for row in data['rows']:
                print(f"  Row: {row}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_contains():
    """Test contains filter."""
    print("\nTesting contains filter...")
    csv_content = [['name'], ['Alice'], ['Bob'], ['Charlie']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Filter by name contains
        r = requests.get(f'{BASE_URL}{endpoint}', params={'name__contains': 'li'})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            for row in data['rows']:
                print(f"  Row: {row}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_less():
    """Test less than filter."""
    print("\nTesting less than filter...")
    csv_content = [['name', 'age'], ['Alice', '25'], ['Bob', '30'], ['Charlie', '35']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Filter by age less than
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__less': '30'})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            for row in data['rows']:
                print(f"  Row: {row}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_greater():
    """Test greater than filter."""
    print("\nTesting greater than filter...")
    csv_content = [['name', 'age'], ['Alice', '25'], ['Bob', '30'], ['Charlie', '35']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Filter by age greater than
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__greater': '28'})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            for row in data['rows']:
                print(f"  Row: {row}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_combined():
    """Test multiple filters (AND)."""
    print("\nTesting combined filters...")
    csv_content = [['name', 'age', 'score'], ['Alice', '25', '95.5'], ['Bob', '30', '87.2'], ['Charlie', '35', '92.0']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Filter by age > 28 AND score > 90
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__greater': '28', 'score__greater': '90'})
        print(f"Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"Rows returned: {len(data['rows'])}")
            for row in data['rows']:
                print(f"  Row: {row}")
        else:
            print(f"Error: {r.text}")
    finally:
        server.shutdown()

def test_filter_errors():
    """Test filter error cases."""
    print("\nTesting filter errors...")
    csv_content = [['name', 'age'], ['Alice', '25']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']

        # Test unknown column
        r = requests.get(f'{BASE_URL}{endpoint}', params={'unknown__exact': 'value'})
        print(f"Unknown column filter - Status: {r.status_code}")
        if r.status_code == 400:
            print(f"  Error (expected): {r.json()['error']}")

        # Test invalid comparator
        r = requests.get(f'{BASE_URL}{endpoint}', params={'name__invalid': 'value'})
        print(f"Invalid comparator - Status: {r.status_code}")
        if r.status_code == 400:
            print(f"  Error (expected): {r.json()['error']}")

        # Test non-numeric for less
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__less': 'not-a-number'})
        print(f"Non-numeric for less - Status: {r.status_code}")
        if r.status_code == 400:
            print(f"  Error (expected): {r.json()['error']}")

        # Test non-numeric for greater
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__greater': 'not-a-number'})
        print(f"Non-numeric for greater - Status: {r.status_code}")
        if r.status_code == 400:
            print(f"  Error (expected): {r.json()['error']}")

        # Test duplicate filter keys
        r = requests.get(f'{BASE_URL}{endpoint}', params={'name__exact': ['Alice', 'Bob']})
        print(f"Duplicate filter keys - Status: {r.status_code}")
        if r.status_code == 400:
            print(f"  Error (expected): {r.json()['error']}")
    finally:
        server.shutdown()

if __name__ == '__main__':
    # Check if server is running
    try:
        requests.get(f'{BASE_URL}/', timeout=1)
        print("Server is running")
    except requests.exceptions.ConnectionError:
        print("ERROR: Server not running on port 8001")
        sys.exit(1)

    test_baseline()
    test_filter_exact()
    test_filter_contains()
    test_filter_less()
    test_filter_greater()
    test_filter_combined()
    test_filter_errors()

    print("\nDone!")
