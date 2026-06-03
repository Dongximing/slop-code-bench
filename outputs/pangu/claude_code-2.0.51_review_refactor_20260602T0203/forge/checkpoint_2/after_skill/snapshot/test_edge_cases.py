#!/usr/bin/env python3
"""Test edge cases for allocation type detection."""
import subprocess
import json
import tempfile

def run_forge_stdin(data_dir, stdin_json, *args):
    """Run forge.py with JSON from stdin."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, input=json.dumps(stdin_json), capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode

def run_forge(data_dir, *args):
    """Run forge.py with given arguments, return stdout."""
    cmd = ["python3", "forge.py", "--data-dir", data_dir] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode

def test_non_exact_type_a_match():
    """Test that 'category=TYPE_A_EXTRA' does NOT trigger double allocation."""
    print("\n=== Test: Non-exact TYPE_A match (should get 1 allocation) ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        blueprint = {
            "name": "bp",
            "requirement_sets": [
                {"resource_type": "vm", "resource_count": 1, "capabilities": ["category=TYPE_A_EXTRA"]}
            ]
        }
        run_forge_stdin(tmpdir, blueprint, "blueprint", "create")
        run_forge_stdin(tmpdir, {"blueprint_name": "bp"}, "allocation", "create")
        allocs = json.loads(run_forge(tmpdir, "allocation", "list"))
        print(f"  Got {len(allocs)} allocations (expected 1)")
        assert len(allocs) == 1, f"Expected 1 allocation, got {len(allocs)}"
        print("  PASS")

def test_missing_capabilities_field():
    """Test requirement set without capabilities field (should get 1 allocation)."""
    print("\n=== Test: Missing capabilities field (should get 1 allocation) ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        blueprint = {
            "name": "bp",
            "requirement_sets": [
                {"resource_type": "vm", "resource_count": 1}
            ]
        }
        run_forge_stdin(tmpdir, blueprint, "blueprint", "create")
        run_forge_stdin(tmpdir, {"blueprint_name": "bp"}, "allocation", "create")
        allocs = json.loads(run_forge(tmpdir, "allocation", "list"))
        print(f"  Got {len(allocs)} allocations (expected 1)")
        assert len(allocs) == 1, f"Expected 1 allocation, got {len(allocs)}"
        print("  PASS")

def test_empty_capabilities_array():
    """Test requirement set with empty capabilities array (should get 1 allocation)."""
    print("\n=== Test: Empty capabilities array (should get 1 allocation) ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        blueprint = {
            "name": "bp",
            "requirement_sets": [
                {"resource_type": "vm", "resource_count": 1, "capabilities": []}
            ]
        }
        run_forge_stdin(tmpdir, blueprint, "blueprint", "create")
        run_forge_stdin(tmpdir, {"blueprint_name": "bp"}, "allocation", "create")
        allocs = json.loads(run_forge(tmpdir, "allocation", "list"))
        print(f"  Got {len(allocs)} allocations (expected 1)")
        assert len(allocs) == 1, f"Expected 1 allocation, got {len(allocs)}"
        print("  PASS")

def test_case_sensitive():
    """Test that 'category=type_a' (different case) doesn't match."""
    print("\n=== Test: Case sensitivity (lowercase 'type_a' should not match) ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        blueprint = {
            "name": "bp",
            "requirement_sets": [
                {"resource_type": "vm", "resource_count": 1, "capabilities": ["category=type_a"]}
            ]
        }
        run_forge_stdin(tmpdir, blueprint, "blueprint", "create")
        run_forge_stdin(tmpdir, {"blueprint_name": "bp"}, "allocation", "create")
        allocs = json.loads(run_forge(tmpdir, "allocation", "list"))
        print(f"  Got {len(allocs)} allocations (expected 1)")
        assert len(allocs) == 1, f"Expected 1 allocation, got {len(allocs)}"
        print("  PASS")

def test_multiple_type_a_requirement_sets():
    """Test multiple requirement sets with TYPE_A should double both."""
    print("\n=== Test: Multiple TYPE_A requirement sets ===")
    with tempfile.TemporaryDirectory() as tmpdir:
        blueprint = {
            "name": "bp",
            "requirement_sets": [
                {"resource_type": "vm", "resource_count": 1, "capabilities": ["category=TYPE_A"]},
                {"resource_type": "disk", "resource_count": 1, "capabilities": ["category=TYPE_A"]},
            ]
        }
        run_forge_stdin(tmpdir, blueprint, "blueprint", "create")
        run_forge_stdin(tmpdir, {"blueprint_name": "bp"}, "allocation", "create")
        allocs = json.loads(run_forge(tmpdir, "allocation", "list"))
        print(f"  Got {len(allocs)} allocations (expected 4)")
        assert len(allocs) == 4, f"Expected 4 allocations, got {len(allocs)}"
        assert all(a["binding_status"] == "unbound" for a in allocs)
        print("  PASS")

def main():
    print("=" * 60)
    print("Running edge case tests")
    print("=" * 60)

    test_non_exact_type_a_match()
    test_missing_capabilities_field()
    test_empty_capabilities_array()
    test_case_sensitive()
    test_multiple_type_a_requirement_sets()

    print("\n" + "=" * 60)
    print("All edge case tests passed!")
    print("=" * 60)

if __name__ == "__main__":
    main()
