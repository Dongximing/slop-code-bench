#!/usr/bin/env python3
"""Comprehensive test suite for meshctl."""

import json
import sys
import os

# Add workspace to path
sys.path.insert(0, '/workspace')

from meshctl import (
    create_mesh, list_meshes, describe_mesh, delete_mesh, update_mesh,
    meshes_db
)

def print_result(name, result, error):
    """Print test result in a readable format."""
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    if error:
        print("ERROR:")
        print(json.dumps(error, indent=2))
    else:
        print(json.dumps(result, indent=2))

def run_tests():
    """Run all tests."""
    passed = 0
    failed = 0

    # Test 1: Create a mesh with instances
    print("\n" + "="*60)
    print("TEST 1: Create mesh with instances")
    print("="*60)

    # Create YAML file
    with open('/workspace/test1.yaml', 'w') as f:
        f.write("""metadata:
  name: web-mesh
spec:
  instances: 3
  network:
    storage:
      size: 5Gi
      ephemeral: false
      className: fast-ssd
    replicationFactor: 3
""")

    result, error = create_mesh('/workspace/test1.yaml')
    print_result("Create web-mesh", result, error)

    if error:
        print("FAILED: Could not create mesh")
        failed += 1
    else:
        print("PASSED: Mesh created successfully")
        passed += 1

    # Test 2: Create mesh without instances (should default to 0, state=Stopped)
    print("\n" + "="*60)
    print("TEST 2: Create mesh without instances (stopped)")
    print("="*60)

    with open('/workspace/test2.yaml', 'w') as f:
        f.write("""metadata:
  name: stopped-mesh
spec:
  network:
    storage:
      size: 1Gi
""")

    result, error = create_mesh('/workspace/test2.yaml')
    print_result("Create stopped-mesh", result, error)

    if error:
        print("FAILED: Could not create stopped mesh")
        failed += 1
    else:
        instances = result['spec'].get('instances')
        if (result['status']['state'] == 'Stopped' and
            result['status']['instances']['ready'] == 0 and
            instances is None):
            print("PASSED: Stopped mesh created correctly")
            passed += 1
        else:
            print("FAILED: Stopped mesh not created correctly")
            failed += 1

    # Test 3: List meshes
    print("\n" + "="*60)
    print("TEST 3: List meshes")
    print("="*60)

    result = list_meshes()
    print_result("List meshes", result, None)

    if 'items' in result and len(result['items']) == 2:
        print("PASSED: List returned 2 meshes")
        passed += 1
    else:
        print("FAILED: List did not return expected meshes")
        failed += 1

    # Test 4: Describe existing mesh
    print("\n" + "="*60)
    print("TEST 4: Describe existing mesh")
    print("="*60)

    result, error = describe_mesh('web-mesh')
    print_result("Describe web-mesh", result, error)

    if error:
        print("FAILED: Could not describe web-mesh")
        failed += 1
    else:
        print("PASSED: web-mesh described successfully")
        passed += 1

    # Test 5: Describe non-existent mesh
    print("\n" + "="*60)
    print("TEST 5: Describe non-existent mesh")
    print("="*60)

    result, error = describe_mesh('non-existent')
    print_result("Describe non-existent", result, error)

    if error and error['errors'][0]['type'] == 'not_found':
        print("PASSED: Correctly returned not_found error")
        passed += 1
    else:
        print("FAILED: Should have returned not_found error")
        failed += 1

    # Test 6: Delete mesh
    print("\n" + "="*60)
    print("TEST 6: Delete mesh")
    print("="*60)

    result, error = delete_mesh('web-mesh')
    print_result("Delete web-mesh", result, error)

    if error:
        print("FAILED: Could not delete web-mesh")
        failed += 1
    else:
        print("PASSED: web-mesh deleted successfully")
        passed += 1

    # Test 7: Verify deletion
    print("\n" + "="*60)
    print("TEST 7: Verify mesh was deleted")
    print("="*60)

    result, error = describe_mesh('web-mesh')
    if error and error['errors'][0]['type'] == 'not_found':
        print("PASSED: Mesh successfully deleted and not found")
        passed += 1
    else:
        print("FAILED: Mesh should not be found after deletion")
        failed += 1

    # Test 8: Update - merge rules
    print("\n" + "="*60)
    print("TEST 8: Update with merge rules")
    print("="*60)

    # First create a mesh to update
    with open('/workspace/test8a.yaml', 'w') as f:
        f.write("""metadata:
  name: merge-test
spec:
  instances: 2
  network:
    storage:
      size: 2Gi
      ephemeral: false
      className: hdd
    replicationFactor: 2
""")

    result, error = create_mesh('/workspace/test8a.yaml')
    print_result("Create merge-test", result, error)

    # Now update with partial spec
    with open('/workspace/test8b.yaml', 'w') as f:
        f.write("""metadata:
  name: merge-test
spec:
  instances: 5
  network:
    storage:
      className: ssd
""")

    result, error = update_mesh('/workspace/test8b.yaml')
    print_result("Update merge-test (partial)", result, error)

    if error:
        print("FAILED: Update failed")
        failed += 1
    else:
        # Verify that className changed but size and ephemeral were preserved
        if (result['spec']['instances'] == 5 and
            result['spec']['network']['storage']['className'] == 'ssd' and
            result['spec']['network']['storage']['size'] == '2Gi' and
            result['spec']['network']['storage']['ephemeral'] == False):
            print("PASSED: Merge rules applied correctly")
            passed += 1
        else:
            print("FAILED: Merge rules not applied correctly")
            failed += 1

    # Test 9: Scale up lifecycle
    print("\n" + "="*60)
    print("TEST 9: Scale up lifecycle")
    print("="*60)

    with open('/workspace/test9.yaml', 'w') as f:
        f.write("""metadata:
  name: scale-test
spec:
  instances: 2
""")

    result, error = create_mesh('/workspace/test9.yaml')
    print_result("Create scale-test", result, error)

    # Scale up to 5
    with open('/workspace/test9b.yaml', 'w') as f:
        f.write("""metadata:
  name: scale-test
spec:
  instances: 5
""")

    result, error = update_mesh('/workspace/test9b.yaml')
    print_result("Update scale-test (scale to 5)", result, error)

    if error:
        print("FAILED: Scale up failed")
        failed += 1
    else:
        # Check transient state
        if (result['status']['stable'] == False and
            result['status']['instances']['ready'] == 2 and
            result['status']['instances']['starting'] == 3 and
            any(c['type'] == 'Scaling' for c in result['status']['conditions'])):
            print("PASSED: Scale up - transient state correct")
            passed += 1
        else:
            print("FAILED: Scale up transient state incorrect")
            failed += 1

    # Test 10: Verify scale-up completion on describe
    print("\n" + "="*60)
    print("TEST 10: Verify scale-up completion on describe")
    print("="*60)

    result, error = describe_mesh('scale-test')
    print_result("Describe scale-test after scale-up", result, error)

    if error:
        print("FAILED: Could not describe scale-test")
        failed += 1
    else:
        # Check final state - Scaling should be omitted, ready = 5
        if (result['status']['stable'] == True and
            result['status']['instances']['ready'] == 5 and
            result['status']['instances']['starting'] == 0 and
            not any(c['type'] == 'Scaling' for c in result['status']['conditions'])):
            print("PASSED: Scale up completed correctly")
            passed += 1
        else:
            print("FAILED: Scale up not completed correctly")
            failed += 1

    # Test 11: Scale down lifecycle
    print("\n" + "="*60)
    print("TEST 11: Scale down lifecycle")
    print("="*60)

    with open('/workspace/test11b.yaml', 'w') as f:
        f.write("""metadata:
  name: scale-test
spec:
  instances: 1
""")

    result, error = update_mesh('/workspace/test11b.yaml')
    print_result("Update scale-test (scale to 1)", result, error)

    if error:
        print("FAILED: Scale down failed")
        failed += 1
    else:
        if (result['status']['stable'] == False and
            result['status']['instances']['ready'] == 1 and
            result['status']['instances']['starting'] == 4 and
            any(c['type'] == 'Scaling' for c in result['status']['conditions'])):
            print("PASSED: Scale down - transient state correct")
            passed += 1
        else:
            print("FAILED: Scale down transient state incorrect")
            failed += 1

    # Test 12: Stop (instances -> 0)
    print("\n" + "="*60)
    print("TEST 12: Stop mesh (scale to 0)")
    print("="*60)

    with open('/workspace/test12.yaml', 'w') as f:
        f.write("""metadata:
  name: stop-test
spec:
  instances: 3
""")

    result, error = create_mesh('/workspace/test12.yaml')
    print_result("Create stop-test", result, error)

    with open('/workspace/test12b.yaml', 'w') as f:
        f.write("""metadata:
  name: stop-test
spec:
  instances: 0
""")

    result, error = update_mesh('/workspace/test12b.yaml')
    print_result("Update stop-test (stop)", result, error)

    if error:
        print("FAILED: Stop failed")
        failed += 1
    else:
        if (result['status']['state'] == 'Stopped' and
            result['status']['instances']['ready'] == 0 and
            result['status']['instances']['starting'] == 0 and
            result['status']['instances']['stopped'] == 3 and
            result['status']['desiredInstancesOnResume'] == 3 and
            any(c['type'] == 'GracefulShutdown' for c in result['status']['conditions'])):
            print("PASSED: Stop operation correct")
            passed += 1
        else:
            print("FAILED: Stop operation incorrect")
            failed += 1

    # Test 13: Resume stopped mesh
    print("\n" + "="*60)
    print("TEST 13: Resume stopped mesh")
    print("="*60)

    with open('/workspace/test13b.yaml', 'w') as f:
        f.write("""metadata:
  name: stop-test
spec:
  instances: 5
""")

    result, error = update_mesh('/workspace/test13b.yaml')
    print_result("Update stop-test (resume to 5)", result, error)

    if error:
        print("FAILED: Resume failed")
        failed += 1
    else:
        if (result['status']['state'] == 'Running' and
            result['status']['instances']['ready'] == 0 and
            result['status']['instances']['starting'] == 5 and
            result['status']['instances']['stopped'] == 0 and
            result['status']['desiredInstancesOnResume'] is None and
            not any(c['type'] == 'GracefulShutdown' for c in result['status']['conditions'])):
            print("PASSED: Resume operation correct")
            passed += 1
        else:
            print("FAILED: Resume operation incorrect")
            failed += 1

    # Test 14: Resume without specifying instances (should use desiredInstancesOnResume)
    print("\n" + "="*60)
    print("TEST 14: Resume without specifying instances")
    print("="*60)

    # First stop again
    with open('/workspace/test14b.yaml', 'w') as f:
        f.write("""metadata:
  name: stop-test
spec:
  instances: 0
""")

    result, error = update_mesh('/workspace/test14b.yaml')

    # Resume without specifying instances in spec (spec.instances is None/omitted)
    with open('/workspace/test14c.yaml', 'w') as f:
        f.write("""metadata:
  name: stop-test
spec:
  # instances omitted - should use desiredInstancesOnResume (3)
""")

    result, error = update_mesh('/workspace/test14c.yaml')
    print_result("Update stop-test (resume without instances)", result, error)

    if error:
        print("FAILED: Resume without instances failed")
        failed += 1
    else:
        if (result['status']['instances']['starting'] == 3):
            print("PASSED: Resume used desiredInstancesOnResume")
            passed += 1
        else:
            print("FAILED: Resume did not use desiredInstancesOnResume correctly")
            failed += 1

    # Test 15: Storage default
    print("\n" + "="*60)
    print("TEST 15: Storage default to 1Gi")
    print("="*60)

    with open('/workspace/test15.yaml', 'w') as f:
        f.write("""metadata:
  name: default-storage
spec:
  instances: 2
""")

    result, error = create_mesh('/workspace/test15.yaml')
    print_result("Create default-storage", result, error)

    if error:
        print("FAILED: Could not create mesh with default storage")
        failed += 1
    else:
        if result['spec']['network']['storage']['size'] == '1Gi':
            print("PASSED: Storage defaulted to 1Gi")
            passed += 1
        else:
            print("FAILED: Storage should default to 1Gi")
            failed += 1

    # Test 16: Replication factor default
    print("\n" + "="*60)
    print("TEST 16: Replication factor default")
    print("="*60)

    if result and 'spec' in result and 'network' in result['spec']:
        rf = result['spec']['network'].get('replicationFactor')
        if rf == min(3, 2):  # instances=2, so min(3,2)=2
            print("PASSED: Replication factor defaulted correctly")
            passed += 1
        else:
            print(f"FAILED: Replication factor should be {min(3,2)}, got {rf}")
            failed += 1
    else:
        print("FAILED: Could not check replication factor")
        failed += 1

    # Test 17: Replication factor validation
    print("\n" + "="*60)
    print("TEST 17: Replication factor validation")
    print("="*60)

    with open('/workspace/test17.yaml', 'w') as f:
        f.write("""metadata:
  name: bad-rf
spec:
  instances: 2
  network:
    replicationFactor: 5
""")

    result, error = create_mesh('/workspace/test17.yaml')
    if error and any('replicationFactor' in str(e) for e in error.get('errors', [])):
        print("PASSED: Replication factor validation worked")
        passed += 1
    else:
        print("FAILED: Should have rejected replication factor > instances")
        failed += 1

    # Test 18: Storage immutability
    print("\n" + "="*60)
    print("TEST 18: Storage size immutability")
    print("="*60)

    with open('/workspace/test18a.yaml', 'w') as f:
        f.write("""metadata:
  name: immutable-test
spec:
  instances: 1
  network:
    storage:
      size: 2Gi
""")

    create_mesh('/workspace/test18a.yaml')

    with open('/workspace/test18b.yaml', 'w') as f:
        f.write("""metadata:
  name: immutable-test
spec:
  network:
    storage:
      size: 3Gi  # Should be rejected
""")

    result, error = update_mesh('/workspace/test18b.yaml')
    if error and error['errors'][0]['type'] == 'immutable':
        print("PASSED: Storage size correctly rejected as immutable")
        passed += 1
    else:
        print("FAILED: Should have rejected storage size change")
        failed += 1

    # Test 19: Conditions sorting
    print("\n" + "="*60)
    print("TEST 19: Conditions sorting")
    print("="*60)

    with open('/workspace/test19a.yaml', 'w') as f:
        f.write("""metadata:
  name: conditions-test
spec:
  instances: 1
""")

    create_mesh('/workspace/test19a.yaml')

    # Add multiple conditions
    with open('/workspace/test19b.yaml', 'w') as f:
        f.write("""metadata:
  name: conditions-test
spec:
  instances: 2
""")

    result, error = update_mesh('/workspace/test19b.yaml')

    # Check that conditions are sorted by type
    if result and 'status' in result and 'conditions' in result['status']:
        types = [c['type'] for c in result['status']['conditions']]
        if types == sorted(types):
            print("PASSED: Conditions are sorted by type")
            passed += 1
        else:
            print(f"FAILED: Conditions not sorted. Order: {types}")
            failed += 1
    else:
        print("FAILED: Could not check conditions sorting")
        failed += 1

    # Test 20: Ephemeral storage output format
    print("\n" + "="*60)
    print("TEST 20: Ephemeral storage output format")
    print("="*60)

    with open('/workspace/test20.yaml', 'w') as f:
        f.write("""metadata:
  name: ephemeral-test
spec:
  instances: 1
  network:
    storage:
      ephemeral: true
      size: 5Gi  # size should be omitted in output when ephemeral=true
""")

    result, error = create_mesh('/workspace/test20.yaml')
    print_result("Create ephemeral-test", result, error)

    if error:
        print("FAILED: Could not create ephemeral mesh")
        failed += 1
    else:
        storage = result['spec']['network']['storage']
        if storage.get('ephemeral') == True and 'size' not in storage:
            print("PASSED: Ephemeral storage output correct (no size)")
            passed += 1
        else:
            print(f"FAILED: Ephemeral storage output incorrect. Got: {storage}")
            failed += 1

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Total: {passed + failed}")

    if failed == 0:
        print("\nAll tests passed!")
        return 0
    else:
        print(f"\n{failed} test(s) failed!")
        return 1

if __name__ == "__main__":
    sys.exit(run_tests())
