#!/usr/bin/env python3
"""Test suite for meshctl implementation."""

import json
import sys
import os

# Add workspace to path
sys.path.insert(0, '/workspace')

from meshctl import (
    create_mesh, describe_mesh, delete_mesh, update_mesh, list_meshes,
    load_yaml_file, validate_size, validate_instances, validate_replication_factor,
    get_initial_conditions, sort_conditions, build_storage_output, MEShes,
)


def test_validate_size():
    """Test storage size validation."""
    assert validate_size("1Gi") == True
    assert validate_size("1024Mi") == True
    assert validate_size("0") == True
    assert validate_size("5") == True
    assert validate_size("-1") == False
    assert validate_size("abc") == False
    assert validate_size("") == False
    print("✓ test_validate_size passed")


def test_validate_instances():
    """Test instances validation."""
    assert validate_instances(None) == []
    assert len(validate_instances("invalid")) > 0
    assert len(validate_instances(-1)) > 0
    assert validate_instances(0) == []
    assert validate_instances(5) == []
    print("✓ test_validate_instances passed")


def test_validate_replication_factor():
    """Test replication factor validation."""
    assert validate_replication_factor(None, 5) == []
    assert len(validate_replication_factor(0, 5)) > 0
    assert len(validate_replication_factor(10, 5)) > 0
    assert validate_replication_factor(3, 5) == []
    assert validate_replication_factor(5, 5) == []
    print("✓ test_validate_replication_factor passed")


def test_get_initial_conditions():
    """Test initial conditions creation."""
    conditions = get_initial_conditions()
    assert len(conditions) == 2
    assert conditions[0]["type"] == "Healthy"
    assert conditions[0]["status"] == "True"
    assert conditions[0]["message"] == ""
    assert conditions[1]["type"] == "PrechecksPassed"
    assert conditions[1]["status"] == "True"
    assert conditions[1]["message"] == ""
    print("✓ test_get_initial_conditions passed")


def test_sort_conditions():
    """Test condition sorting and uniqueness."""
    conditions = [
        {"type": "Zebra", "status": "True"},
        {"type": "Apple", "status": "False"},
        {"type": "Zebra", "status": "True"},  # Duplicate - should be removed
        {"type": "Banana", "status": "True"},
    ]
    sorted_conditions = sort_conditions(conditions)
    assert len(sorted_conditions) == 3  # Zebra deduplicated
    assert sorted_conditions[0]["type"] == "Apple"
    assert sorted_conditions[1]["type"] == "Banana"
    assert sorted_conditions[2]["type"] == "Zebra"
    print("✓ test_sort_conditions passed")


def test_build_storage_output():
    """Test storage output building."""
    # Ephemeral true - should only return ephemeral
    storage = {"ephemeral": True, "size": "1Gi", "className": "fast"}
    output = build_storage_output(storage)
    assert output == {"ephemeral": True}

    # Ephemeral false - should return ephemeral and size, optionally className
    storage = {"ephemeral": False, "size": "10Gi", "className": "slow"}
    output = build_storage_output(storage)
    assert output["ephemeral"] == False
    assert output["size"] == "10Gi"
    assert output["className"] == "slow"

    # Ephemeral false, no size (shouldn't happen but test)
    storage = {"ephemeral": False}
    output = build_storage_output(storage)
    assert output["ephemeral"] == False
    assert "size" not in output

    print("✓ test_build_storage_output passed")


def test_create_mesh_with_instances():
    """Test creating a mesh with instances."""
    data = {
        "metadata": {"name": "test-mesh-1"},
        "spec": {
            "instances": 3,
            "network": {
                "storage": {"size": "5Gi"},
                "replicationFactor": 3
            }
        }
    }
    result, errors = create_mesh(data)
    assert errors == []
    assert result["metadata"]["name"] == "test-mesh-1"
    assert result["spec"]["instances"] == 3
    assert result["status"]["state"] == "Running"
    assert result["status"]["instances"]["ready"] == 3
    assert result["status"]["stable"] == True
    assert len(result["status"]["conditions"]) == 2
    print("✓ test_create_mesh_with_instances passed")


def test_create_mesh_without_instances():
    """Test creating a mesh without instances (stopped)."""
    data = {
        "metadata": {"name": "test-mesh-stopped"},
        "spec": {}
    }
    result, errors = create_mesh(data)
    assert errors == []
    assert result["metadata"]["name"] == "test-mesh-stopped"
    assert result["spec"]["instances"] == None
    assert result["status"]["state"] == "Stopped"
    assert result["status"]["instances"]["ready"] == 0
    print("✓ test_create_mesh_without_instances passed")


