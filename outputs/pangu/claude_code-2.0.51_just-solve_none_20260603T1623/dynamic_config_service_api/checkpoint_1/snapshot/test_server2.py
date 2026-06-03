#!/usr/bin/env python3
"""
Comprehensive test for the config server
Run with: PYTHONPATH=/workspace/lib python3 test_server2.py
"""

import sys
import subprocess
import json
import time
import requests
import threading
import signal
import os

BASE_URL = 'http://localhost:18080'
server_process = None

def start_server():
    """Start the server in a subprocess."""
    global server_process
    env = os.environ.copy()
    env['PYTHONPATH'] = '/workspace/lib:/workspace/lib2'
    server_process = subprocess.Popen(
        [sys.executable, 'config_server.py', '--port', '18080', '--address', '127.0.0.1'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd='/workspace'
    )
    # Wait for server to start
    time.sleep(2)
    # Check it's responding
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

def test_api():
    """Test the API endpoints."""

    print("\n=== Test 1: Health check ===")
    resp = requests.get(f'{BASE_URL}/healthz')
    assert resp.status_code == 200
    assert resp.json()['ok'] == True
    print("PASS")

    print("\n=== Test 2: Create base config ===")
    body = {
        "scope": {"env": "prod"},
        "config": {"db": {"host": "prod.db", "pool": 4}}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/base', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['name'] == 'base'
    assert result['scope'] == {"env": "prod"}
    assert result['version'] == 1
    assert result['active'] == True
    print(f"PASS: Created version {result['version']}")

    print("\n=== Test 3: Create billing config with includes ===")
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "includes": [{"name": "base", "scope": {"env": "prod"}}],
        "config": {"db": {"pool": 8}, "feature_flags": ["a", "b"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['name'] == 'billing'
    assert result['version'] == 1
    assert result['active'] == True
    print(f"PASS: Created version {result['version']}")

    print("\n=== Test 4: Resolve billing config ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:resolve', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert 'resolved_config' in result
    assert 'resolution_graph' in result
    assert result['resolved_config']['db']['host'] == 'prod.db'
    assert result['resolved_config']['db']['pool'] == 8
    assert result['resolved_config']['feature_flags'] == ['a', 'b']
    # Check graph order
    assert len(result['resolution_graph']) == 2
    # First should be billing (the one being resolved), second should be base
    assert result['resolution_graph'][0]['name'] == 'billing'
    assert result['resolution_graph'][1]['name'] == 'base'
    print(f"PASS: Resolved config with {len(result['resolution_graph'])} nodes")

    print("\n=== Test 5: List versions ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:versions', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert len(result['versions']) == 1
    assert result['versions'][0]['version'] == 1
    assert result['versions'][0]['active'] == True
    print("PASS")

    print("\n=== Test 6: Get active version ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:active', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    assert 'config' in result
    assert 'includes' in result
    print("PASS")

    print("\n=== Test 7: Create second version ===")
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "config": {"db": {"pool": 10}, "feature_flags": ["a", "b", "c"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 2
    assert result['active'] == True
    print(f"PASS: Created version {result['version']}")

    print("\n=== Test 8: List versions after new version ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:versions', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert len(result['versions']) == 2
    assert result['versions'][0]['version'] == 1
    assert result['versions'][0]['active'] == False
    assert result['versions'][1]['version'] == 2
    assert result['versions'][1]['active'] == True
    print("PASS")

    print("\n=== Test 9: Activate version 1 ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/1:activate', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['active'] == True
    assert result['version'] == 1
    print("PASS")

    print("\n=== Test 10: Check active version is now 1 ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:active', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    assert result['active'] == True
    # Version 1 config should have includes
    assert 'includes' in result
    print("PASS")

    print("\n=== Test 11: Rollback to version 2 (should fail - not earlier) ===")
    body = {"scope": {"env": "prod", "app": "billing"}, "to_version": 2}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:rollback', json=body)
    assert resp.status_code == 409, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'conflict'
    print("PASS")

    print("\n=== Test 12: Idempotent rollback to same version (1) ===")
    body = {"scope": {"env": "prod", "app": "billing"}, "to_version": 1}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing:rollback', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['active'] == True
    assert result['version'] == 1
    print("PASS")

    print("\n=== Test 13: Get specific version 1 (should be active) ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/1', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 1
    # Should be active since we just activated/rolled back to it
    assert result['active'] == True
    print("PASS")

    print("\n=== Test 14: Get specific version 2 (should NOT be active) ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/2', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 2
    assert result['active'] == False
    print("PASS")

    print("\n=== Test 15: Idempotency check - same config body ===")
    body = {
        "scope": {"env": "prod", "app": "billing"},
        "includes": [{"name": "base", "scope": {"env": "prod"}}],
        "config": {"db": {"pool": 8}, "feature_flags": ["a", "b"]}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/billing', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    # Should return version 1 (same body), not create version 3
    assert result['version'] == 1, f"Expected version 1, got {result['version']}"
    assert result['active'] == True
    print(f"PASS: Returned existing version {result['version']}")

    print("\n=== Test 16: Error - missing scope ===")
    body = {"config": {"foo": "bar"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/test', json=body)
    assert resp.status_code == 400, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert 'error' in result
    assert result['error']['code'] == 'invalid_input'
    print("PASS")

    print("\n=== Test 17: Error - get non-existent version ===")
    body = {"scope": {}}
    resp = requests.post(f'{BASE_URL}/v1/configs/test/999', json=body)
    assert resp.status_code == 404, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert 'error' in result
    assert result['error']['code'] == 'not_found'
    print("PASS")

    print("\n=== Test 18: Error - activate non-existent version ===")
    body = {"scope": {"env": "prod", "app": "billing"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/billing/999:activate', json=body)
    assert resp.status_code == 409, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert 'error' in result
    assert result['error']['code'] == 'conflict'
    print("PASS")

    print("\n=== Test 19: Cycle detection ===")
    body1 = {
        "scope": {"env": "prod"},
        "config": {"a": 1},
        "includes": [{"name": "cycle2", "scope": {"env": "prod"}}]
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle1', json=body1)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    body2 = {
        "scope": {"env": "prod"},
        "config": {"b": 2},
        "includes": [{"name": "cycle1", "scope": {"env": "prod"}}]
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle2', json=body2)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    # Try to resolve cycle1 - should detect cycle
    body = {"scope": {"env": "prod"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/cycle1:resolve', json=body)
    assert resp.status_code == 409, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'cycle_detected'
    print("PASS")

    print("\n=== Test 20: Type conflict detection ===")
    body = {
        "scope": {"env": "prod"},
        "config": {"a": {"b": "string"}}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/type1', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    body = {
        "scope": {"env": "prod"},
        "config": {"a": {"b": 123}}  # Different type for 'a.b'
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/type2', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    body = {
        "scope": {"env": "prod"},
        "includes": [
            {"name": "type1", "scope": {"env": "prod"}},
            {"name": "type2", "scope": {"env": "prod"}}
        ],
        "config": {}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/type3', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    body = {"scope": {"env": "prod"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/type3:resolve', json=body)
    assert resp.status_code == 422, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['error']['code'] == 'unprocessable'
    assert 'type_conflict' in result['error']['details'].get('reason', '')
    print("PASS")

    print("\n=== Test 21: Inherits_active ===")
    body = {
        "scope": {"env": "prod"},
        "config": {"parent_field": "value", "another": 42}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/parent', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"

    body = {
        "scope": {"env": "prod"},
        "config": {"parent_field": "overridden"},  # Should inherit 'another' from active
        "inherits_active": True
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/parent', json=body)
    assert resp.status_code == 201, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    assert result['version'] == 2

    body = {"scope": {"env": "prod"}, "version": 2}
    resp = requests.post(f'{BASE_URL}/v1/configs/parent:resolve', json=body)
    assert resp.status_code == 200, f"Got {resp.status_code}: {resp.text}"
    result = resp.json()
    # parent_field should be overridden, another should be inherited
    print(f"    Resolved: {result['resolved_config']}")
    assert result['resolved_config']['parent_field'] == 'overridden'
    assert result['resolved_config']['another'] == 42
    print("PASS")

    print("\n=== Test 22: Max versions ===")
    # Create 10002 versions - should hit limit on 10001
    for i in range(2, 10003):
        body = {
            "scope": {"env": f"prod{i}"},
            "config": {"v": i}
        }
        resp = requests.post(f'{BASE_URL}/v1/configs/maxv', json=body)
        if i == 10001:
            assert resp.status_code == 201, f"Failed at {i}: {resp.status_code} {resp.text}"
        elif i > 10001:
            # Should hit limit
            assert resp.status_code == 409, f"Should have hit limit at {i}: {resp.status_code} {resp.text}"
            result = resp.json()
            assert result['error']['code'] == 'conflict'
            print(f"    Limit hit at version {i}")
            break
    print("PASS")

    print("\n=== Test 23: Deep merge with null ===")
    body = {
        "scope": {"env": "prod"},
        "config": {"a": None}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/null1', json=body)
    assert resp.status_code == 201

    body = {
        "scope": {"env": "prod"},
        "includes": [{"name": "null1", "scope": {"env": "prod"}}],
        "config": {"a": "value"}
    }
    resp = requests.post(f'{BASE_URL}/v1/configs/null2', json=body)
    assert resp.status_code == 201

    body = {"scope": {"env": "prod"}}
    resp = requests.post(f'{BASE_URL}/v1/configs/null2:resolve', json=body)
    assert resp.status_code == 200
    result = resp.json()
    assert result['resolved_config']['a'] == 'value'
    print("PASS")


if __name__ == '__main__':
    try:
        start_server()
        test_api()
        print("\n✓ All 23 tests passed!")
    finally:
        stop_server()
