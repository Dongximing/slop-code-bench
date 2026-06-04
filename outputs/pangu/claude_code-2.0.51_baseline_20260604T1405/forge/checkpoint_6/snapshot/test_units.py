#!/usr/bin/env python3
"""Comprehensive tests for the forge unit inventory."""

import json
import os
import sys
import tempfile
import subprocess

def run_forge(cmd, stdin=None):
    """Run forge and return (output, exit_code)."""
    args = ["python", "forge.py", "--data-dir", sys.argv[1]] + cmd
    result = subprocess.run(args, capture_output=True, input=stdin, text=True)
    return result.stdout.strip(), result.returncode

def test_create_unit():
    """Test creating a unit."""
    stdin = json.dumps({
        "category": "gpu",
        "manufacturer": "nvidia",
        "host": "server1",
        "capacity": 4
    })
    output, exit_code = run_forge(["unit", "create"], stdin)
    assert exit_code == 0, f"Create failed: {output}"
    unit = json.loads(output)
    assert unit["category"] == "gpu"
    assert unit["manufacturer"] == "nvidia"
    assert unit["host"] == "server1"
    assert unit["status"] == "active"
    assert unit["parent_uuid"] is None
    assert unit["module_uuids"] == []
    assert unit["capacity"] == 4
    assert "uuid" in unit
    assert "created_at" in unit
    return unit

def test_create_unit_with_parent():
    """Test creating a unit with parent_uuid."""
    # Create parent
    parent_input = json.dumps({
        "category": "parent",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 10
    })
    parent_output, _ = run_forge(["unit", "create"], parent_input)
    parent_unit = json.loads(parent_output)
    parent_uuid = parent_unit["uuid"]

    # Create child
    child_input = json.dumps({
        "category": "child",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 5,
        "parent_uuid": parent_uuid
    })
    child_output, exit_code = run_forge(["unit", "create"], child_input)
    assert exit_code == 0, f"Create with parent failed: {child_output}"
    child_unit = json.loads(child_output)
    assert child_unit["parent_uuid"] == parent_uuid

def test_list_units():
    """Test listing all units."""
    output, exit_code = run_forge(["unit", "list"])
    assert exit_code == 0, f"List failed: {output}"
    units = json.loads(output)
    assert isinstance(units, list)
    # Should have at least the parent and child units
    assert len(units) >= 2

def test_list_filter_category():
    """Test listing with category filter."""
    output, exit_code = run_forge(["unit", "list", "--category", "gpu"])
    assert exit_code == 0
    units = json.loads(output)
    for unit in units:
        assert unit["category"] == "gpu"

def test_list_filter_manufacturer():
    """Test listing with manufacturer filter."""
    output, exit_code = run_forge(["unit", "list", "--manufacturer", "nvidia"])
    assert exit_code == 0
    units = json.loads(output)
    for unit in units:
        assert unit["manufacturer"] == "nvidia"

def test_list_filter_host():
    """Test listing with host filter."""
    output, exit_code = run_forge(["unit", "list", "--host", "server1"])
    assert exit_code == 0
    units = json.loads(output)
    for unit in units:
        assert unit["host"] == "server1"

def test_list_filter_generic():
    """Test listing with generic filter."""
    output, exit_code = run_forge(["unit", "list", "--filter", "category=cpu"])
    assert exit_code == 0
    units = json.loads(output)
    for unit in units:
        assert unit["category"] == "cpu"

def test_list_filter_combination():
    """Test listing with combination of filters."""
    output, exit_code = run_forge([
        "unit", "list", "--category", "gpu",
        "--filter", "manufacturer=nvidia"
    ])
    assert exit_code == 0
    units = json.loads(output)
    for unit in units:
        assert unit["category"] == "gpu"
        assert unit["manufacturer"] == "nvidia"

def test_list_filter_nonexistent_field():
    """Test listing with filter on nonexistent field returns empty."""
    output, exit_code = run_forge(["unit", "list", "--filter", "nonexistent=value"])
    assert exit_code == 0
    units = json.loads(output)
    assert units == []

def test_list_filter_invalid_format():
    """Test listing with invalid filter format (no =) fails."""
    output, exit_code = run_forge(["unit", "list", "--filter", "invalid"])
    assert exit_code == 1, f"Should fail with validation_error: {output}"
    error = json.loads(output)
    assert error["error"] == "validation_error"

