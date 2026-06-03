#!/usr/bin/env python3
"""
Test the config server with the minimal example from the spec.
Run with: PYTHONPATH=/workspace/lib:/workspace/lib2 python3 test_server.py
"""

import sys
import subprocess
import json
import time
import requests
import os

BASE_URL = 'http://localhost:19080'
server_process = None

def start_server():
    """Start the server in a subprocess."""
    global server_process
    env = os.environ.copy()
    env['PYTHONPATH'] = '/workspace/lib:/workspace/lib2'
    server_process = subprocess.Popen(
        [sys.executable, 'config_server.py', '--port', '19080', '--address', '127.0.0.1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd='/workspace'
    )
    time.sleep(2)
    for _ in range(10):
        try:
            resp = requests.get(f'{BASE_URL}/healthz', timeout=1)
            if resp.status_code == 200:
                print('Server started successfully')
                return
        except:
            pass
        time.sleep(0.5)
    raise RuntimeError('Server failed to start')

def stop_server():
    """Stop the server."""
    global server_process
    if server_process:
        server_process.terminate()
        try:
            server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_process.kill()
        print('Server stopped')

def test_spec_example():
    """Test the minimal example from the spec."""

    print("\n=== Test 1: Health check ===")
    resp = requests.get(f'{BASE_URL}/healthz')
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    assert resp.json()['ok'] == True
    print("PASS")

    print("\n=== Test 2: Create base config ===")
    body = {
        "scope": {"env": "prod"},
        "config": {"db": {"host": "prod.db", "pool": 4}}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/base', json=body)
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['name'] == 'base'
    assert result['scope'] == {"env": "prod"}
    assert result['version'] == 1
    assert result['active'] == True
    print("PASS: Created base config version 1")

    print("\n=== Test 3: Create app inheriting base ===")
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "includes": [{"name": "base", "scope": {"env": "prod"}}],
        "config": {"db": {"pool": 8}, "feature_flags": ["a", "b"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['name'] == 'billing'
    assert result['version'] == 1
    assert result['active'] == True
    print("PASS: Created billing config version 1")

    print("\n=== Test 4: Resolve app (from spec) ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:resolve', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()

    # Expected from spec:
    # "db": {"host":"prod.db", "pool":8},
    # "feature_flags": ["a","b"]
    expected = {
        "db": {"host": "prod.db", "pool": 8},
        "feature_flags": ["a", "b"]
    }
    assert result['resolved_config'] == expected, f"Expected {expected}, got {result['resolved_config']}"
    print("PASS: Resolved config matches spec")

    print("\n=== Test 5: List versions ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:versions', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert len(result['versions']) == 1
    assert result['versions'][0]['version'] == 1
    assert result['versions'][0]['active'] == True
    print("PASS")

    print("\n=== Test 6: Get active version ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:active', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    assert 'config' in result
    assert 'includes' in result
    print("PASS")

    print("\n=== Test 7: Activate a specific version ===")
    # First create another version
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "config": {"db": {"pool": 12}, "feature_flags": ["a", "b", "c"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201
    result = resp.json()
    v2 = result['version']

    # Now activate version 1
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/1:activate', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    print(f"PASS: Activated version 1")

    print("\n=== Test 8: Get specific raw version ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/1', json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result['version'] == 1
    assert 'config' in result
    assert 'includes' in result
    print("PASS")

    print("\n=== Test 9: Rollback ===")
    body = {"scope": {"env": "prod", "app": "billing"}, "to_version": 1}
    # Current active is version 1 (after Test 7), so this should be idempotent
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:rollback', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    print("PASS: Idempotent rollback to version 1")

    # Now create version 3 to have something earlier to rollback to
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "config": {"db": {"pool": 15}, "feature_flags": ["a", "b", "c", "d"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201
    result = resp.json()
    v3 = result['version']
    print(f"Created version {v3}")

    # Now rollback from version 3 to version 1
    body = {"scope": {"env": "prod", "app": "billing"}, "to_version": 1}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:rollback', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    print(f"PASS: Rollback from version {v3} to version 1")

    print("\n=== Test 10: Idempotency - same config body ===")
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "includes": [{"name": "base", "scope": {"env": "prod"}}],
        "config": {"db": {"pool": 8}, "feature_flags": ["a", "b"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    result = resp.json()
    # Should return version 1 (same body), not create a new one
    assert result['version'] == 1, f"Expected version 1, got {result['version']}"
    print("PASS")

    print("\n=== Test 11: Error handling ===")
    # Test missing scope
    body = {"config": {"foo": "bar"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/test', json=body)
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'invalid_input'
    print("PASS")

    # Test 404 on non-existent version
    body = {"scope": {}}
    resp = requests.post(f'{BASE_URL}/v1/configs/test/999', json=body)
    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'not_found'
    print("PASS")

    print("\n=== Test 12: Cycle detection ===")
    # Create mutually recursive includes: A includes B, B includes A
    body = {
        "scope": {"env": "prod"},
        "config": {"a": 1},
        "includes": [{"name": "cycle2", "scope": {"env": "prod"}}]
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle1', json=body)
    assert resp.status_code == 201

    body = {
        "scope": {"env": "prod"},
        "config": {"b": 2},
        "includes": [{"name": "cycle1", "scope": {"env": "prod"}}]
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle2', json=body)
    assert resp.status_code == 201

    # Resolve - should detect cycle
    body = {"scope": {"env": "prod"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle1:resolve', json=body)
    assert resp.status_code == 409, f"Expected 409, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'cycle_detected'
    print("PASS")

    print("\n=== Test 13: Type conflict ===")
    body = {"scope": {"env": "prod"}, "config": {"a": {"b": "string"}}}
    resp = requests.post(f'{BASE_URL}/v1/configs/type1', json=body)
    assert resp.status_code == 201

    body = {"scope": {"env": "prod"}, "config": {"a": {"b": 123}}}
    resp = requests.post(f'{BASE_URL}/v1/configs/type2', json=body)
    assert resp.status_code == 201

    body = {
        "scope": {"env": "prod"},
        "includes": [
            {"name": "type1", "scope": {"env": "prod"}},
            {"name": "type2", "scope": {"env": "prod"}}
        ],
        "config": {}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/type3', json=body)
    assert resp.status_code == 201

    body = {"scope": {"env": "prod"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/type3:resolve', json=body)
    assert resp.status_code == 422, f"Expected 422, got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'unprocessable'
    print("PASS")

    print("\n=== Test 14: Inherits_active ===")
    body = {"scope": {"env": "prod"}, "config": {"parent_field": "value", "another": 42}}
    resp = requests.post(f'{BASE_URL}/v1/configs/parent', json=body)
    assert resp.status_code == 201

    body = {
        "scope": {"env": "prod"},
        "config": {"parent_field": "overridden"},
        "inherits_active": True
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/parent', json=body)
    assert resp.status_code == 201

    body = {"scope": {"env": "prod"}, "version": 2}
    resp = requests.post(f'{BASE_URL}/v1/configs/parent:resolve', json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result['resolved_config']['parent_field'] == 'overridden'
    assert result['resolved_config']['another'] == 42
    print("PASS")

    print("\n=== Test 15: dry_run option ===")
    body = {"scope": {"env": "prod"}, "version": 1, "dry_run": True}
    resp = requests.post(f'{BASE_URL}/v1/configs/base:resolve', json=body)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    print("PASS")

    # dry_run with missing version should error
    body = {"scope": {"env": "prod"}, "dry_run": True}
    resp = requests.post(f'{BASE_URL}/v1/configs/base:resolve', json=body)
    assert resp.status_code == 400, f"Expected 400, got {resp.status_code}: {resp.text}"
    print("PASS")

    print("\n" + "=" * 50)
    print("All 15 tests passed!")
    print("=" * 50)

if __name__ == '__main__':
    try:
        start_server()
        test_spec_example()
    finally:
        stop_server()
