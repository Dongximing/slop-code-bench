#!/usr/bin/env python3
"""Test script for Datagate functionality."""

import subprocess
import time
import urllib.request
import urllib.error
import json
import sys

SERVER_URL = "http://127.0.0.1:18080"


def start_server():
    """Start the server in background."""
    proc = subprocess.Popen(
        ["venv/bin/python", "datagate.py", "start", "--port", "18080", "--address", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Wait for server to start
    time.sleep(2)
    return proc


def stop_server(proc):
    """Stop the server."""
    proc.terminate()
    proc.wait(timeout=5)


def test_endpoint(path, expected_status=200):
    """Test an endpoint."""
    try:
        req = urllib.request.Request(f"{SERVER_URL}{path}")
        req.add_header('Origin', '*')
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            print(f"✓ {path}: {json.dumps(data, indent=2)}")
            return data
    except urllib.error.HTTPError as e:
        data = json.loads(e.read().decode('utf-8'))
        print(f"✗ {path}: HTTP {e.code} - {json.dumps(data)}")
        if expected_status == e.code:
            print(f"  (Expected status {e.code})")
            return data
        return None
    except Exception as e:
        print(f"✗ {path}: Error - {e}")
        return None


def check_cors_headers():
    """Check CORS headers are present."""
    try:
        req = urllib.request.Request(f"{SERVER_URL}/convert?source=https://example.com")
        req.add_header('Origin', 'http://test.com')
        with urllib.request.urlopen(req, timeout=10) as response:
            headers = response.headers
            print(f"CORS Access-Control-Allow-Origin: {headers.get('Access-Control-Allow-Origin', 'MISSING')}")
            return headers.get('Access-Control-Allow-Origin') == '*'
    except Exception as e:
        print(f"CORS check failed: {e}")
        return False


if __name__ == '__main__':
    print("Starting Datagate server...")
    server_proc = start_server()

    try:
        time.sleep(3)  # Give server time to fully start

        print("\n=== Testing /convert endpoint ===")

        # Test 1: Missing source parameter
        print("\nTest 1: Missing source parameter")
        test_endpoint("/convert", expected_status=400)

        # Test 2: Invalid URL
        print("\nTest 2: Invalid URL")
        test_endpoint("/convert?source=not-a-valid-url", expected_status=400)

        # Test 3: Invalid charset
        print("\nTest 3: Invalid charset")
        test_endpoint("/convert?source=https://example.com&charset=invalid-charset", expected_status=400)

        # Test 4: Non-existent dataset
        print("\nTest 4: Non-existent dataset")
        test_endpoint("/datasets/unknown", expected_status=404)

        print("\n=== Checking CORS headers ===")
        if check_cors_headers():
            print("✓ CORS headers are present")
        else:
            print("✗ CORS headers missing")

        print("\n=== All basic tests completed ===")

    finally:
        print("\nStopping server...")
        stop_server(server_proc)
