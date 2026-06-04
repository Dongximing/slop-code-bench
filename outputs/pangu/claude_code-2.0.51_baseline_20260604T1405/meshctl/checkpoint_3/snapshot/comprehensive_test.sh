#!/bin/bash
# Comprehensive test script for meshctl

echo "=== Setting up fresh storage ==="
rm -f meshes.json

echo ""
echo "=== Test 1: Create valid mesh ==="
source venv/bin/activate && python meshctl.py mesh create -f test_mesh.yaml
echo "Expected: Full resource JSON with all defaults applied"

echo ""
echo "=== Test 2: Create with minimal YAML (defaults) ==="
source venv/bin/activate && python meshctl.py mesh create -f test_mesh_defaults.yaml
echo "Expected: Resource with memory default 1Gi/1Gi"

echo ""
echo "=== Test 3: Create second mesh with different name ==="
source venv/bin/activate && python meshctl.py mesh create -f test_mesh2.yaml
echo "Expected: alpha-mesh created"

echo ""
echo "=== Test 4: List all meshes ==="
source venv/bin/activate && python meshctl.py mesh list
echo "Expected: Both meshes listed, sorted alphabetically"

echo ""
echo "=== Test 5: Describe existing mesh ==="
source venv/bin/activate && python meshctl.py mesh describe zeta-mesh
echo "Expected: Full zeta-mesh resource"

echo ""
echo "=== Test 6: Describe non-existent mesh ==="
source venv/bin/activate && python meshctl.py mesh describe does-not-exist 2>&1 | head -5
echo "Expected: not_found error"

echo ""
echo "=== Test 7: Delete existing mesh ==="
source venv/bin/activate && python meshctl.py mesh delete zeta-mesh
echo "Expected: Confirmation object"

echo ""
echo "=== Test 8: Delete non-existent mesh ==="
source venv/bin/activate && python meshctl.py mesh delete does-not-exist 2>&1 | head -5
echo "Expected: not_found error"

echo ""
echo "=== Test 9: Validate errors ==="
echo "--- Empty name ---"
source venv/bin/activate && python meshctl.py mesh create -f test_empty.yaml 2>&1 | head -2
echo "--- Invalid name format ---"
source venv/bin/activate && python meshctl.py mesh create -f test_invalid_name.yaml 2>&1 | head -2
echo "--- Duplicate name ---"
source venv/bin/activate && python meshctl.py mesh create -f test_dup.yaml 2>&1 | head -2
echo "--- Invalid instances ---"
source venv/bin/activate && python meshctl.py mesh create -f test_bad_instances.yaml 2>&1 | head -2
echo "--- Invalid runtime ---"
source venv/bin/activate && python meshctl.py mesh create -f test_bad_runtime.yaml 2>&1 | head -2
echo "--- Missing memory limit ---"
source venv/bin/activate && python meshctl.py mesh create -f test_missing_memory_limit.yaml 2>&1 | head -2
echo "--- Missing CPU limit ---"
source venv/bin/activate && python meshctl.py mesh create -f test_cpu_missing_limit.yaml 2>&1 | head -2
echo "--- Request exceeds limit ---"
source venv/bin/activate && python meshctl.py mesh create -f test_req_exceeds_limit.yaml 2>&1 | head -2
echo "--- Bad memory format ---"
source venv/bin/activate && python meshctl.py mesh create -f test_bad_memory.yaml 2>&1 | head -2
echo "--- Bad CPU format ---"
source venv/bin/activate && python meshctl.py mesh create -f test_bad_cpu.yaml 2>&1 | head -2
echo "--- Invalid migration strategy ---"
source venv/bin/activate && python meshctl.py mesh create -f test_bad_migration.yaml 2>&1 | head -2
echo "--- Forbidden autoScaling ---"
source venv/bin/activate && python meshctl.py mesh create -f test_autoscaling.yaml 2>&1 | head -2

echo ""
echo "=== Test 10: Resource quantity parsing ==="
source venv/bin/activate && python meshctl.py mesh create -f test_with_cpu.yaml
echo "Expected: CPU defaults applied (limit=2000m, request=2000m)"

echo ""
echo "=== Test 11: File not found ==="
source venv/bin/activate && python meshctl.py mesh create -f /tmp/nonexistent.yaml 2>&1 | head -2
echo "Expected: parse error"

echo ""
echo "=== All tests completed ==="
