#!/usr/bin/env python3
"""
Integration tests for the Config Management Service.
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
    [sys.executable, 'config_server.py', '--address', '0.0.0.0', '--port', '18181'],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE
)
time.sleep(3)  # Wait for server to start

BASE = "http://localhost:18181"

def run_test(name, test_func):
    """Run a single test."""
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

def test_basic_create():
    r = requests.post(f"{BASE}/v1/configs/base", json={
        "scope": {"env": "prod"},
        "config": {"db": {"host": "prod.db", "pool": 4}}
    })
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["version"] == 1 and data["active"] == True

def test_active_version():
    r = requests.post(f"{BASE}/v1/configs/base:active", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 1 and data["active"] == True

def test_activate_idempotent():
    r = requests.post(f"{BASE}/v1/configs/base/1:activate", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    r = requests.post(f"{BASE}/v1/configs/base:active", json={"scope": {"env": "prod"}})
    assert r.json()["version"] == 1

def test_second_version():
    r = requests.post(f"{BASE}/v1/configs/base", json={
        "scope": {"env": "prod"},
        "config": {"db": {"host": "prod.db", "pool": 8}}
    })
    assert r.status_code == 201
    data = r.json()
    assert data["version"] == 2 and data["active"] == True

def test_version_list():
    r = requests.post(f"{BASE}/v1/configs/base:versions", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    data = r.json()
    assert len(data["versions"]) == 2
    assert data["versions"][0]["version"] == 1
    assert data["versions"][0]["active"] == False
    assert data["versions"][1]["version"] == 2
    assert data["versions"][1]["active"] == True

def test_rollback_to_earlier():
    r = requests.post(f"{BASE}/v1/configs/base:rollback", json={
        "scope": {"env": "prod"},
        "to_version": 1
    })
    assert r.status_code == 200, r.text
    r = requests.post(f"{BASE}/v1/configs/base:active", json={"scope": {"env": "prod"}})
    assert r.json()["version"] == 1

def test_rollback_to_newer():
    r = requests.post(f"{BASE}/v1/configs/base:rollback", json={
        "scope": {"env": "prod"},
        "to_version": 2
    })
    assert r.status_code == 409, f"Expected 409, got {r.status_code}: {r.text}"
    assert "conflict" in r.json()["error"]["code"]

def test_idempotent_create():
    payload = {"scope": {"env": "prod"}, "config": {"x": 1}}
    r1 = requests.post(f"{BASE}/v1/configs/idemo", json=payload)
    assert r1.status_code == 201
    v1 = r1.json()["version"]
    r2 = requests.post(f"{BASE}/v1/configs/idemo", json=payload)
    assert r2.status_code == 201, f"Not idempotent: {r2.status_code} {r2.text}"
    v2 = r2.json()["version"]
    assert v1 == v2, f"Not idempotent: {v1} != {v2}"

def test_include_resolution():
    r = requests.post(f"{BASE}/v1/configs/lib", json={
        "scope": {"env": "prod"},
        "config": {"common": "from_lib"}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/app", json={
        "scope": {"env": "prod", "app": "api"},
        "config": {"port": 8080},
        "includes": [{"name": "lib", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/app:resolve", json={
        "scope": {"env": "prod", "app": "api"}
    })
    assert r.status_code == 200
    data = r.json()
    assert data["resolved_config"]["common"] == "from_lib"
    assert data["resolved_config"]["port"] == 8080

def test_cycle_detection():
    r = requests.post(f"{BASE}/v1/configs/a", json={
        "scope": {"env": "prod"},
        "config": {"a": 1},
        "includes": [{"name": "b", "scope": {"env": "prod"}}]
    })
    r = requests.post(f"{BASE}/v1/configs/b", json={
        "scope": {"env": "prod"},
        "config": {"b": 2},
        "includes": [{"name": "c", "scope": {"env": "prod"}}]
    })
    r = requests.post(f"{BASE}/v1/configs/c", json={
        "scope": {"env": "prod"},
        "config": {"c": 3},
        "includes": [{"name": "a", "scope": {"env": "prod"}}]
    })
    r = requests.post(f"{BASE}/v1/configs/a:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 409, f"Expected 409 cycle, got {r.status_code}"
    data = r.json()
    assert data["error"]["code"] == "cycle_detected"

def test_inherits_active():
    r = requests.post(f"{BASE}/v1/configs/active_base", json={
        "scope": {"env": "prod"},
        "config": {"common": "from_base"}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/active_base", json={
        "scope": {"env": "prod"},
        "config": {"new_field": "from_new"},
        "inherits_active": True
    })
    assert r.status_code == 201
    data = r.json()
    assert data["version"] == 2
    # Check that it inherited
    r = requests.post(f"{BASE}/v1/configs/active_base/2", json={"scope": {"env": "prod"}})
    config = r.json()["config"]
    assert config["common"] == "from_base", f"Missing inherited field: {config}"
    assert config["new_field"] == "from_new"

def test_404_not_found():
    r = requests.post(f"{BASE}/v1/configs/nonexistent/1", json={"scope": {"env": "prod"}})
    assert r.status_code == 404, f"Expected 404, got {r.status_code}"
    assert "not_found" in r.json()["error"]["code"]

def test_health_check():
    r = requests.get(f"{BASE}/healthz")
    assert r.status_code == 200, f"Health check failed: {r.status_code}"
    data = r.json()
    assert data == {"ok": True}, f"Unexpected response: {data}"

def test_overrides_includes():
    """Test that child config overrides parent."""
    r = requests.post(f"{BASE}/v1/configs/db_base", json={
        "scope": {"env": "prod"},
        "config": {"db": {"pool": 4, "host": "prod.db"}}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/web", json={
        "scope": {"env": "prod"},
        "config": {"db": {"pool": 8}},
        "includes": [{"name": "db_base", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/web:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    data = r.json()
    assert data["resolved_config"]["db"]["pool"] == 8
    assert data["resolved_config"]["db"]["host"] == "prod.db"

def test_array_replacement():
    """Test array replacement in merge."""
    r = requests.post(f"{BASE}/v1/configs/parent_arr", json={
        "scope": {"env": "prod"},
        "config": {"features": ["a", "b"]}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/child_arr", json={
        "scope": {"env": "prod"},
        "config": {"features": ["x", "y", "z"]},
        "includes": [{"name": "parent_arr", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/child_arr:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 200
    data = r.json()
    assert data["resolved_config"]["features"] == ["x", "y", "z"]

def test_type_conflict():
    """Test type conflict detection."""
    r = requests.post(f"{BASE}/v1/configs/p1", json={
        "scope": {"env": "prod"},
        "config": {"setting": "string_value"}
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/c1", json={
        "scope": {"env": "prod"},
        "config": {"setting": 123},
        "includes": [{"name": "p1", "scope": {"env": "prod"}}]
    })
    assert r.status_code == 201
    r = requests.post(f"{BASE}/v1/configs/c1:resolve", json={"scope": {"env": "prod"}})
    assert r.status_code == 422, f"Expected 422, got {r.status_code}"
    assert "unprocessable" in r.json()["error"]["code"]

# Run all tests
tests = [
    test_health_check,
    test_basic_create,
    test_active_version,
    test_activate_idempotent,
    test_second_version,
    test_version_list,
    test_rollback_to_earlier,
    test_rollback_to_newer,
    test_idempotent_create,
    test_include_resolution,
    test_overrides_includes,
    test_array_replacement,
    test_type_conflict,
    test_cycle_detection,
    test_inherits_active,
    test_404_not_found,
]

passed = 0
failed = 0

for test_func in tests:
    if run_test(test_func.__name__, test_func):
        passed += 1
    else:
        failed += 1

print(f"\n{'='*50}")
print(f"Results: {passed} passed, {failed} failed")
if failed > 0:
    proc.terminate()
    sys.exit(1)
else:
    proc.terminate()
    print("All tests passed!")
    sys.exit(0)