def test_get_unit():
    """Test getting a specific unit."""
    # First get a UUID from create
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 20
    })
    output, _ = run_forge(["unit", "create"], input_data)
    unit = json.loads(output)
    uuid = unit["uuid"]

    output, exit_code = run_forge(["unit", "get", uuid])
    assert exit_code == 0, f"Get failed: {output}"
    retrieved = json.loads(output)
    assert retrieved["uuid"] == uuid

def test_get_nonexistent():
    """Test getting a nonexistent unit returns error."""
    output, exit_code = run_forge(["unit", "get", "non-existent-uuid"])
    assert exit_code == 1, f"Should fail with not_found: {output}"
    error = json.loads(output)
    assert error["error"] == "not_found"

def test_validation_empty_category():
    """Test validation rejects empty category."""
    input_data = json.dumps({
        "category": "",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 4
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1, f"Should fail with validation_error: {output}"
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "category must be a non-empty string" in error["detail"]

def test_validation_empty_manufacturer():
    """Test validation rejects empty manufacturer."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "",
        "host": "server1",
        "capacity": 4
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "manufacturer must be a non-empty string" in error["detail"]

def test_validation_empty_host():
    """Test validation rejects empty host."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "",
        "capacity": 4
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "host must be a non-empty string" in error["detail"]

def test_validation_negative_capacity():
    """Test validation rejects negative capacity."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": -1
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "capacity must be a non-negative integer" in error["detail"]

def test_validation_boolean_capacity():
    """Test validation rejects boolean capacity."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": True
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "capacity must be a non-negative integer" in error["detail"]

def test_validation_parent_not_found():
    """Test validation rejects nonexistent parent_uuid."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 4,
        "parent_uuid": "non-existent-uuid"
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "not_found"
    assert "Parent unit" in error["detail"]

def test_validation_parent_empty_string():
    """Test validation rejects empty string parent_uuid."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 4,
        "parent_uuid": ""
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "parent_uuid must be a non-empty string" in error["detail"]

def test_validation_parent_not_string():
    """Test validation rejects non-string parent_uuid."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 4,
        "parent_uuid": 123
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "parent_uuid must be a non-empty string" in error["detail"]

def test_validation_system_fields():
    """Test validation rejects system-assigned fields."""
    input_data = json.dumps({
        "category": "test",
        "manufacturer": "test",
        "host": "server1",
        "capacity": 4,
        "uuid": "fake-uuid",
        "status": "fake-status",
        "module_uuids": [],
        "created_at": "fake-date"
    })
    output, exit_code = run_forge(["unit", "create"], input_data)
    assert exit_code == 1
    error = json.loads(output)
    assert error["error"] == "validation_error"
    assert "system-assigned" in error["detail"]

def test_ordering():
    """Test that results are ordered correctly."""
    # Create units in various orders
    input_data = json.dumps({
        "category": "A",
        "manufacturer": "Z",
        "host": "z",
        "capacity": 1
    })
    output, _ = run_forge(["unit", "create"], input_data)
    unit_a = json.loads(output)

    input_data = json.dumps({
        "category": "B",
        "manufacturer": "A",
        "host": "a",
        "capacity": 1
    })
    output, _ = run_forge(["unit", "create"], input_data)
    unit_b = json.loads(output)

    output, _ = run_forge(["unit", "list"])
    units = json.loads(output)

    # Verify ordering (host, category, created_at, uuid)
    # Since hosts are different, it should be: a, z
    hosts_seen = []
    for unit in units:
        if unit["category"] in ["A", "B"]:
            hosts_seen.append(unit["host"])

    assert hosts_seen == sorted(hosts_seen), f"Order check: {hosts_seen}"

def main(tmpdir):
    """Run all tests."""
    tests = [
        test_create_unit,
        test_create_unit_with_parent,
        test_list_units,
        test_list_filter_category,
        test_list_filter_manufacturer,
        test_list_filter_host,
        test_list_filter_generic,
        test_list_filter_combination,
        test_list_filter_nonexistent_field,
        test_list_filter_invalid_format,
        test_get_unit,
        test_get_nonexistent,
        test_validation_empty_category,
        test_validation_empty_manufacturer,
        test_validation_empty_host,
        test_validation_negative_capacity,
        test_validation_boolean_capacity,
        test_validation_parent_not_found,
        test_validation_parent_empty_string,
        test_validation_parent_not_string,
        test_validation_system_fields,
        test_ordering,
    ]

    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            sys.exit(1)

    print(f"All {len(tests)} tests passed!")

if __name__ == "__main__":
    tmpdir = tempfile.mkdtemp()
    main(tmpdir)
