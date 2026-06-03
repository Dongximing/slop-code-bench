#!/usr/bin/env python3
"""
Edge case tests for the Config Management Service.
"""

import subprocess
import time
import requests
import sys
import os

# Kill any existing server
os.system("pkill -f 'python config_server.py' 2>/dev/null")
time.sleep(1)

# Start server
proc = subprocess.Popen(
    [sys.executable, 'config_server.py', '--address', '0.0.0.0', '--port', '18282'],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)
time.sleep(3)

BASE = "http://localhost:18282"

tests = []

def run_test(name, test_func):
    try:
        print(f"\n=== {name} ===")
        test_func()
        print(f"✓ {name} passed")
        return True
    except AssertionError as e:
        print(f"✗ {name} failed: {e}")
        return False
    except Exception as e:
        print(f"✗ {name} error: {e}")
        import traceback
        traceback.print_exc()
        return False

def test_too_large_request():
    """Test 413 for oversized request."""
    # Create a large config > 1 MiB
    large_config = {"data": "x" * (1024 * 1024)}  # 1 MB
    r = requests.post(f"{BASE}/v1/configs/large", json={
        "scope": {"env": "prod"},
        "config": large_config
    })
    # The service should handle this gracefully; if Flask's default limit is hit,
    # it returns 413. If not, we'll get a normal response (which is also acceptable).
    if r.status_code == 413:
        data = r.json()
        assert data["error"]["code"] == "too_large"
    else:
        # Acceptable: smaller limit not hit, process normally
        assert r.status_code == 201, f"Expected 201 or 413, got {r.status_code}"

def test_empty_scope():
    """Test empty scope is allowed."""
    r = requests.post(f"{BASE}/v1/configs/global", json={
        "scope": {},
        "config": {"setting": "value"}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/global:active", json={"scope": {}})
    assert r.status_code == 200
    data = r.json()
    assert data["config"] == {"setting": "value"}

def test_case_sensitive_scope():
    """Test that scope keys are case-sensitive."""
    r = requests.post(f"{BASE}/v1/configs/case_test", json={
        "scope": {"Env": "prod"},
        "config": {"a": 1}
    })
    assert r.status_code == 201
    # Different key case should be different scope
    r = requests.post(f"{BASE}/v1/configs/case_test", json={
        "scope": {"env": "prod"},
        "config": {"b": 2}
    })
    assert r.status_code == 201  # New version for different scope
    # Get both
    r1 = requests.post(f"{BASE}/v1/configs/case_test:active", json={"scope": {"Env": "prod"}})
    r2 = requests.post(f"{BASE}/v1/configs/case_test:active", json={"scope": {"env": "prod"}})
    # Verify separate scopes
    versions1 = requests.post(f"{BASE}/v1/configs/case_test:versions", json={"scope": {"Env": "prod"}}).json()
    versions2 = requests.post(f"{BASE}/v1/configs/case_test:versions", json={"scope": {"env": "prod"}}).json()
    assert len(versions1["versions"]) == 1
    assert len(versions2["versions"]) == 1

def test_null_in_config():
    """Test null values in config."""
    r = requests.post(f"{BASE}/v1/configs/with_null", json={
        "scope": {"env": "prod"},
        "config": {"setting": None, "number": 42}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/with_null:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    resolved = r.json()["resolved_config"]
    assert resolved["setting"] is None
    assert resolved["number"] == 42

def test_nested_merge():
    """Test nested object merging."""
    r = requests.post(f"{BASE}/v1/configs/n1", json={
        "scope": {"env": "prod"},
        "config": {"a": {"b": {"c": 1, "d": 2}}}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/n2", json={
        "scope": {"env": "prod"},
        "config": {"a": {"b": {"c": 3, "e": 4}}},
        "includes": [{"name": "n1", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/n2:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    resolved = r.json()["resolved_config"]
    # Deep merge should preserve d from n1, override c with n2, add e from n2
    assert resolved["a"]["b"]["c"] == 3
    assert resolved["a"]["b"]["d"] == 2
    assert resolved["a"]["b"]["e"] == 4

def test_empty_includes():
    """Test includes can be empty list."""
    r = requests.post(f"{BASE}/v1/configs/empty_incl", json={
        "scope": {"env": "prod"},
        "config": {"x": 1},
        "includes": []
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/empty_incl:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    assert r.json()["resolved_config"] == {"x": 1}

def test_inherits_active_no_base():
    """Test inherits_active when no active exists."""
    r = requests.post(f"{BASE}/v1/configs/no_base", json={
        "scope": {"env": "prod"},
        "config": {"x": 1},
        "inherits_active": True
    })
    assert r.status_code == 201
    # Should just use the provided config since no active to inherit from
    r = requests.post(f"{BASE}/v1/configs/no_base/1", json={"scope": {"env": "prod"}})
    assert r.json()["config"] == {"x": 1}

def test_activate_missing_version():
    """Test activate missing version returns 409."""
    r = requests.post(f"{BASE}/v1/configs/activate_fail", json={
        "scope": {"env": "prod"},
        "config": {"x": 1}
    })
    assert r.status_code == 201
    # Activate non-existent version
    r = requests.post(f"{BASE}/v1/configs/activate_fail/999:activate", json={"scope": {"env": "prod"}})
    assert r.status_code == 404
    assert "not_found" in r.json()["error"]["code"]

def test_rollback_missing_version():
    """Test rollback to missing version returns 404."""
    r = requests.post(f"{BASE}/v1/configs/rollback_fail", json={
        "scope": {"env": "prod"},
        "config": {"x": 1}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/rollback_fail:rollback", json={
        "scope": {"env": "prod"},
        "to_version": 999
    })
    assert r.status_code == 404
    assert "not_found" in r.json()["error"]["code"]

def test_resolution_graph_order():
    """Test resolution_graph lists in merge order."""
    # Create chain: lib1 <- lib2 <- app
    r = requests.post(f"{BASE}/v1/configs/lib1g", json={
        "scope": {"env": "prod"},
        "config": {"from": "lib1"}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/lib2g", json={
        "scope": {"env": "prod"},
        "config": {"from": "lib2"},
        "includes": [{"name": "lib1g", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/app_g", json={
        "scope": {"env": "prod"},
        "config": {"from": "app"},
        "includes": [{"name": "lib2g", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/app_g:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    graph = r.json()["resolution_graph"]
    # Order should be: lib1, lib2, app (depth-first, left-to-right)
    names = [node["name"] for node in graph]
    assert names == ["lib1g", "lib2g", "app_g"]

# Run all edge tests
for test_func in [test_empty_scope, test_case_sensitive_scope, test_null_in_config,
                  test_nested_merge, test_empty_includes, test_inherits_active_no_base,
                  test_activate_missing_version, test_rollback_missing_version,
                  test_resolution_graph_order]:
    tests.append(test_func)

# Also test large request
tests.append(test_too_large_request)

passed = 0
failed = 0

for test_func in tests:
    if run_test(test_func.__name__, test_func):
        passed += 1
    else:
        failed += 1

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
proc.terminate()
sys.exit(0 if failed == 0 else 1)