def test_create_mesh_default_size():
    """Test that size defaults to 1Gi when omitted."""
    data = {
        "metadata": {"name": "test-default-size"},
        "spec": {
            "instances": 1
        }
    }
    result, errors = create_mesh(data)
    assert errors == []
    assert result["spec"]["network"]["storage"]["size"] == "1Gi"
    print("✓ test_create_mesh_default_size passed")


def test_create_mesh_duplicate():
    """Test creating a mesh with duplicate name."""
    data = {
        "metadata": {"name": "test-duplicate"},
        "spec": {"instances": 1}
    }
    create_mesh(data)
    result, errors = create_mesh(data)
    assert len(errors) > 0
    assert errors[0]["type"] == "already_exists"
    print("✓ test_create_mesh_duplicate passed")


def test_create_mesh_missing_name():
    """Test creating a mesh without name."""
    data = {"spec": {"instances": 1}}
    result, errors = create_mesh(data)
    assert len(errors) > 0
    assert errors[0]["field"] == "metadata.name"
    print("✓ test_create_mesh_missing_name passed")


def test_create_mesh_invalid_size():
    """Test creating a mesh with invalid storage size."""
    data = {
        "metadata": {"name": "test-invalid-size"},
        "spec": {
            "network": {
                "storage": {"size": "invalid"}
            }
        }
    }
    result, errors = create_mesh(data)
    assert len(errors) > 0
    assert errors[0]["field"] == "spec.network.storage.size"
    print("✓ test_create_mesh_invalid_size passed")


def test_create_mesh_replication_factor_exceeds_instances():
    """Test creating a mesh where replication factor > instances."""
    data = {
        "metadata": {"name": "test-rf-exceeds"},
        "spec": {
            "instances": 2,
            "network": {
                "replicationFactor": 5
            }
        }
    }
    result, errors = create_mesh(data)
    assert len(errors) > 0
    assert errors[0]["field"] == "spec.network.replicationFactor"
    print("✓ test_create_mesh_replication_factor_exceeds_instances passed")


def test_describe_mesh():
    """Test describing a mesh."""
    data = {
        "metadata": {"name": "test-describe"},
        "spec": {"instances": 2}
    }
    create_mesh(data)
    result, errors = describe_mesh("test-describe")
    assert errors == []
    assert result["metadata"]["name"] == "test-describe"
    assert "network" in result["spec"]
    print("✓ test_describe_mesh passed")


def test_describe_mesh_not_found():
    """Test describing a non-existent mesh."""
    result, errors = describe_mesh("non-existent")
    assert result is None
    assert len(errors) > 0
    assert errors[0]["type"] == "not_found"
    print("✓ test_describe_mesh_not_found passed")


def test_delete_mesh():
    """Test deleting a mesh."""
    data = {
        "metadata": {"name": "test-delete"},
        "spec": {"instances": 1}
    }
    create_mesh(data)
    assert "test-delete" in MEShes
    result, errors = delete_mesh("test-delete")
    assert errors == []
    assert "test-delete" not in MEShes
    print("✓ test_delete_mesh passed")


def test_delete_mesh_not_found():
    """Test deleting a non-existent mesh."""
    result, errors = delete_mesh("non-existent")
    assert result is None
    assert len(errors) > 0
    assert errors[0]["type"] == "not_found"
    print("✓ test_delete_mesh_not_found passed")


def test_list_meshes():
    """Test listing meshes."""
    # Create test meshes
    for i in range(3):
        data = {
            "metadata": {"name": f"test-list-{i}"},
            "spec": {"instances": 1}
        }
        create_mesh(data)

    result = list_meshes()
    assert len(result) == 3
    names = [m["metadata"]["name"] for m in result]
    assert names == ["test-list-0", "test-list-1", "test-list-2"]
    print("✓ test_list_meshes passed")


def test_scale_up():
    """Test scaling up (increasing instances)."""
    data = {
        "metadata": {"name": "test-scale-up"},
        "spec": {"instances": 2}
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-scale-up"},
        "spec": {"instances": 5}
    }
    result, errors = update_mesh(update_data)
    assert errors == []

    # Check update response
    assert result["status"]["instances"]["ready"] == 2  # Previous count
    assert result["status"]["instances"]["starting"] == 3  # New - old

    # Check that Scaling condition was added
    conditions = result["status"]["conditions"]
    scaling = [c for c in conditions if c["type"] == "Scaling"]
    assert len(scaling) == 1
    assert scaling[0]["status"] == "True"
    print("✓ test_scale_up passed")


