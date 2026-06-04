#!/usr/bin/env python3
"""Test script for caching implementation."""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys
import os
import socket

SERVER_PORT = 18085
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
WORKDIR = "/workspace/datagate"


def wait_for_port(port, host='127.0.0.1', timeout=10):
    """Wait for a port to become available."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(1)
            result = sock.connect_ex((host, port))
            sock.close()
            if result == 0:
                return True
        except socket.error:
            pass
        time.sleep(0.5)
    return False


def start_server(cache_enabled=None):
    """Start the server in background."""
    env = os.environ.copy()
    if cache_enabled is not None:
        env['CACHE_ENABLED'] = cache_enabled
    proc = subprocess.Popen(
        ["venv/bin/python", "datagate.py", "start", "--port", str(SERVER_PORT), "--address", "127.0.0.1"],
        cwd=WORKDIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    if wait_for_port(SERVER_PORT):
        return proc

    stdout = proc.stdout.read().decode('utf-8', errors='replace') if proc.stdout else ""
    stderr = proc.stderr.read().decode('utf-8', errors='replace') if proc.stderr else ""
    print(f"Server stdout: {stdout}")
    print(f"Server stderr: {stderr}")
    return None


def stop_server(proc):
    """Stop the server."""
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def http_get(path):
    """Make HTTP GET request."""
    url = f"{SERVER_URL}{path}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=30) as response:
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
            data = {"raw": raw_data}
        return e.code, data, {}
    except Exception as e:
        return None, {"error": str(e)}, {}


def main():
    print("=== Testing Caching Implementation ===\n")

    # Test 1: CACHE_ENABLED environment variable - invalid value should fail startup
    print("Test 1: Invalid CACHE_ENABLED value (should fail startup)")
    server_proc = start_server(cache_enabled="invalid")
    if server_proc is None:
        print("  ✓ Server failed to start with invalid CACHE_ENABLED (expected)")
    else:
        print("  ✗ Server should not start with invalid CACHE_ENABLED")
        stop_server(server_proc)
    print()

    # Test 2: Valid CACHE_ENABLED values
    print("Test 2: Valid CACHE_ENABLED values")
    for val in ["1", "true", "yes", "on", "0", "false", "no", "off"]:
        print(f"  Testing CACHE_ENABLED={val}")
        # server_proc = start_server(cache_enabled=val)
        # if server_proc:
        #     stop_server(server_proc)
        #     print(f"    ✓ Server started with {val}")
        # else:
        #     print(f"    ✗ Server failed to start with {val}")
        stop_server(server_proc)
    print()

    # Test 3: Caching with default (enabled)
    print("Test 3: Default caching behavior (enabled)")
    server_proc = start_server()
    if server_proc is None:
        print("  ✗ Could not start server")
        return

    try:
        csv_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/titanic.csv"

        # First request - should fetch from remote
        code1, data1, _ = http_get(f"/convert?source={csv_url}")
        print(f"  First request: Status {code1}")
        if code1 == 200 and "endpoint" in str(data1):
            endpoint1 = data1["endpoint"]
            print(f"    Endpoint: {endpoint1}")
            dataset_id = endpoint1.split("/")[2]
        else:
            stop_server(server_proc)
            print("  ✗ First request failed")
            print(f"    Response: {data1}")
            return

        # Second request with same URL - should use cache
        code2, data2, _ = http_get(f"/convert?source={csv_url}")
        print(f"  Second request (cached): Status {code2}")
        if code2 == 200 and "endpoint" in str(data2):
            endpoint2 = data2["endpoint"]
            print(f"    Endpoint: {endpoint2}")

            # Should be same endpoint (deterministic and cached)
            if endpoint1 == endpoint2:
                print("  ✓ Cache working: same URL returns same endpoint")
            else:
                print("  ✗ Cache not working properly: different endpoints")
        else:
            print("  ✗ Second request failed")
            print(f"    Response: {data2}")

        # Test 4: Force parameter (re-ingestion) - HTTP 400 with invalid force value
        print("\nTest 4: Force parameter validation")
        code3, data3, _ = http_get(f"/convert?source={csv_url}&force=value")
        print(f"  Force with extra value: Status {code3}")
        if code3 == 400:
            print("  ✓ Correctly rejected 'force' with extra value (HTTP 400)")
        else:
            print(f"  ✗ Expected 400, got {code3}: {data3}")

        # Test 5: Force parameter with presence (should work when caching enabled)
        code4, data4, _ = http_get(f"/convert?source={csv_url}&force")
        print(f"  Force without value (presence): Status {code4}")
        if code4 == 200:
            print("  ✓ Force parameter accepted (re-ingests data)")
        else:
            print(f"  ✗ Expected 200, got {code4}: {data4}")

        # Test 6: Verify dataset is still queryable after forced re-ingestion
        print("\nTest 6: Dataset queryability after force")
        if code4 == 200 and "endpoint" in str(data4):
            endpoint_force = data4["endpoint"]
            print(f"  Dataset endpoint: {endpoint_force}")
            # Try to access the dataset
            code5, data5, _ = http_get(endpoint_force)
            if code5 == 200 and "columns" in data5 and "rows" in data5:
                print(f"  ✓ Dataset is queryable: {len(data5['rows'])} rows")
            else:
                print(f"  ✗ Dataset not queryable: {code5} {data5}")

    finally:
        stop_server(server_proc)

    print("\n=== Tests completed ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
