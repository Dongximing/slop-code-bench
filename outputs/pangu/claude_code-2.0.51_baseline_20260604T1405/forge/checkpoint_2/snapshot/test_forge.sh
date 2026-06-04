#!/bin/bash
set -e

echo "=== Forge Tests ==="

DATA_DIR="/tmp/forge_test_data"
FORGE_CMD="python forge.py --data-dir $DATA_DIR"

echo ""
echo "1. Test blueprint create with valid data"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "test-blueprint",
  "requirement_sets": [
    {
      "resource_type": "gpu",
      "resource_count": 4,
      "capabilities": ["nvidia", "ampere"]
    },
    {
      "resource_type": "cpu",
      "resource_count": 2
    }
  ]
}
EOF
RESULT=$($FORGE_CMD blueprint create < /tmp/test_input.json)
echo "Result: $RESULT"
echo "Exit code: $?"
echo ""

echo "2. Test blueprint list (should show 1 blueprint)"
RESULT=$($FORGE_CMD blueprint list)
echo "Result: $RESULT"
echo ""

echo "3. Test blueprint get with the returned UUID"
UUID=$(echo $RESULT | python3 -c "import sys, json; print(json.load(sys.stdin)['uuid'])")
RESULT=$($FORGE_CMD blueprint get $UUID)
echo "Result: $RESULT"
echo ""

echo "4. Test blueprint create with duplicate name (should fail with conflict)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "test-blueprint",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "5. Test blueprint create with missing name (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "6. Test blueprint create with empty name (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "7. Test blueprint create with missing resource_count (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu"
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "8. Test blueprint create with negative resource_count (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": -1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "9. Test blueprint create with zero resource_count (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 0
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "10. Test blueprint create with boolean resource_count (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": true
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "11. Test blueprint create with invalid capabilities type (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1,
      "capabilities": "gpu"
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "12. Test blueprint create with non-string capability (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1,
      "capabilities": ["gpu", 123]
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "13. Test blueprint create with unsupported key (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1,
      "invalid_key": "value"
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "14. Test blueprint create with empty requirement_sets (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint2",
  "requirement_sets": []
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "15. Test blueprint get with non-existent UUID (should return not_found)"
$FORGE_CMD blueprint get non-existent-uuid 2>&1; echo "Exit code: $?"
echo ""

echo "16. Test blueprint delete with non-existent UUID (should return not_found)"
$FORGE_CMD blueprint delete non-existent-uuid 2>&1; echo "Exit code: $?"
echo ""

echo "17. Create more blueprints for delete testing"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint-a",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json > /dev/null

cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint-b",
  "requirement_sets": [
    {
      "resource_type": "gpu",
      "resource_count": 2
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json > /dev/null

cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint-c",
  "requirement_sets": [
    {
      "resource_type": "storage",
      "resource_count": 3
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json > /dev/null
echo "Created 3 blueprints"
$FORGE_CMD blueprint list | python3 -c "import sys, json; print(f'Total: {len(json.load(sys.stdin))}')"
echo ""

echo "18. Test batch delete by names (all should succeed)"
RESULT=$($FORGE_CMD blueprint delete --names blueprint-a,blueprint-b,blueprint-c)
echo "Result: $RESULT"
echo "Exit code: $?"
echo ""

echo "19. Test batch delete with non-existent name (should fail all)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "blueprint-d",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json > /dev/null

# Try to delete blueprint-d and non-existent
$FORGE_CMD blueprint delete --names blueprint-d,non-existent 2>&1; echo "Exit code: $?"
echo ""

echo "20. Test ordering - create blueprints with different timestamps to check ordering"
# Delete everything first
$FORGE_CMD blueprint delete --names blueprint-d > /dev/null 2>/dev/null || true

# Create multiple blueprints
for i in 1 2 3; do
cat > /tmp/test_input.json << EOF
{
  "name": "name${i}",
  "requirement_sets": [
    {
      "resource_type": "cpu",
      "resource_count": $i
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json > /dev/null
done

echo "Blueprints created"
$FORGE_CMD blueprint list
echo ""

echo "21. Test single delete by UUID"
UUID=$($FORGE_CMD blueprint list | python3 -c "import sys, json; print(json.load(sys.stdin)[0]['uuid'])")
echo "Deleting UUID: $UUID"
RESULT=$($FORGE_CMD blueprint delete $UUID)
echo "Result: $RESULT"
echo "Exit code: $?"
echo ""

echo "22. Test delete with empty CSV member (should fail)"
$FORGE_CMD blueprint delete --names blueprint-1,,blueprint-2 2>&1; echo "Exit code: $?"
echo ""

echo "23. Test input is not valid JSON (should fail validation)"
echo "{invalid json}" | $FORGE_CMD blueprint create 2>&1; echo "Exit code: $?"
echo ""

echo "24. Test blueprint create with non-string resource_type (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "test",
  "requirement_sets": [
    {
      "resource_type": 123,
      "resource_count": 1
    }
  ]
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "25. Test blueprint create with non-array requirement_sets (should fail validation)"
cat > /tmp/test_input.json << 'EOF'
{
  "name": "test",
  "requirement_sets": "not-an-array"
}
EOF
$FORGE_CMD blueprint create < /tmp/test_input.json 2>&1; echo "Exit code: $?"
echo ""

echo "=== All tests completed ==="
