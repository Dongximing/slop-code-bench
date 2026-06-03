#!/usr/bin/env python3
"""Comprehensive test script for allocation binding and deletion functionality."""
import subprocess
import json
import tempfile
import sys

def run_forge(data_dir, *args):
    """Run forge.py with given arguments, return stdout."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def run_forge_stdin(data_dir, stdin_json, *args):
    """Run forge.py with JSON from stdin."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, input=json.dumps(stdin_json), capture_output=True, text=True)
    return result.stdout.strip(), result.stderr.strip(), result.returncode

def create_test_blueprint(data_dir, name=None):
    """Create a test blueprint with a unique name."""
    import random
    if name is None:
        name = f"test-blueprint-{random.randint(100000, 999999)}"
    blueprint_data = {
        "name": name,
        "requirement_sets": [
            {"resource_type": "vm", "resource_count": 1},
            {"resource_type": "disk", "resource_count": 1, "capabilities": ["category=TYPE_A"]}
        ]
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, blueprint_data, "blueprint", "create")
    assert rc == 0, f"Failed to create blueprint: {stdout}"
    return json.loads(stdout), name

def create_allocations(data_dir, blueprint_name):
    """Create allocations from a blueprint."""
    alloc_data = {"blueprint_name": blueprint_name}
    stdout, stderr, rc = run_forge_stdin(data_dir, alloc_data, "allocation", "create")
    assert rc == 0, f"Failed to create allocations: {stdout}"
    return json.loads(stdout)

def test_bind_basic(data_dir):
    """Test basic bind functionality."""
    print("\n=== Test: Basic bind ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    # Bind first allocation to an assignment
    assignment_id = "test-assignment-123"
    bind_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, bind_payload, "allocation", "bind")
    assert rc == 0, f"Failed to bind: {stdout}"
    assert json.loads(stdout) == {"status": "accepted"}, f"Unexpected response: {stdout}"

    # Verify allocation is bound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    assert rc == 0, f"Failed to get allocation: {stdout}"
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "bound"
    assert alloc["assignment_id"] == assignment_id
    print("  PASS: Basic bind works")

def test_bind_multiple_allocations(data_dir):
    """Test binding multiple allocations at once."""
    print("\n=== Test: Bind multiple allocations ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    assignment_id = "test-assignment-456"
    bind_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add",
            allocations[1]["uuid"]: "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, bind_payload, "allocation", "bind")
    assert rc == 0, f"Failed to bind: {stdout}"
    assert json.loads(stdout) == {"status": "accepted"}, f"Unexpected response: {stdout}"

    # Verify both allocations are bound
    for i in range(2):
        stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[i]["uuid"])
        alloc = json.loads(stdout)
        assert alloc["binding_status"] == "bound"
        assert alloc["assignment_id"] == assignment_id

    # Verify third allocation is still unbound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[2]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "unbound"
    assert alloc["assignment_id"] is None
    print("  PASS: Multiple bind works")

def test_remove_binding(data_dir):
    """Test removing (unbinding) allocations."""
    print("\n=== Test: Remove binding ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    # First bind allocation
    assignment_id = "test-assignment-789"
    bind_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind_payload, "allocation", "bind")

    # Verify it's bound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "bound"
    assert alloc["assignment_id"] == assignment_id

    # Now remove binding
    remove_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "remove"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, remove_payload, "allocation", "bind")
    assert rc == 0, f"Failed to remove: {stdout}"
    assert json.loads(stdout) == {"status": "accepted"}, f"Unexpected response: {stdout}"

    # Verify allocation is unbound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "unbound"
    assert alloc["assignment_id"] is None
    print("  PASS: Remove binding works")

