#!/usr/bin/env python3
"""Comprehensive test script for allocation functionality."""
import subprocess
import json
import os
import tempfile

def run_forge(data_dir, *args):
    """Run forge.py with given arguments, return stdout."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode

def run_forge_stdin(data_dir, stdin_json, *args):
    """Run forge.py with JSON from stdin."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, input=json.dumps(stdin_json), capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode

def create_test_blueprint(data_dir):
    """Create a test blueprint with requirement sets including TYPE_A."""
    print("\n=== Test: Create blueprint ===")
    blueprint_data = {
        "name": "test-blueprint",
        "requirement_sets": [
            {
                "resource_type": "vm",
                "resource_count": 2,
                "capabilities": []
            },
            {
                "resource_type": "disk",
                "resource_count": 1,
                "capabilities": ["category=TYPE_A"]
            },
            {
                "resource_type": "network",
                "resource_count": 1,
                "capabilities": ["other=value"]
            }
        ]
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, blueprint_data, "blueprint", "create")
    assert rc == 0, f"Failed to create blueprint: {stdout}"
    blueprint = json.loads(stdout)
    assert blueprint["name"] == "test-blueprint"
    assert len(blueprint["requirement_sets"]) == 3
    print("  PASS: Blueprint created")
    return blueprint

def test_allocation_create(data_dir):
    """Test creating allocations from blueprint."""
    print("\n=== Test: Create allocations ===")
    alloc_data = {"blueprint_name": "test-blueprint"}
    stdout, stderr, rc = run_forge_stdin(data_dir, alloc_data, "allocation", "create")
    assert rc == 0, f"Failed to create allocations: {stdout}"

    allocations = json.loads(stdout)

    # Check: 1 + 2 + 1 = 4 allocations (TYPE_A produces 2)
    assert len(allocations) == 4, f"Expected 4 allocations, got {len(allocations)}"

    # Check ordering: by requirement_set_index ascending
    prev_idx = -1
    for alloc in allocations:
        assert alloc["requirement_set_index"] >= prev_idx, "Not ordered by requirement_set_index"
        prev_idx = alloc["requirement_set_index"]

    # Verify TYPE_A requirement set has 2 allocations
    type_a_allocs = [a for a in allocations if a["requirement_set_index"] == 1]
    assert len(type_a_allocs) == 2, f"Expected 2 allocations for requirement_set 1 (TYPE_A), got {len(type_a_allocs)}"

    # Verify other requirement sets have 1 allocation each
    non_type_a = [a for a in allocations if a["requirement_set_index"] != 1]
    assert len(non_type_a) == 2, f"Expected 2 allocations for non-TYPE_A sets, got {len(non_type_a)}"

    print(f"  PASS: Created {len(allocations)} allocations")
    print(f"    - rs_idx=0: {[a['uuid'][:8] for a in allocations if a['requirement_set_index']==0]}")
    print(f"    - rs_idx=1 (TYPE_A): {[a['uuid'][:8] for a in allocations if a['requirement_set_index']==1]}")
    print(f"    - rs_idx=2: {[a['uuid'][:8] for a in allocations if a['requirement_set_index']==2]}")

    return allocations