def test_scale_down():
    """Test scaling down (decreasing instances)."""
    data = {
        "metadata": {"name": "test-scale-down"},
        "spec": {"instances": 5}
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-scale-down"},
        "spec": {"instances": 2}
    }
    result, errors = update_mesh(update_data)
    assert errors == []

    # Check update response
    assert result["status"]["instances"]["ready"] == 2
    assert result["status"]["instances"]["starting"] == 0
    assert result["status"]["instances"]["stopped"] == 3

    print("✓ test_scale_down passed")


def test_stop_mesh():
    """Test stopping a mesh (instances -> 0)."""
    data = {
        "metadata": {"name": "test-stop"},
        "spec": {"instances": 3}
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-stop"},
        "spec": {"instances": 0}
    }
    result, errors = update_mesh(update_data)
    assert errors == []

    # Check that GracefulShutdown was added
    conditions = result["status"]["conditions"]
    gs = [c for c in conditions if c["type"] == "GracefulShutdown"]
    assert len(gs) == 1

    # Check status fields
    assert result["status"]["state"] == "Stopped"
    assert result["status"]["desiredInstancesOnResume"] == 3
    print("✓ test_stop_mesh passed")


def test_resume_mesh():
    """Test resuming a stopped mesh."""
    # First create and stop the mesh
    data = {
        "metadata": {"name": "test-resume"},
        "spec": {"instances": 3}
    }
    create_mesh(data)

    # Stop it
    stop_data = {
        "metadata": {"name": "test-resume"},
        "spec": {"instances": 0}
    }
    update_mesh(stop_data)

    # Resume it
    resume_data = {
        "metadata": {"name": "test-resume"},
        "spec": {"instances": 5}
    }
    result, errors = update_mesh(resume_data)
    assert errors == []

    # Check that GracefulShutdown was removed
    conditions = result["status"]["conditions"]
    gs = [c for c in conditions if c["type"] == "GracefulShutdown"]
    assert len(gs) == 0

    # Check desiredInstancesOnResume was removed
    assert "desiredInstancesOnResume" not in result["status"]

    # Check status fields (resume response)
    assert result["status"]["instances"]["ready"] == 0
    assert result["status"]["instances"]["starting"] == 5
    assert result["status"]["state"] == "Running"

    print("✓ test_resume_mesh passed")


def test_storage_immutable_size():
    """Test that storage size is immutable after creation."""
    data = {
        "metadata": {"name": "test-immutable"},
        "spec": {
            "network": {
                "storage": {"size": "5Gi"}
            }
        }
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-immutable"},
        "spec": {
            "network": {
                "storage": {"size": "10Gi"}
            }
        }
    }
    result, errors = update_mesh(update_data)
    assert len(errors) > 0
    assert errors[0]["type"] == "immutable"
    assert errors[0]["field"] == "spec.network.storage.size"
    print("✓ test_storage_immutable_size passed")


def test_update_not_found():
    """Test updating a non-existent mesh."""
    data = {
        "metadata": {"name": "non-existent"},
        "spec": {"instances": 1}
    }
    result, errors = update_mesh(data)
    assert result is None
    assert len(errors) > 0
    assert errors[0]["type"] == "not_found"
    print("✓ test_update_not_found passed")


def test_scale_up_then_describe():
    """Test that Scaling condition is omitted on describe."""
    data = {
        "metadata": {"name": "test-scale-describe"},
        "spec": {"instances": 2}
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-scale-describe"},
        "spec": {"instances": 5}
    }
    update_mesh(update_data)

    # Describe should not have Scaling condition (transient removed)
    result, errors = describe_mesh("test-scale-describe")
    assert errors == []

    conditions = result["status"]["conditions"]
    scaling = [c for c in conditions if c["type"] == "Scaling"]
    # Scaling should be removed on describe
    assert len(scaling) == 0

    # Status instances should reflect stable state
    assert result["status"]["instances"]["ready"] == 5
    assert result["status"]["instances"]["starting"] == 0

    print("✓ test_scale_up_then_describe passed")


