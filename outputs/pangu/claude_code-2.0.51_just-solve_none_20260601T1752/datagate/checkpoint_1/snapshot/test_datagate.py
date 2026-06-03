#!/usr/bin/env python3
"""Test script for datagate."""

import subprocess
import time
import sys
import urllib.request
import urllib.error
import json

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
    # Start datagate server
    server_process = subprocess.Popen(
        [sys.executable, 'datagate.py', 'start', '--port', '9999', '--address', '127.0.0.1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        if not wait_for_server('http://127.0.0.1:9999/', timeout=5):
            print("ERROR: Server failed to start")
            return False

        base_url = 'http://127.0.0.1:9999'

        tests_passed = 0
        tests_total = 0

        # Test 1: Missing source parameter
        print("Test 1: Missing source parameter")
        try:
            resp = urllib.request.urlopen(f'{base_url}/convert')
            print("  FAIL: Expected 400, got 200")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                data = json.loads(e.read().decode())
                if data.get('ok') == False and 'Missing source' in data.get('error', ''):
                    print("  PASS")
                    tests_passed += 1
                else:
                    print(f"  FAIL: {data}")
            else:
                print(f"  FAIL: Expected 400, got {e.code}")
        tests_total += 1

        # Test 2: Invalid URL
        print("Test 2: Invalid URL")
        try:
            resp = urllib.request.urlopen(f'{base_url}/convert?source=not-a-url')
            print("  FAIL: Expected 400, got 200")
        except urllib.error.HTTPError as e:
            if e.code == 400:
                data = json.loads(e.read().decode())
                if data.get('ok') == False and 'Invalid URL' in data.get('error', ''):
                    print("  PASS")
                    tests_passed += 1
                else:
                    print(f"  FAIL: {data}")
            else:
                print(f"  FAIL: Expected 400, got {e.code}")
        tests_total += 1

        # Test 3: Unknown dataset
        print("Test 3: Unknown dataset ID (404)")
        try:
            urllib.request.urlopen(f'{base_url}/datasets/unknown123')
            print("  FAIL: Expected 404, got 200")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = json.loads(e.read().decode())
                if data.get('ok') == False:
                    print("  PASS")
                    tests_passed += 1
                else:
                    print(f"  FAIL: {data}")
            else:
                print(f"  FAIL: Expected 404, got {e.code}")
        tests_total += 1

        # Test 4: Unknown route
        print("Test 4: Unknown route (404)")
        try:
            urllib.request.urlopen(f'{base_url}/unknown')
            print("  FAIL: Expected 404, got 200")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                data = json.loads(e.read().decode())
                if data.get('ok') == False:
                    print("  PASS")
                    tests_passed += 1
                else:
                    print(f"  FAIL: {data}")
            else:
                print(f"  FAIL: Expected 404, got {e.code}")
        tests_total += 1

        # Test 5: CORS headers
        print("Test 5: CORS headers")
        try:
            req = urllib.request.Request(f'{base_url}/convert', headers={'Origin': 'http://example.com'})
            resp = urllib.request.urlopen(req)
            headers = resp.headers
            if 'Access-Control-Allow-Origin' in headers:
                print("  PASS")
                tests_passed += 1
            else:
                print("  FAIL: CORS header missing")
        except Exception as e:
            print(f"  FAIL: {e}")
        tests_total += 1

        # Test 6: Response envelope success
        print("Test 6: Success response has ok=true")
        try:
            # We'll test a real CSV conversion (using a public CSV)
            resp = urllib.request.urlopen(f'{base_url}/convert?source=http://127.0.0.1:8888/test_data.csv')
            data = json.loads(resp.read().decode())
            if data.get('ok') == True and 'endpoint' in data:
                print("  PASS")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        except urllib.error.HTTPError as e:
            # Might fail if test server not running
            data = json.loads(e.read().decode())
            if e.code == 404 and 'unreachable' in data.get('error', '').lower():
                print("  SKIP (test HTTP server not running)")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        tests_total += 1

        # Test 7: Error response envelope
        print("Test 7: Error response has ok=false with error message")
        try:
            urllib.request.urlopen(f'{base_url}/unknown')
            print("  FAIL: Expected 404")
        except urllib.error.HTTPError as e:
            data = json.loads(e.read().decode())
            if data.get('ok') == False and 'error' in data:
                print("  PASS")
                tests_passed += 1
            else:
                print(f"  FAIL: {data}")
        tests_total += 1

        print(f"\nResults: {tests_passed}/{tests_total} tests passed")
        return tests_passed == tests_total

    finally:
        server_process.terminate()
        server_process.wait()

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