def test_allocation_list(data_dir, allocations):
    """Test listing allocations."""
    print("\n=== Test: List allocations (no filter) ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "list")
    assert rc == 0, f"Failed to list allocations: {stdout}"

    listed = json.loads(stdout)
    assert len(listed) == len(allocations), f"Expected {len(allocations)} allocations, got {len(listed)}"

    # Verify all fields
    for alloc in listed:
        assert "uuid" in alloc
        assert "blueprint_name" in alloc
        assert "requirement_set_index" in alloc
        assert "binding_status" in alloc
        assert "assignment_id" in alloc
        assert alloc["binding_status"] == "unbound"
        assert alloc["assignment_id"] is None

    print(f"  PASS: Listed {len(listed)} allocations")

def test_allocation_list_filter_status(data_dir):
    """Test listing allocations with status filter."""
    print("\n=== Test: List allocations with status filter ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "list", "--status", "unbound")
    assert rc == 0, f"Failed to list allocations: {stdout}"

    listed = json.loads(stdout)
    assert all(a["binding_status"] == "unbound" for a in listed), "Not all have status unbound"
    print(f"  PASS: Filtered by status, got {len(listed)} results")

def test_allocation_list_filter_assignment(data_dir):
    """Test listing allocations with assignment filter."""
    print("\n=== Test: List allocations with assignment filter (no matches) ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "list", "--assignment", "some-assignment")
    assert rc == 0, f"Failed to list allocations: {stdout}"

    listed = json.loads(stdout)
    assert len(listed) == 0, f"Expected no allocations for non-existent assignment, got {len(listed)}"
    print("  PASS: No matches for assignment filter")

def test_allocation_get(data_dir, allocations):
    """Test getting a specific allocation."""
    print("\n=== Test: Get allocation ===")
    target_uuid = allocations[0]["uuid"]
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", target_uuid)
    assert rc == 0, f"Failed to get allocation: {stdout}"

    alloc = json.loads(stdout)
    assert alloc["uuid"] == target_uuid
    assert alloc["binding_status"] == "unbound"
    print(f"  PASS: Got allocation {alloc['uuid'][:8]}")

def test_allocation_create_nonexistent(data_dir):
    """Test creating allocation from non-existent blueprint."""
    print("\n=== Test: Create allocation from non-existent blueprint ===")
    alloc_data = {"blueprint_name": "non-existent"}
    stdout, stderr, rc = run_forge_stdin(data_dir, alloc_data, "allocation", "create")
    assert rc == 1, f"Expected exit code 1, got {rc}"

    error = json.loads(stdout)
    assert "error" in error
    assert error["error"] == "not_found"
    print("  PASS: Got expected not_found error")

def test_allocation_create_empty_name(data_dir):
    """Test creating allocation with empty blueprint_name."""
    print("\n=== Test: Allocation create with empty blueprint_name ===")
    alloc_data = {"blueprint_name": ""}
    stdout, stderr, rc = run_forge_stdin(data_dir, alloc_data, "allocation", "create")
    assert rc == 1, f"Expected exit code 1, got {rc}"

    error = json.loads(stdout)
    assert "error" in error
    assert error["error"] == "validation_error"
    assert "non-empty" in error["detail"]
    print("  PASS: Got expected validation_error")

def test_allocation_create_invalid_type(data_dir):
    """Test creating allocation with non-string blueprint_name."""
    print("\n=== Test: Allocation create with non-string blueprint_name ===")
    alloc_data = {"blueprint_name": 123}
    stdout, stderr, rc = run_forge_stdin(data_dir, alloc_data, "allocation", "create")
    assert rc == 1, f"Expected exit code 1, got {rc}"

    error = json.loads(stdout)
    assert "error" in error
    assert error["error"] == "validation_error"
    assert "string" in error["detail"]
    print("  PASS: Got expected validation_error for non-string")

def test_allocation_get_nonexistent(data_dir):
    """Test getting non-existent allocation."""
    print("\n=== Test: Get non-existent allocation ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", "00000000-0000-0000-0000-000000000000")
    assert rc == 1, f"Expected exit code 1, got {rc}"

    error = json.loads(stdout)
    assert "error" in error
    assert error["error"] == "not_found"
    print("  PASS: Got expected not_found error")

def test_allocation_list_combined_filters(data_dir):
    """Test listing allocations with both assignment and status filters."""
    print("\n=== Test: List allocations with both filters (no matches expected) ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "list", "--status", "unbound", "--assignment", "some-assignment")
    assert rc == 0, f"Failed to list allocations: {stdout}"

    listed = json.loads(stdout)
    assert len(listed) == 0, f"Expected no allocations, got {len(listed)}"
    print("  PASS: Combined filters work correctly")

def test_sorting_by_created_at_and_uuid(data_dir):
    """Test that allocations are sorted by created_at then uuid."""
    print("\n=== Test: Sorting by created_at then uuid ===")
    # Create a new blueprint and allocations to test sorting
    new_blueprint = {
        "name": "new-blueprint",
        "requirement_sets": [
            {"resource_type": "vm", "resource_count": 1}
        ]
    }
    run_forge_stdin(data_dir, new_blueprint, "blueprint", "create")

    # Create allocations
    run_forge_stdin(data_dir, {"blueprint_name": "new-blueprint"}, "allocation", "create")

    # List all allocations
    stdout, _, _ = run_forge(data_dir, "allocation", "list")
    all_allocations = json.loads(stdout)

    # Check sorting: by created_at ascending, ties broken by uuid ascending
    for i in range(1, len(all_allocations)):
        prev = all_allocations[i-1]
        curr = all_allocations[i]
        assert (prev["created_at"], prev["uuid"]) <= (curr["created_at"], curr["uuid"]), "Not sorted correctly"

    print(f"  PASS: {len(all_allocations)} allocations sorted correctly")

def main():
    print("=" * 60)
    print("Running comprehensive allocation tests")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\nUsing temp directory: {tmpdir}")

        # Test 1: Create a blueprint
        create_test_blueprint(tmpdir)

        # Test 2: Create allocations
        allocations = test_allocation_create(tmpdir)

        # Test 3: List all allocations
        test_allocation_list(tmpdir, allocations)

        # Test 4: List with status filter
        test_allocation_list_filter_status(tmpdir)

        # Test 5: List with assignment filter
        test_allocation_list_filter_assignment(tmpdir)

        # Test 6: Get specific allocation
        test_allocation_get(tmpdir, allocations)

        # Test 7: Create allocation from non-existent blueprint
        test_allocation_create_nonexistent(tmpdir)

        # Test 8: Validation errors
        test_allocation_create_empty_name(tmpdir)
        test_allocation_create_invalid_type(tmpdir)

        # Test 9: Get non-existent allocation
        test_allocation_get_nonexistent(tmpdir)

        # Test 10: Combined filters
        test_allocation_list_combined_filters(tmpdir)

        # Test 11: Sorting
        test_sorting_by_created_at_and_uuid(tmpdir)

    print("\n" + "=" * 60)
    print("All tests passed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
