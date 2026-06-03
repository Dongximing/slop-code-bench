#!/usr/bin/env python3
"""Quick test script to check filtering functionality."""

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
    time.sleep(0.5)
    return url, server

def run_tests():
    print("Testing baseline pagination...")
    csv_content = [['num']] + [[str(i)] for i in range(100)]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}', params={'_size': 5, '_offset': 0})
        print(f"Baseline - Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            print(f"  Rows: {len(data['rows'])}, First value: {data['rows'][0][0]}")
            print(f"  Total: {data.get('total')}")
    finally:
        server.shutdown()

    print("\nTesting filter: exact...")
    csv_content = [['name', 'age'], ['Alice', '25'], ['Bob', '30'], ['Charlie', '35']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}', params={'name__exact': 'Alice'})
        print(f"Exact filter - Status: {r.status_code}")
        if r.status_code == 200:
            print(f"  Rows: {len(r.json()['rows'])}")
        else:
            print(f"  Error: {r.text}")
    finally:
        server.shutdown()

    print("\nTesting filter: less...")
    csv_content = [['name', 'age'], ['Alice', '25'], ['Bob', '30'], ['Charlie', '35']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}', params={'age__less': '30'})
        print(f"Less filter - Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            rows = data['rows']
            print(f"  Rows: {len(rows)}")
            for row in rows:
                print(f"    {row}")
        else:
            print(f"  Error: {r.text}")
    finally:
        server.shutdown()

    print("\nTesting filter: greater...")
    csv_content = [['name', 'score'], ['Alice', '95.5'], ['Bob', '87.2'], ['Charlie', '92.0']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}', params={'score__greater': '90'})
        print(f"Greater filter - Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            rows = data['rows']
            print(f"  Rows: {len(rows)}")
            for row in rows:
                print(f"    {row}")
        else:
            print(f"  Error: {r.text}")
    finally:
        server.shutdown()

    print("\nTesting multiple filters (AND)...")
    csv_content = [['name', 'score'], ['Alice', '95.5'], ['Bob', '87.2'], ['Charlie', '92.0']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        r = requests.get(f'{BASE_URL}{endpoint}', params={'score__greater': '88', 'score__less': '94'})
        print(f"Combined filters - Status: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            rows = data['rows']
            print(f"  Rows: {len(rows)}")
            for row in rows:
                print(f"    {row}")
        else:
            print(f"  Error: {r.text}")
    finally:
        server.shutdown()

    print("\nTesting filter errors...")
    csv_content = [['name', 'age'], ['Alice', '25']]
    url, server = create_test_csv(csv_content)
    try:
        r = requests.get(f'{BASE_URL}/convert', params={'source': url})
        endpoint = r.json()['endpoint']
        
        tests = [
            ('unknown__exact', 'value', 'Unknown column'),
            ('name__invalid', 'value', 'Invalid comparator'),
            ('age__less', 'not-a-number', 'Comparator target not numeric'),
        ]
        
        for param, val, expected in tests:
            r = requests.get(f'{BASE_URL}{endpoint}', params={param: val})
            print(f"  {param} - Status: {r.status_code}")
            if r.status_code == 400:
                err = r.json()['error']
                print(f"    Error: {err}")
            print()
    finally:
        server.shutdown()

    print("\nAll filter tests completed!")

if __name__ == '__main__':
    run_tests()