def test_stop_then_describe():
    """Test that GracefulShutdown persists until resume."""
    data = {
        "metadata": {"name": "test-stop-describe"},
        "spec": {"instances": 3}
    }
    create_mesh(data)

    update_data = {
        "metadata": {"name": "test-stop-describe"},
        "spec": {"instances": 0}
    }
    update_mesh(update_data)

    # Describe should still have GracefulShutdown
    result, errors = describe_mesh("test-stop-describe")
    assert errors == []

    conditions = result["status"]["conditions"]
    gs = [c for c in conditions if c["type"] == "GracefulShutdown"]
    assert len(gs) == 1

    # Should have desiredInstancesOnResume
    assert "desiredInstancesOnResume" in result["status"]
    assert result["status"]["desiredInstancesOnResume"] == 3

    # State should be Stopped
    assert result["status"]["state"] == "Stopped"

    print("✓ test_stop_then_describe passed")


def test_storage_output_ephemeral():
    """Test storage output based on ephemeral flag."""
    # Test with ephemeral true
    data = {
        "metadata": {"name": "test-ephemeral"},
        "spec": {
            "instances": 1,
            "network": {
                "storage": {"ephemeral": True, "size": "1Gi", "className": "fast"}
            }
        }
    }
    create_mesh(data)

    result, errors = describe_mesh("test-ephemeral")
    assert errors == []

    storage = result["spec"]["network"]["storage"]
    assert storage["ephemeral"] == True
    assert "size" not in storage  # size should not appear when ephemeral is true
    assert "className" not in storage  # className should not appear

    # Test with ephemeral false
    update_data = {
        "metadata": {"name": "test-ephemeral"},
        "spec": {
            "network": {
                "storage": {"ephemeral": False}
            }
        }
    }
    update_mesh(update_data)

    result, errors = describe_mesh("test-ephemeral")
    assert errors == []

    storage = result["spec"]["network"]["storage"]
    assert storage["ephemeral"] == False
    assert "size" in storage

    print("✓ test_storage_output_ephemeral passed")


def test_replication_factor_update_validation():
    """Test replication factor update validation."""
    data = {
        "metadata": {"name": "test-rf-validation"},
        "spec": {"instances": 3}
    }
    create_mesh(data)

    # Invalid: rf > instances
    update_data = {
        "metadata": {"name": "test-rf-validation"},
        "spec": {"network": {"replicationFactor": 5}}
    }
    result, errors = update_mesh(update_data)
    assert len(errors) > 0
    assert errors[0]["field"] == "spec.network.replicationFactor"

    # Valid update
    update_data = {
        "metadata": {"name": "test-rf-validation"},
        "spec": {"network": {"replicationFactor": 2}}
    }
    result, errors = update_mesh(update_data)
    assert errors == []
    assert result["spec"]["network"]["replicationFactor"] == 2

    print("✓ test_replication_factor_update_validation passed")


def test_conditions_sorting():
    """Test that conditions are sorted by type ascending."""
    # Create a mesh and add conditions in random order
    data = {
        "metadata": {"name": "test-sort-conditions"},
        "spec": {"instances": 1}
    }
    create_mesh(data)

    # Update to add multiple conditions
    update_data = {
        "metadata": {"name": "test-sort-conditions"},
        "spec": {"instances": 2}
    }
    result, errors = update_mesh(update_data)

    # Conditions should be sorted
    conditions = result["status"]["conditions"]
    types = [c["type"] for c in conditions]
    assert types == sorted(types), f"Conditions not sorted: {types}"

    print("✓ test_conditions_sorting passed")


def cleanup_meshes():
    """Clean up all test meshes."""
    global MEShes
    MEShes = {}


def run_all_tests():
    """Run all tests."""
    test_functions = [
        test_validate_size,
        test_validate_instances,
        test_validate_replication_factor,
        test_get_initial_conditions,
        test_sort_conditions,
        test_build_storage_output,
        test_create_mesh_with_instances,
        test_create_mesh_without_instances,
        test_create_mesh_default_size,
        test_create_mesh_duplicate,
        test_create_mesh_missing_name,
        test_create_mesh_invalid_size,
        test_create_mesh_replication_factor_exceeds_instances,
        test_describe_mesh,
        test_describe_mesh_not_found,
        test_delete_mesh,
        test_delete_mesh_not_found,
        test_list_meshes,
        test_scale_up,
        test_scale_down,
        test_stop_mesh,
        test_resume_mesh,
        test_storage_immutable_size,
        test_update_not_found,
        test_scale_up_then_describe,
        test_stop_then_describe,
        test_storage_output_ephemeral,
        test_replication_factor_update_validation,
        test_conditions_sorting,
    ]

    total = len(test_functions)
    passed = 0
    failed = 0

    for test_func in test_functions:
        try:
            cleanup_meshes()
            test_func()
            passed += 1
        except Exception as e:
            print(f"✗ {test_func.__name__} failed: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed, {total} total")
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
