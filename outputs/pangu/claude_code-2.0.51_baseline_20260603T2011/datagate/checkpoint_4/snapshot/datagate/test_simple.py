#!/usr/bin/env python3
"""Simple test script for Datagate."""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys
import os
import socket

SERVER_PORT = 18082
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


def start_server():
    """Start the server in background."""
    env = os.environ.copy()
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
    print(f"Server stdout:\n{stdout}")
    print(f"Server stderr:\n{stderr}")
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
        req.add_header('Origin', 'http://test.com')
        with urllib.request.urlopen(req, timeout=10) as response:
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
    print("Starting Datagate server...")
    server_proc = start_server()
    if server_proc is None:
        print("FAILED: Could not start server")
        sys.exit(1)

    try:
        print("\n=== Testing /convert endpoint ===\n")

        # Test 1: Missing source parameter
        print("Test 1: Missing source parameter")
        code, data, _ = http_get("/convert")
        print(f"  Status: {code}, Data: {data}")

        # Test 2: Invalid URL
        print("\nTest 2: Invalid URL")
        code, data, _ = http_get("/convert?source=not-a-valid-url")
        print(f"  Status: {code}, Data: {data}")

        # Test 3: Invalid charset
        print("\nTest 3: Invalid charset")
        code, data, _ = http_get("/convert?source=https://example.com&charset=invalid-charset")
        print(f"  Status: {code}, Data: {data}")

        # Test 4: Non-existent dataset
        print("\nTest 4: Non-existent dataset")
        code, data, _ = http_get("/datasets/unknown")
        print(f"  Status: {code}, Data: {data}")

        # Test 5: Valid CSV
        print("\nTest 5: Valid CSV (titanic.csv)")
        csv_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/doc/data/titanic.csv"
        code, data, _ = http_get(f"/convert?source={csv_url}")
        print(f"  Convert Status: {code}, Data: {data}")

        if code == 200:
            endpoint = data.get("endpoint", "")
            print(f"\n  Fetching dataset from {endpoint}")
            code2, data2, _ = http_get(endpoint)
            print(f"  Dataset Status: {code2}")
            if code2 == 200:
                print(f"  Columns: {data2.get('columns', [])[:5]}...")
                print(f"  Rows count: {len(data2.get('rows', []))}")
                print(f"  Query time: {data2.get('query_ms')}ms")

        print("\n=== Tests completed ===")

    finally:
        stop_server(server_proc)


if __name__ == "__main__":
    main()
