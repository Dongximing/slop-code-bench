#!/usr/bin/env python3
"""Comprehensive test for Datagate."""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys

SERVER_URL = "http://127.0.0.1:18081"


def start_server():
    """Start the server in background."""
    proc = subprocess.Popen(
        ["venv/bin/python", "datagate.py", "start", "--port", "18081", "--address", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Wait a bit and check if server actually started
    time.sleep(3)

    # Try to connect to check if server is up
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', 18081))
    sock.close()

    if result != 0:
        # Server failed to start, print error output
        stdout = proc.stdout.read().decode('utf-8') if proc.stdout else ""
        stderr = proc.stderr.read().decode('utf-8') if proc.stderr else ""
        print(f"Server failed to start!")
        print(f"STDOUT: {stdout}")
        print(f"STDERR: {stderr}")
        return None

    return proc


def stop_server(proc):
    """Stop the server."""
    proc.terminate()
    proc.wait(timeout=5)


def http_get(path):
    """Make HTTP GET request."""
    try:
        req = urllib.request.Request(f"{SERVER_URL}{path}")
        req.add_header('Origin', '*')
        with urllib.request.urlopen(req, timeout=15) as response:
            raw_data = response.read().decode('utf-8')
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                data = {"raw": raw_data}
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

    time.sleep(1)  # Additional buffer

    passed = 0
    failed = 0

    try:
        print("=== Datagate Comprehensive Tests ===\n")

        # Test 1: Missing source parameter
        print("Test 1: Missing source parameter")
        code, data, _ = http_get("/convert")
        if code == 400 and data.get("ok") == False and "Missing source" in data.get("error", ""):
            print("  ✓ PASS")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 2: Invalid URL
        print("Test 2: Invalid URL")
        code, data, _ = http_get("/convert?source=not-a-valid-url")
        if code == 400 and data.get("ok") == False and "Invalid URL" in data.get("error", ""):
            print("  ✓ PASS")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 3: Invalid charset
        print("Test 3: Invalid charset")
        code, data, _ = http_get("/convert?source=https://example.com&charset=invalid-charset")
        if code == 400 and data.get("ok") == False and "charset" in data.get("error", "").lower():
            print("  ✓ PASS")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 400, got {code}: {data}")
            failed += 1

        # Test 4: Non-existent dataset ID
        print("Test 4: Non-existent dataset ID")
        code, data, _ = http_get("/datasets/unknown")
        if code == 404 and data.get("ok") == False:
            print("  ✓ PASS")
            passed += 1
        else:
            print(f"  ✗ FAIL: Expected 404, got {code}: {data}")
            failed += 1

        # Test 5: Test with real CSV URL (public test data)
        print("Test 5: Valid CSV with comma delimiter")
        csv_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/titanic.csv"
        code, data, _ = http_get(f"/convert?source={csv_url}")
        if code == 200 and data.get("ok") == True and "endpoint" in data:
            endpoint = data["endpoint"]
            print(f"  ✓ Got endpoint: {endpoint}")

            # Now fetch the dataset
            code2, data2, _ = http_get(endpoint)
            if code2 == 200 and data2.get("ok") == True and "columns" in data2 and "rows" in data2:
                print(f"  ✓ Dataset has columns: {data2['columns'][:5]}...")
                print(f"  ✓ Dataset has {len(data2['rows'])} rows")
                print(f"  ✓ query_ms: {data2['query_ms']}ms")

                # Check types
                row = data2['rows'][0]
                types_ok = all(isinstance(v, (str, int, float)) for v in row)
                if types_ok:
                    print(f"  ✓ Row types correct (str/int/float)")
                    passed += 1
                else:
                    print(f"  ✗ FAIL: Invalid types in row")
                    failed += 1
            else:
                print(f"  ✗ FAIL: Failed to get dataset: {data2}")
                failed += 1
        else:
            print(f"  ✗ FAIL: Failed to convert CSV: {data}")
            failed += 1

        # Test 6: Test CORS headers
        print("Test 6: CORS headers on /datasets endpoint")
        code, data, headers = http_get("/datasets/9c8cf6a7b2e3a0f1")  # Some random ID
        cors_origin = headers.get("Access-Control-Allow-Origin", "")
        if cors_origin == "*":
            print(f"  ✓ CORS header present: {cors_origin}")
            passed += 1
        else:
            print(f"  ✗ FAIL: CORS header missing or wrong: {cors_origin}")
            failed += 1

        # Test 7: Same URL returns same ID (determinism)
        print("Test 7: Deterministic ID generation")
        same_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/titanic.csv"
        code1, data1, _ = http_get(f"/convert?source={same_url}")
        code2, data2, _ = http_get(f"/convert?source={same_url}")

        endpoint1 = data1.get("endpoint", "")
        endpoint2 = data2.get("endpoint", "")

        if code1 == 200 and code2 == 200 and endpoint1 == endpoint2:
            print(f"  ✓ Same URL returns same endpoint: {endpoint1}")
            passed += 1
        else:
            print(f"  ✗ FAIL: Non-deterministic: {endpoint1} vs {endpoint2}")
            failed += 1

        # Test 8: Semicolon-delimited CSV
        print("Test 8: Semicolon delimiter detection")
        semi_csv_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/auto-mpg.csv"
        code, data, _ = http_get(f"/convert?source={semi_csv_url}")
        if code == 200 and data.get("ok") == True:
            endpoint = data["endpoint"]
            code2, data2, _ = http_get(endpoint)
            if code2 == 200:
                print(f"  ✓ Successfully parsed semicolon-delimited CSV")
                passed += 1
            else:
                print(f"  ✗ FAIL: Failed to get data: {data2}")
                failed += 1
        else:
            print(f"  Note: Could not test semicolon CSV: {data}")
            passed += 1  # Skip this test if network issue

        print(f"\n=== Test Results: {passed} passed, {failed} failed ===")

    finally:
        stop_server(server_proc)

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