def test_remove_already_unbound(data_dir):
    """Test removing binding from already-unbound allocation (silent no-op)."""
    print("\n=== Test: Remove already-unbound allocation ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    # Try to remove from unbound allocation (should be silent no-op)
    remove_payload = {
        "assignment_id": "any-assignment",
        "allocations": {
            allocations[0]["uuid"]: "remove"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, remove_payload, "allocation", "bind")
    assert rc == 0, f"Failed to remove: {stdout}"
    assert json.loads(stdout) == {"status": "accepted"}, f"Unexpected response: {stdout}"

    # Verify allocation is still unbound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "unbound"
    assert alloc["assignment_id"] is None
    print("  PASS: Remove unbound is silent no-op")

def test_duplicate_assignment_guard(data_dir):
    """Test duplicate assignment guard prevents binding to an assignment that already has bound allocations."""
    print("\n=== Test: Duplicate assignment guard ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    assignment_id = "test-assignment-dup"

    # First bind one allocation to the assignment
    bind1 = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind1, "allocation", "bind")

    # Try to bind another allocation to same assignment (should fail)
    bind2 = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[1]["uuid"]: "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, bind2, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "duplicate_assignment"
    print("  PASS: Duplicate assignment guard prevents duplicate binding")

def test_duplicate_assignment_guard_removes_bypass(data_dir):
    """Test that remove operations bypass duplicate assignment guard."""
    print("\n=== Test: Remove bypasses duplicate guard ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    assignment_id = "test-assignment-dup-remove"

    # First bind one allocation to the assignment
    bind1 = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind1, "allocation", "bind")

    # Try to remove a different allocation from same assignment (should succeed)
    remove_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[1]["uuid"]: "remove"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, remove_payload, "allocation", "bind")
    assert rc == 0, f"Expected success, got: {stdout}"
    assert json.loads(stdout) == {"status": "accepted"}, f"Unexpected response: {stdout}"
    print("  PASS: Remove bypasses duplicate assignment guard")

def test_atomicity(data_dir):
    """Test that all operations succeed or fail together (atomicity)."""
    print("\n=== Test: Atomicity ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    assignment_id = "test-assignment-atomic"

    # Bind first allocation
    bind1 = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind1, "allocation", "bind")

    # Try to bind first allocation (existing) and a non-existent allocation (should fail)
    nonexistent_uuid = "00000000-0000-0000-0000-000000000000"
    bind_fail = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add",
            nonexistent_uuid: "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, bind_fail, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "not_found"

    # Verify first allocation is still bound (no partial changes)
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "bound"
    assert alloc["assignment_id"] == assignment_id
    print("  PASS: Atomicity maintained")

def test_bind_validation_missing_assignment_id(data_dir):
    """Test bind with missing assignment_id."""
    print("\n=== Test: Bind validation - missing assignment_id ===")

    payload = {
        "allocations": {
            "some-uuid": "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, payload, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "validation_error"
    assert "assignment_id" in error.get("detail", "").lower()
    print("  PASS: Missing assignment_id validation works")

def test_bind_validation_missing_allocations(data_dir):
    """Test bind with missing allocations."""
    print("\n=== Test: Bind validation - missing allocations ===")
    payload = {
        "assignment_id": "test-assignment"
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, payload, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "validation_error"
    assert "allocations" in error.get("detail", "").lower()
    print("  PASS: Missing allocations validation works")

def test_bind_validation_empty_allocations(data_dir):
    """Test bind with empty allocations map."""
    print("\n=== Test: Bind validation - empty allocations ===")
    payload = {
        "assignment_id": "test-assignment",
        "allocations": {}
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, payload, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "validation_error"
    assert "empty" in error.get("detail", "").lower()
    print("  PASS: Empty allocations validation works")

def test_bind_validation_invalid_operation(data_dir):
    """Test bind with invalid operation value."""
    print("\n=== Test: Bind validation - invalid operation ===")
    _, name = create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, name)

    payload = {
        "assignment_id": "test-assignment",
        "allocations": {
            allocations[0]["uuid"]: "invalid_operation"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, payload, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "invalid_operation"
    print("  PASS: Invalid operation validation works")

def test_bind_validation_invalid_operation_priority(data_dir):
    """Test that invalid_operation is checked before not_found."""
    print("\n=== Test: Bind validation - invalid operation priority ===")
    payload = {
        "assignment_id": "test-assignment",
        "allocations": {
            "00000000-0000-0000-0000-000000000000": "invalid"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, payload, "allocation", "bind")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "invalid_operation"
    print("  PASS: Invalid operation checked before not_found")

def test_delete_by_assignment(data_dir):
    """Test deletion by assignment_id."""
    print("\n=== Test: Delete by assignment ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    assignment_id = "test-assignment-delete"

    # Bind some allocations
    bind_payload = {
        "assignment_id": assignment_id,
        "allocations": {
            allocations[0]["uuid"]: "add",
            allocations[1]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind_payload, "allocation", "bind")

    # Delete by assignment
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--assignment", assignment_id)
    assert rc == 0, f"Failed to delete: {stdout}"
    result = json.loads(stdout)
    assert result == {"deleted": 2}, f"Expected deleted=2, got: {result}"

    # Verify allocations are deleted
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    assert rc == 1, "Allocation should be deleted"
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[1]["uuid"])
    assert rc == 1, "Allocation should be deleted"

    # Verify third allocation still exists
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[2]["uuid"])
    assert rc == 0, "Unrelated allocation should still exist"
    print("  PASS: Delete by assignment works")

def test_delete_by_assignment_no_matches(data_dir):
    """Test deletion by assignment_id with no matches."""
    print("\n=== Test: Delete by assignment (no matches) ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    assignment_id = "non-existent-assignment"
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--assignment", assignment_id)
    assert rc == 0, f"Failed to delete: {stdout}"
    result = json.loads(stdout)
    assert result == {"deleted": 0}, f"Expected deleted=0, got: {result}"
    print("  PASS: Delete by assignment with no matches works")

def test_delete_by_ids_success(data_dir):
    """Test deletion by ID list."""
    print("\n=== Test: Delete by IDs ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    # Delete first two allocations by ID
    uuids = f"{allocations[0]['uuid']},{allocations[1]['uuid']}"
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--ids", uuids)
    assert rc == 0, f"Failed to delete: {stdout}"
    result = json.loads(stdout)
    assert result == {"deleted": 2}, f"Expected deleted=2, got: {result}"

    # Verify they're deleted
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    assert rc == 1, "Allocation should be deleted"
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[1]["uuid"])
    assert rc == 1, "Allocation should be deleted"

    # Verify third still exists
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[2]["uuid"])
    assert rc == 0, "Unrelated allocation should still exist"
    print("  PASS: Delete by IDs works")

def test_delete_by_ids_not_found(data_dir):
    """Test deletion by ID list with non-existent UUID."""
    print("\n=== Test: Delete by IDs (not found) ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    nonexistent_uuid = "00000000-0000-0000-0000-000000000000"
    uuids = f"{allocations[0]['uuid']},{nonexistent_uuid}"
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--ids", uuids)
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "not_found"

    # Verify no allocations were deleted (atomicity)
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    assert rc == 0, "Allocation should not be deleted"
    print("  PASS: Delete by IDs with not_found fails atomically")

def test_delete_by_ids_empty_csv(data_dir):
    """Test deletion by empty CSV."""
    print("\n=== Test: Delete by empty CSV ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--ids", "")
    assert rc == 0, f"Failed to delete: {stdout}"
    result = json.loads(stdout)
    assert result == {"deleted": 0}, f"Expected deleted=0, got: {result}"
    print("  PASS: Delete by empty CSV works")

def test_delete_validation_no_flags(data_dir):
    """Test delete with neither --assignment nor --ids."""
    print("\n=== Test: Delete validation - no flags ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "validation_error"
    assert "exactly one" in error.get("detail", "").lower()
    print("  PASS: Delete with no flags validation works")

def test_delete_validation_both_flags(data_dir):
    """Test delete with both --assignment and --ids."""
    print("\n=== Test: Delete validation - both flags ===")
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--assignment", "assign-123", "--ids", "uuid-123")
    assert rc == 1, f"Expected failure, got: {stdout}"
    error = json.loads(stdout)
    assert error.get("error") == "validation_error"
    assert "exactly one" in error.get("detail", "").lower()
    print("  PASS: Delete with both flags validation works")

def test_bind_then_delete_by_assignment(data_dir):
    """Test binding allocations then deleting them all by assignment."""
    print("\n=== Test: Bind then delete by assignment ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    assignment_id = "test-bind-delete"

    # Bind all allocations to same assignment
    bind_ops = {a["uuid"]: "add" for a in allocations}
    bind_payload = {
        "assignment_id": assignment_id,
        "allocations": bind_ops
    }
    run_forge_stdin(data_dir, bind_payload, "allocation", "bind")

    # Delete by assignment
    stdout, stderr, rc = run_forge(data_dir, "allocation", "delete", "--assignment", assignment_id)
    assert rc == 0, f"Failed to delete: {stdout}"
    result = json.loads(stdout)
    assert result == {"deleted": len(allocations)}, f"Expected deleted={len(allocations)}, got: {result}"

    # Verify all are deleted
    for alloc in allocations:
        stdout, stderr, rc = run_forge(data_dir, "allocation", "get", alloc["uuid"])
        assert rc == 1, f"Allocation {alloc['uuid'][:8]} should be deleted"
    print("  PASS: Bind then delete by assignment works")

def test_remove_then_rebind(data_dir):
    """Test removing binding then rebinding to a different assignment."""
    print("\n=== Test: Remove then rebind ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    # Bind to first assignment
    assignment1 = "assignment-1"
    bind1 = {
        "assignment_id": assignment1,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind1, "allocation", "bind")

    # Remove binding
    remove = {
        "assignment_id": assignment1,
        "allocations": {
            allocations[0]["uuid"]: "remove"
        }
    }
    run_forge_stdin(data_dir, remove, "allocation", "bind")

    # Rebind to different assignment
    assignment2 = "assignment-2"
    bind2 = {
        "assignment_id": assignment2,
        "allocations": {
            allocations[0]["uuid"]: "add"
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, bind2, "allocation", "bind")
    assert rc == 0, f"Failed to rebind: {stdout}"

    # Verify it's now bound to assignment2
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[0]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "bound"
    assert alloc["assignment_id"] == assignment2
    print("  PASS: Remove then rebind works")

def test_mixed_bind_operations(data_dir):
    """Test mixing add and remove operations in a single bind request."""
    print("\n=== Test: Mixed bind operations ===")
    create_test_blueprint(data_dir)
    allocations = create_allocations(data_dir, "test-blueprint")

    assignment1 = "assignment-1"
    assignment2 = "assignment-2"

    # Bind first two allocations
    bind1 = {
        "assignment_id": assignment1,
        "allocations": {
            allocations[0]["uuid"]: "add",
            allocations[1]["uuid"]: "add"
        }
    }
    run_forge_stdin(data_dir, bind1, "allocation", "bind")

    # Mix operations: remove first, add third (both to assignment2)
    mixed = {
        "assignment_id": assignment2,
        "allocations": {
            allocations[0]["uuid"]: "remove",  # Remove from assignment1 (unbound)
            allocations[1]["uuid"]: "add",      # Add to assignment2 (should work even though it's already bound to assignment1 - remove happens first)
            allocations[2]["uuid"]: "add",      # Add fresh
        }
    }
    stdout, stderr, rc = run_forge_stdin(data_dir, mixed, "allocation", "bind")
    assert rc == 0, f"Failed: {stdout}"

    # Check allocations 0, 1, 2 are bound to assignment2
    for i in range(3):
        stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[i]["uuid"])
        alloc = json.loads(stdout)
        assert alloc["binding_status"] == "bound"
        assert alloc["assignment_id"] == assignment2

    # Allocation 3 should still be unbound
    stdout, stderr, rc = run_forge(data_dir, "allocation", "get", allocations[3]["uuid"])
    alloc = json.loads(stdout)
    assert alloc["binding_status"] == "unbound"
    print("  PASS: Mixed bind operations work")

def main():
    print("=" * 60)
    print("Running allocation binding and deletion tests")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmpdir:
        print(f"\nUsing temp directory: {tmpdir}")

        # Test: Basic bind
        test_bind_basic(tmpdir)

        # Test: Bind multiple allocations
        test_bind_multiple_allocations(tmpdir)

        # Test: Remove binding
        test_remove_binding(tmpdir)

        # Test: Remove already-unbound
        test_remove_already_unbound(tmpdir)

        # Test: Duplicate assignment guard
        test_duplicate_assignment_guard(tmpdir)

        # Test: Remove bypasses duplicate guard
        test_duplicate_assignment_guard_removes_bypass(tmpdir)

        # Test: Atomicity
        test_atomicity(tmpdir)

        # Test: Validation - missing assignment_id
        test_bind_validation_missing_assignment_id(tmpdir)

        # Test: Validation - missing allocations
        test_bind_validation_missing_allocations(tmpdir)

        # Test: Validation - empty allocations
        test_bind_validation_empty_allocations(tmpdir)

        # Test: Validation - invalid operation
        test_bind_validation_invalid_operation(tmpdir)

        # Test: Validation - invalid operation priority
        test_bind_validation_invalid_operation_priority(tmpdir)

        # Test: Delete by assignment
        test_delete_by_assignment(tmpdir)

        # Test: Delete by assignment (no matches)
        test_delete_by_assignment_no_matches(tmpdir)

        # Test: Delete by IDs
        test_delete_by_ids_success(tmpdir)

        # Test: Delete by IDs (not found)
        test_delete_by_ids_not_found(tmpdir)

        # Test: Delete by empty CSV
        test_delete_by_ids_empty_csv(tmpdir)

        # Test: Delete validation - no flags
        test_delete_validation_no_flags(tmpdir)

        # Test: Delete validation - both flags
        test_delete_validation_both_flags(tmpdir)

        # Test: Bind then delete by assignment
        test_bind_then_delete_by_assignment(tmpdir)

        # Test: Remove then rebind
        test_remove_then_rebind(tmpdir)

        # Test: Mixed bind operations
        test_mixed_bind_operations(tmpdir)

    print("\n" + "=" * 60)
    print("All binding and deletion tests passed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
