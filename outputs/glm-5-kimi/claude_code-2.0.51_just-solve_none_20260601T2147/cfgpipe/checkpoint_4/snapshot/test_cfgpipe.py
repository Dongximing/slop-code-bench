#!/usr/bin/env python3
"""Test script for cfgpipe with mock servers."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from flask import Flask, jsonify, request

# Mock Primary Store
primary_store_app = Flask(__name__)
primary_store_data = {}
primary_store_versions = {}
primary_store_events = []
primary_store_cursor = 0

@primary_store_app.route('/v1/primary/kv', methods=['GET'])
def primary_kv():
    key = request.args.get('key')
    if key not in primary_store_data:
        return jsonify({"found": False}), 404
    return jsonify({"found": True, "value": primary_store_data[key]})

@primary_store_app.route('/v1/primary/watch', methods=['GET'])
def primary_watch():
    global primary_store_cursor
    cursor = int(request.args.get('cursor', 0))
    keys = request.args.getlist('key')

    # Return events after the cursor
    events_to_return = []
    for evt in primary_store_events:
        if evt['version'] > cursor:
            if evt['key'] in keys:
                events_to_return.append({
                    "key": evt['key'],
                    "value": evt['value'],
                    "version": evt['version']
                })

    new_cursor = primary_store_cursor
    return jsonify({"cursor": new_cursor, "events": events_to_return})

def run_primary_store():
    primary_store_app.run(port=8500, threaded=True)

# Mock Secondary Store
secondary_store_app = Flask(__name__)
secondary_store_data = {}

@secondary_store_app.route('/v1/secondary/kv', methods=['GET'])
def secondary_kv():
    key = request.args.get('key')
    if key not in secondary_store_data:
        return jsonify({"found": False})
    return jsonify({"found": True, "value": secondary_store_data[key]})

@secondary_store_app.route('/v1/secondary/batch-read', methods=['POST'])
def secondary_batch_read():
    data = request.get_json()
    keys = data.get('keys', [])
    items = []
    for key in keys:
        if key in secondary_store_data:
            items.append({"key": key, "status": "ok", "value": secondary_store_data[key]})
        else:
            items.append({"key": key, "status": "missing"})
    return jsonify({"items": items})

def run_secondary_store():
    secondary_store_app.run(port=6400, threaded=True)

def test_non_watch_mode():
    """Test example 6.3: Secondary-Store Seeding."""
    global secondary_store_data
    secondary_store_data = {
        "server_host": "10.0.0.5",
        "server_port": "9090"
    }

    schema = {
        "server": {
            "host": {"type": "string", "default": "localhost", "secondary-store": "server_host"},
            "port": {"type": "integer", "default": "8080", "secondary-store": "server_port"}
        }
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py', '--secondary-store', 'http://localhost:6400', schema_path],
            capture_output=True, text=True, timeout=5
        )
        output = json.loads(result.stdout.strip())
        expected = {"server": {"host": "10.0.0.5", "port": "9090"}}

        if output == expected:
            print("PASS: Test 6.3 Secondary-Store Seeding")
        else:
            print(f"FAIL: Test 6.3 - Got {output}, expected {expected}")
            print(f"stderr: {result.stderr}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def test_watch_mode_primary():
    """Test example 6.1: Watch Mode Seed Events with primary store."""
    global primary_store_data, primary_store_events, primary_store_cursor
    primary_store_data = {
        "app/host": "prod.example.com"
    }
    primary_store_events = []
    primary_store_cursor = 0

    schema = {
        "server": {
            "host": {"type": "string", "default": "localhost", "primary-store": "app/host"},
            "port": {"type": "integer", "default": "8080"}
        },
        "verbose": {"type": "boolean", "default": "false", "primary-store": "app/verbose"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        # Start the subprocess with unbuffered output
        proc = subprocess.Popen(
            ['python', '-u', '/workspace/cfgpipe.py', '--primary-store', 'http://localhost:8500', '--watch', schema_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        # Read output with a timeout - we expect seed events + config quickly
        lines = []
        start_time = time.time()

        while time.time() - start_time < 2:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
                if len(lines) >= 4:
                    break

        proc.terminate()
        proc.wait(timeout=1)

        # Should have 3 seed events + 1 config line
        if len(lines) < 4:
            print(f"FAIL: Test 6.1 - Expected 4 lines, got {len(lines)}")
            print(f"Output: {lines}")
            return False

        # Check seed events (first 3 lines)
        events = [json.loads(line) for line in lines[:3]]
        config = json.loads(lines[3])

        # Check events have correct structure
        for evt in events:
            if 'path' not in evt or 'type' not in evt or 'previous' not in evt or 'current' not in evt:
                print(f"FAIL: Test 6.1 - Event missing required field: {evt}")
                return False
            if evt['previous'] != '':
                print(f"FAIL: Test 6.1 - Seed event should have previous='': {evt}")
                return False

        # Check config
        expected_config = {"server": {"host": "prod.example.com", "port": "8080"}, "verbose": "false"}
        if config != expected_config:
            print(f"FAIL: Test 6.1 - Config mismatch: {config} vs {expected_config}")
            return False

        print("PASS: Test 6.1 Watch Mode Seed Events")
    finally:
        os.unlink(schema_path)

    return True

def test_priority():
    """Test that secondary-store overrides primary-store."""
    global primary_store_data, secondary_store_data
    primary_store_data = {"key1": "primary_value"}
    secondary_store_data = {"key2": "secondary_value"}

    schema = {
        "a": {"type": "string", "default": "default_a", "primary-store": "key1"},
        "b": {"type": "string", "default": "default_b", "secondary-store": "key2"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py',
             '--primary-store', 'http://localhost:8500',
             '--secondary-store', 'http://localhost:6400',
             schema_path],
            capture_output=True, text=True, timeout=5
        )
        output = json.loads(result.stdout.strip())
        expected = {"a": "primary_value", "b": "secondary_value"}

        if output == expected:
            print("PASS: Priority test")
        else:
            print(f"FAIL: Priority test - Got {output}, expected {expected}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def test_arg_override():
    """Test that arg overrides all other sources."""
    global primary_store_data, secondary_store_data
    primary_store_data = {"key1": "primary_value"}
    secondary_store_data = {"key1": "secondary_value"}

    schema = {
        "a": {"type": "string", "default": "default", "primary-store": "key1", "secondary-store": "key1", "arg": "a"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py',
             '--primary-store', 'http://localhost:8500',
             '--secondary-store', 'http://localhost:6400',
             schema_path, '--a=arg_value'],
            capture_output=True, text=True, timeout=5
        )
        output = json.loads(result.stdout.strip())

        if output == {"a": "arg_value"}:
            print("PASS: Arg override test")
        else:
            print(f"FAIL: Arg override test - Got {output}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def test_port_type():
    """Test port type parsing and validation."""
    schema = {
        "http_port": {"type": "port", "default": "80"},
        "https_port": {"type": "port", "default": "443"},
        "custom_port": {"type": "port", "default": "8080"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py', schema_path],
            capture_output=True, text=True, timeout=5
        )
        output = json.loads(result.stdout.strip())
        expected = {"http_port": "80", "https_port": "443", "custom_port": "8080"}

        if output == expected:
            print("PASS: Port type test")
        else:
            print(f"FAIL: Port type test - Got {output}, expected {expected}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def test_float_formatting():
    """Test float formatting with 6 decimal places."""
    schema = {
        "ratio": {"type": "float", "default": "0.5"},
        "price": {"type": "float", "default": "19.99"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py', schema_path],
            capture_output=True, text=True, timeout=5
        )
        output = json.loads(result.stdout.strip())
        expected = {"ratio": "0.500000", "price": "19.990000"}

        if output == expected:
            print("PASS: Float formatting test")
        else:
            print(f"FAIL: Float formatting test - Got {output}, expected {expected}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def test_error_cases():
    """Test various error conditions."""
    # Test: secondary-store declared but not configured
    schema = {
        "a": {"type": "string", "secondary-store": "key1"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py', schema_path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 and 'secondary-store' in result.stderr:
            print("PASS: Missing secondary-store URL error")
        else:
            print(f"FAIL: Missing secondary-store URL error - got code {result.returncode}, stderr: {result.stderr}")
            return False
    finally:
        os.unlink(schema_path)

    # Test: duplicate secondary-store keys
    schema = {
        "a": {"type": "string", "secondary-store": "dup_key", "default": "x"},
        "b": {"type": "string", "secondary-store": "dup_key", "default": "y"}
    }

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(schema, f)
        schema_path = f.name

    try:
        result = subprocess.run(
            ['python', '/workspace/cfgpipe.py', '--secondary-store', 'http://localhost:6400', schema_path],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0 and 'duplicate secondary-store key' in result.stderr:
            print("PASS: Duplicate secondary-store key error")
        else:
            print(f"FAIL: Duplicate secondary-store key error - got code {result.returncode}, stderr: {result.stderr}")
            return False
    finally:
        os.unlink(schema_path)

    return True

def main():
    # Start mock servers
    primary_thread = threading.Thread(target=run_primary_store, daemon=True)
    secondary_thread = threading.Thread(target=run_secondary_store, daemon=True)

    primary_thread.start()
    secondary_thread.start()

    # Wait for servers to start
    time.sleep(1)

    tests = [
        ("Non-watch mode", test_non_watch_mode),
        ("Watch mode primary", test_watch_mode_primary),
        ("Priority", test_priority),
        ("Arg override", test_arg_override),
        ("Port type", test_port_type),
        ("Float formatting", test_float_formatting),
        ("Error cases", test_error_cases),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        try:
            if test_fn():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"FAIL: {name} - Exception: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
