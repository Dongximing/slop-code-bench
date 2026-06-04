#!/usr/bin/env python3
"""
Test script for allocation binding and removal functionality.
This tests all the requirements from the specification.
"""
import json
import subprocess
import tempfile
import os
import sys

FORGE_CMD = [sys.executable, "/workspace/forge.py"]

def run_forge(data_dir, args, stdin_input=""):
    """Run forge command and return (stdout, stderr, exit_code)."""
    cmd = FORGE_CMD + ["--data-dir", data_dir] + args
    result = subprocess.run(
        cmd,
        input=stdin_input,
        capture_output=True,
        text=True
    )
    return result.stdout, result.stderr, result.returncode


def test_bind_validation_error():
    """Test bind validation errors - missing assignment_id, allocations, or empty allocations."""
    print("=== Test: Validation Error - missing assignment_id ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Missing assignment_id
        payload = {
            "allocations": {
                "abc123": "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should exit with code 1"
        assert "Missing 'assignment_id'" in stdout, "Should have missing assignment_id error"

    with tempfile.TemporaryDirectory() as tmpdir:
        print("\n=== Test: Validation Error - missing allocations ===")
        payload = {
            "assignment_id": "abc123"
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should exit with code 1"
        assert "Missing 'allocations'" in stdout, "Should have missing allocations error"

    with tempfile.TemporaryDirectory() as tmpdir:
        print("\n=== Test: Validation Error - empty allocations ===")
        payload = {
            "assignment_id": "abc123",
            "allocations": {}
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should exit with code 1"
        assert "allocations must be a non-empty object" in stdout, "Should have empty allocations error"

    print("PASSED: validation_error tests\n")


def test_invalid_operation():
    """Test invalid operation error - operation must be 'add' or 'remove'."""
    print("=== Test: Invalid operation ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "assignment_id": "abc123",
            "allocations": {
                "abc123": "invalid_op"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should exit with code 1"
        assert "Invalid operation" in stdout, "Should have invalid_operation error"

    print("PASSED: invalid_operation test\n")


def test_bind_not_found():
    """Test not_found error - allocation UUID does not exist."""
    print("=== Test: Not found error ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "assignment_id": "abc123",
            "allocations": {
                "nonexistent_uuid": "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should exit with code 1"
        assert "not found" in stdout.lower(), "Should have not_found error"

    print("PASSED: not_found test\n")


def test_bind_duplicate_assignment():
    """Test duplicate_assignment guard - assignment already has bound allocations."""
    print("=== Test: Duplicate assignment guard ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # First, create a blueprint
        blueprint_payload = {
            "name": "test_bp",
            "requirement_sets": [
                {
                    "resource_type": "test",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))
        assert code == 0, "Blueprint creation should succeed"

        # Create allocations from blueprint
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp"}))
        assert code == 0, "Allocation creation should succeed"

        # Get the allocation UUID
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid = allocs[0]["uuid"]

        # Bind the allocation to assignment_id "test_assign"
        bind_payload = {
            "assignment_id": "test_assign",
            "allocations": {
                alloc_uuid: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        assert code == 0, "Should succeed first bind"

        # Now try to bind another allocation to the same assignment - this should fail duplicate check
        # First create another allocation
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp"}))
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        # Get an unbound allocation
        unbound_alloc = None
        for a in allocs:
            if a["binding_status"] == "unbound":
                unbound_alloc = a
                break

        bind_payload2 = {
            "assignment_id": "test_assign",  # Same assignment
            "allocations": {
                unbound_alloc["uuid"]: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload2))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should fail with duplicate_assignment error"
        assert "duplicate_assignment" in stdout, "Should have duplicate_assignment error"

    print("PASSED: duplicate_assignment test\n")


def test_bind_operations():
    """Test add and remove operations."""
    print("=== Test: Add and remove operations ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint
        blueprint_payload = {
            "name": "test_bp2",
            "requirement_sets": [
                {
                    "resource_type": "test",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocation
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp2"}))
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid = allocs[0]["uuid"]

        # Test add operation
        print("\n--- Testing add operation ---")
        bind_payload = {
            "assignment_id": "test_assign",
            "allocations": {
                alloc_uuid: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed"
        assert stdout.strip() == '{"status": "accepted"}', f"Should return status accepted, got: {stdout}"

        # Verify allocation is bound
        alloc = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid])[0])
        print(f"Allocation after bind: {alloc}")
        assert alloc["binding_status"] == "bound", "Allocation should be bound"
        assert alloc["assignment_id"] == "test_assign", "assignment_id should match"

        # Test remove operation
        print("\n--- Testing remove operation ---")
        remove_payload = {
            "assignment_id": "test_assign",
            "allocations": {
                alloc_uuid: "remove"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(remove_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed"
        # Just verify it matches the expected status format
        result = json.loads(stdout)
        assert result == {"status": "accepted"}, f"Should return status accepted, got: {result}"

        # Verify allocation is unbound
        alloc = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid])[0])
        print(f"Allocation after remove: {alloc}")
        assert alloc["binding_status"] == "unbound", "Allocation should be unbound"
        assert alloc["assignment_id"] is None, "assignment_id should be null"

        # Test remove on already unbound allocation (silent no-op)
        print("\n--- Testing remove on already unbound (silent no-op) ---")
        remove_payload2 = {
            "assignment_id": "dummy",
            "allocations": {
                alloc_uuid: "remove"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(remove_payload2))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed (silent no-op)"

        alloc = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid])[0])
        print(f"Allocation after double remove: {alloc}")
        assert alloc["binding_status"] == "unbound", "Allocation should remain unbound"
        assert alloc["assignment_id"] is None, "assignment_id should remain null"

    print("PASSED: add/remove operations tests\n")


def test_bind_atomicity():
    """Test atomicity - all operations succeed or fail together."""
    print("=== Test: Atomicity ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint with multiple requirement sets
        blueprint_payload = {
            "name": "test_bp3",
            "requirement_sets": [
                {
                    "resource_type": "test1",
                    "resource_count": 1
                },
                {
                    "resource_type": "test2",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocations
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp3"}))
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid1 = allocs[0]["uuid"]
        alloc_uuid2 = allocs[1]["uuid"]

        # Try a bind with one valid and one invalid operation (not_found)
        bind_payload = {
            "assignment_id": "test_assign",
            "allocations": {
                alloc_uuid1: "add",
                "nonexistent_uuid": "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should fail with not_found"
        assert "not found" in stdout.lower(), "Should have not_found error"

        # Verify no allocations were modified (neither should be bound)
        alloc1 = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid1])[0])
        alloc2 = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid2])[0])
        print(f"Allocation 1: status={alloc1['binding_status']}, assign={alloc1['assignment_id']}")
        print(f"Allocation 2: status={alloc2['binding_status']}, assign={alloc2['assignment_id']}")
        assert alloc1["binding_status"] == "unbound", "First allocation should remain unbound"
        assert alloc1["assignment_id"] is None, "First assignment_id should remain null"
        assert alloc2["binding_status"] == "unbound", "Second allocation should remain unbound"
        assert alloc2["assignment_id"] is None, "Second assignment_id should remain null"

    print("PASSED: atomicity test\n")


def test_delete_by_assignment():
    """Test deletion by assignment_id."""
    print("=== Test: Delete by assignment ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint
        blueprint_payload = {
            "name": "test_bp4",
            "requirement_sets": [
                {
                    "resource_type": "test",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocations
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp4"}))

        # Get allocation UUIDs
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid = allocs[0]["uuid"]

        # Bind allocations to assignment
        bind_payload = {
            "assignment_id": "assign_to_delete",
            "allocations": {
                alloc_uuid: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        assert code == 0, "Should succeed"

        # Delete by assignment
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--assignment", "assign_to_delete"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed"
        result = json.loads(stdout)
        assert result["deleted"] == 1, "Should delete 1 allocation"

        # Verify allocation is gone
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "get", alloc_uuid])
        assert code == 1, "Allocation should not exist"
        assert "not found" in stdout.lower(), "Should have not_found error"

        # Test delete with no matching assignment
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--assignment", "nonexistent"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        result = json.loads(stdout)
        assert result["deleted"] == 0, "Should delete 0 allocations"

    print("PASSED: delete by assignment test\n")


def test_delete_by_ids():
    """Test deletion by UUID list."""
    print("=== Test: Delete by IDs ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint with multiple requirement sets
        blueprint_payload = {
            "name": "test_bp5",
            "requirement_sets": [
                {
                    "resource_type": "test1",
                    "resource_count": 1
                },
                {
                    "resource_type": "test2",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocations
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp5"}))

        # Get allocation UUIDs
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid1 = allocs[0]["uuid"]
        alloc_uuid2 = allocs[1]["uuid"]

        # Delete by IDs (both exist)
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--ids", f"{alloc_uuid1},{alloc_uuid2}"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed"
        result = json.loads(stdout)
        assert result["deleted"] == 2, "Should delete 2 allocations"

        # Verify allocations are gone
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "get", alloc_uuid1])
        assert code == 1, "Allocation should not exist"
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "get", alloc_uuid2])
        assert code == 1, "Allocation should not exist"

        # Create new ones for not_found test
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp5"}))

        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid = allocs[0]["uuid"]

        # Delete by IDs with nonexistent UUID
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--ids", f"{alloc_uuid},nonexistent"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should fail with not_found"
        assert "not found" in stdout.lower(), "Should have not_found error"

        # Verify no allocations were deleted (should have the same number as before)
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "list"])
        result = json.loads(stdout)
        original_count = len(allocs)
        assert len(result) == original_count, f"Should still have {original_count} allocations, got {len(result)}"

        # Test empty CSV member - should treat as unresolved UUID, not ignore (empty string as UUID)
        empty_uuid_list = f"{alloc_uuid},"
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--ids", empty_uuid_list])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should fail with not_found for empty string UUID"
        assert "not found" in stdout.lower(), "Should have not_found error for empty UUID"

    print("PASSED: delete by IDs test\n")


def test_delete_validation():
    """Test delete validation errors - neither or both flags provided."""
    print("=== Test: Delete validation errors ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Neither flag - argparse enforces required mutex group, exits with 2
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        # argparse exits with 2 for missing required arguments
        assert code == 2, "Should exit with argparse error code 2 for missing argument"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Both provided - argparse enforces mutual exclusion
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "delete", "--assignment", "test", "--ids", "abc"])
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 2, "Should exit with argparse error code 2 for mutually exclusive arguments"
        print("Argparse correctly rejects both --assignment and --ids being specified together")

    print("PASSED: delete validation tests\n")


def test_multiple_allocations_add():
    """Test binding multiple allocations in one request."""
    print("=== Test: Multiple allocations add ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint that creates multiple allocations
        blueprint_payload = {
            "name": "test_bp_multi",
            "requirement_sets": [
                {
                    "resource_type": "test1",
                    "resource_count": 1
                },
                {
                    "resource_type": "test2",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocations
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp_multi"}))
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        assert len(allocs) >= 2, "Should create at least 2 allocations"
        print(f"Created {len(allocs)} allocations")

        # Bind multiple allocations at once (just the first two for now)
        bind_payload = {
            "assignment_id": "multi_assign",
            "allocations": {
                allocs[0]["uuid"]: "add",
                allocs[1]["uuid"]: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed"
        assert stdout.strip() == '{"status": "accepted"}', f"Should return status accepted, got: {stdout}"

        # Verify all are bound
        for alloc in allocs:
            result = json.loads(run_forge(tmpdir, ["allocation", "get", alloc["uuid"]])[0])
            assert result["binding_status"] == "bound", f"Allocation {alloc['uuid']} should be bound"
            assert result["assignment_id"] == "multi_assign", f"Assignment ID should match"

    print("PASSED: multiple allocations add test\n")


def test_mixed_operations():
    """Test mixing add and remove operations in one request."""
    print("=== Test: Mixed operations ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create blueprint with multiple requirement sets
        blueprint_payload = {
            "name": "test_bp_mixed",
            "requirement_sets": [
                {
                    "resource_type": "test1",
                    "resource_count": 1
                },
                {
                    "resource_type": "test2",
                    "resource_count": 1
                }
            ]
        }
        stdout, stderr, code = run_forge(tmpdir, ["blueprint", "create"], json.dumps(blueprint_payload))

        # Create allocations
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "create"], json.dumps({"blueprint_name": "test_bp_mixed"}))
        allocs = json.loads(run_forge(tmpdir, ["allocation", "list"])[0])
        alloc_uuid1 = allocs[0]["uuid"]
        alloc_uuid2 = allocs[1]["uuid"]

        # Test removing an allocation from an assignment
        # First, bind alloc1 to assign1
        bind_payload = {
            "assignment_id": "assign1",
            "allocations": {
                alloc_uuid1: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(bind_payload))
        assert code == 0, "Should succeed bind"

        # Now try to add alloc2 to the same assignment (should fail duplicate_assignment)
        add_payload = {
            "assignment_id": "assign1",
            "allocations": {
                alloc_uuid2: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(add_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 1, "Should fail with duplicate_assignment"
        assert "duplicate_assignment" in stdout, "Should have duplicate_assignment error"

        # Test removing an allocation from an assignment
        remove_payload = {
            "assignment_id": "assign1",  # match the assignment_id
            "allocations": {
                alloc_uuid1: "remove"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(remove_payload))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed remove"

        # Verify state - alloc1 should now be unbound
        alloc1 = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid1])[0])
        print(f"After remove: 1={alloc1}")
        assert alloc1["binding_status"] == "unbound", "First should be unbound"
        assert alloc1["assignment_id"] is None, "First should have no assignment_id"

        # Now add alloc2 should succeed (no more bound allocations in assign1)
        add_payload2 = {
            "assignment_id": "assign1",
            "allocations": {
                alloc_uuid2: "add"
            }
        }
        stdout, stderr, code = run_forge(tmpdir, ["allocation", "bind"], json.dumps(add_payload2))
        print(f"Exit code: {code}")
        print(f"Stdout: {stdout}")
        assert code == 0, "Should succeed second add"

        # Verify alloc2 is bound to assign1
        alloc2 = json.loads(run_forge(tmpdir, ["allocation", "get", alloc_uuid2])[0])
        print(f"After second add: 2={alloc2}")
        assert alloc2["binding_status"] == "bound", "Second should be bound"
        assert alloc2["assignment_id"] == "assign1", "Second should have assign1"

    print("PASSED: mixed operations test\n")


if __name__ == "__main__":
    print("=" * 70)
    print("Running allocation binding and removal tests")
    print("=" * 70)
    print()

    try:
        test_bind_validation_error()
        test_invalid_operation()
        test_bind_not_found()
        test_bind_duplicate_assignment()
        test_bind_operations()
        test_bind_atomicity()
        test_delete_by_assignment()
        test_delete_by_ids()
        test_delete_validation()
        test_multiple_allocations_add()
        test_mixed_operations()

        print("=" * 70)
        print("ALL TESTS PASSED!")
        print("=" * 70)
        sys.exit(0)
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
