#!/bin/bash
# Test script for configuration service

SERVER_PID=""
VENV="/workspace/config_service/venv/bin"
VENV_PYTHON="$VENV/python"
BASE_URL="http://127.0.0.1:18081"

# Start server
start_server() {
    echo "Starting server..."
    $VENV_PYTHON config_server.py --address 127.0.0.1 --port 18081 &
    SERVER_PID=$!
    sleep 3  # Give more time for server to start
    echo "Server PID: $SERVER_PID"
}

# Stop server
stop_server() {
    echo "Stopping server..."
    kill $SERVER_PID 2>/dev/null || true
}

# Test healthcheck
test_healthcheck() {
    echo "=== Testing Healthcheck ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" $BASE_URL/healthz)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"ok":true'* ]] && echo "PASS" || echo "FAIL"
}

# Test create base config
test_create_base() {
    echo "=== Test 1: Create base config ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"config":{"db":{"host":"prod.db","pool":4}}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "201" && "$body" == *'"version":1'* && "$body" == *'"active":true'* ]] && echo "PASS" || echo "FAIL"
}

# Test create app with include
test_create_app_with_include() {
    echo "=== Test 2: Create app config with includes ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/billing" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod","app":"billing"},"includes":[{"name":"base","scope":{"env":"prod"}}],"config":{"db":{"pool":8},"feature_flags":["a","b"]}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "201" && "$body" == *'"version":1'* ]] && echo "PASS" || echo "FAIL"
}

# Test resolve billing config
test_resolve() {
    echo "=== Test 3: Resolve billing config ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/billing:resolve" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod","app":"billing"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    if [[ "$http_status" == "200" && "$body" == *'"resolved_config"'* ]]; then
        echo "PASS"
    else
        echo "FAIL: Body=$body"
    fi
}

# Test list versions
test_list_versions() {
    echo "=== Test 4: List versions for base ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base:versions" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"versions"'* ]] && echo "PASS" || echo "FAIL"
}

# Test create second version
test_create_second_version() {
    echo "=== Test 5: Create second version of base ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"config":{"db":{"host":"prod.db","pool":4,"retries":3}}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "201" && "$body" == *'"version":2'* && "$body" == *'"active":true'* ]] && echo "PASS" || echo "FAIL"
}

# Test get active
test_get_active() {
    echo "=== Test 6: Get active version ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base:active" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"version":2'* && "$body" == *'"active":true'* ]] && echo "PASS" || echo "FAIL"
}

# Test activate version
test_activate() {
    echo "=== Test 7: Activate version 1 ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base/1:activate" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"version":1'* ]] && echo "PASS" || echo "FAIL"
}

# Test get version
test_get_version() {
    echo "=== Test 8: Get version 1 ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base/1" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"version":1'* && "$body" == *'"config"'* ]] && echo "PASS" || echo "FAIL"
}

# Test rollback
test_rollback() {
    echo "=== Test 9: Rollback to version 2 ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base:rollback" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"to_version":2}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "200" && "$body" == *'"version":2'* ]] && echo "PASS" || echo "FAIL"
}

# Test create with inherits_active
test_create_with_inherits() {
    echo "=== Test 10: Create version with inherits_active ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"config":{"db":{"retries":5}},"inherits_active":true}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "201" && "$body" == *'"version":3'* ]] && echo "PASS" || echo "FAIL"
}

# Test idempotency
test_idempotency() {
    echo "=== Test 11: Test idempotency (same request) ==="
    body='{"scope":{"env":"prod"},"config":{"db":{"retries":5}},"inherits_active":true}'
    response1=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d "$body" 2>/dev/null)
    http_status1=$(echo "$response1" | grep "HTTP_STATUS:" | cut -d: -f2)
    body1=$(echo "$response1" | sed '/HTTP_STATUS:/d')
    ver1=$(echo "$body1" | grep -o '"version":[0-9]*' | cut -d: -f2)

    # Same request again - should NOT create new version
    response2=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d "$body" 2>/dev/null)
    http_status2=$(echo "$response2" | grep "HTTP_STATUS:" | cut -d: -f2)
    body2=$(echo "$response2" | sed '/HTTP_STATUS:/d')
    ver2=$(echo "$body2" | grep -o '"version":[0-9]*' | cut -d: -f2)

    echo "First request status: $http_status1, version: $ver1"
    echo "Second request status: $http_status2, version: $ver2"
    if [[ "$http_status1" == "$http_status2" && "$ver1" == "$ver2" ]]; then
        echo "PASS - Same response (idempotent)"
    else
        echo "FAIL - Response differs"
    fi
}

# Test error handling
test_error_handling() {
    echo "=== Test 12: Error handling - invalid JSON ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/billing:resolve" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d 'invalid json' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "400" ]] && echo "PASS" || echo "FAIL"
}

# Test different scope
test_different_scope() {
    echo "=== Test 13: Create config with different scope (env=dev) ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"dev"},"config":{"db":{"host":"dev.db","pool":2}}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    echo "Body: $body"
    [[ "$http_status" == "201" && "$body" == *'"version":1'* ]] && echo "PASS" || echo "FAIL"
}

# Test resolve with version
test_resolve_with_version() {
    echo "=== Test 14: Resolve with explicit version ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base:resolve" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"version":2}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    # Check if resolved config contains retries from version 2 (version 3 has retries=5)
    if [[ "$http_status" == "200" ]]; then
        echo "PASS - Resolved successfully"
    else
        echo "FAIL: Body=$body"
    fi
}

# Test resolve graph
test_resolve_graph() {
    echo "=== Test 15: Check resolution graph ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/billing:resolve" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod","app":"billing"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    if [[ "$http_status" == "200" && "$body" == *'"resolution_graph"'* ]]; then
        echo "PASS - Resolution graph present"
    else
        echo "FAIL"
    fi
}

# Test missing version
test_missing_version() {
    echo "=== Test 16: Get nonexistent version ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base/9999" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    if [[ "$http_status" == "404" ]]; then
        echo "PASS - Not found returned"
    else
        echo "FAIL: Expected 404, got $http_status"
    fi
}

# Test missing scope
test_missing_scope() {
    echo "=== Test 17: Create version for nonexistent scope ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/unknown" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"config":{"test":1}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Status: $http_status"
    if [[ "$http_status" == "201" ]]; then
        echo "PASS - Created new config for new scope"
    else
        echo "FAIL"
    fi
}

# Test cycle detection
test_cycle_detection() {
    echo "=== Test 18: Cycle detection ==="
    # Create a config that includes itself - this should fail at create time
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/cyclic" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"test":"cycle"},"includes":[{"name":"cyclic","scope":{"test":"cycle"}}],"config":{"test":"value"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    echo "Create Status: $http_status"
    # During creation, cycle is not detected (includes are not validated for existence until resolve)
    # So this should create v1
    if [[ "$http_status" == "201" ]]; then
        # Now try to resolve - should detect cycle
        resolve_response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/cyclic:resolve" \
            -H "Content-Type: application/json; charset=utf-8" \
            -d '{"scope":{"test":"cycle"}}' 2>/dev/null)
        resolve_status=$(echo "$resolve_response" | grep "HTTP_STATUS:" | cut -d: -f2)
        resolve_body=$(echo "$resolve_response" | sed '/HTTP_STATUS:/d')
        echo "Resolve Status: $resolve_status"
        echo "Resolve Body: $resolve_body"
        if [[ "$resolve_status" == "409" ]]; then
            echo "PASS - Cycle detected during resolve"
        else
            echo "FAIL - Cycle not detected during resolve, got $resolve_status"
        fi
    else
        echo "FAIL - Could not create cyclic config"
    fi
}

# Test rollback with >= active version
test_rollback_conflict() {
    echo "=== Test 19: Rollback to same or higher version (conflict) ==="
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/base:rollback" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"env":"prod"},"to_version":3}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    if [[ "$http_status" == "409" ]]; then
        echo "PASS - Conflict (can't rollback to same/higher version)"
    else
        echo "FAIL: Expected 409, got $http_status"
    fi
}

# Test nested includes
test_nested_includes() {
    echo "=== Test 20: Nested includes ==="
    # Create level1
    curl -s -X POST "$BASE_URL/v1/configs/level1" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"app":"test"},"config":{"level1":"value1"}}' > /dev/null
    sleep 0.5
    # Create level2 that includes level1
    curl -s -X POST "$BASE_URL/v1/configs/level2" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"app":"test"},"includes":[{"name":"level1","scope":{"app":"test"}}],"config":{"level2":"value2"}}' > /dev/null
    sleep 0.5
    # Create level3 that includes level2
    response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/level3" \
        -H "Content-Type: application/json; charset=utf-8" \
        -d '{"scope":{"app":"test"},"includes":[{"name":"level2","scope":{"app":"test"}}],"config":{"level3":"value3"}}' 2>/dev/null)
    http_status=$(echo "$response" | grep "HTTP_STATUS:" | cut -d: -f2)
    body=$(echo "$response" | sed '/HTTP_STATUS:/d')
    if [[ "$http_status" == "201" ]]; then
        echo "PASS - Nested includes created"
        # Now resolve level3
        resolve_response=$(curl -s -w "\nHTTP_STATUS:%{http_code}" -X POST "$BASE_URL/v1/configs/level3:resolve" \
            -H "Content-Type: application/json; charset=utf-8" \
            -d '{"scope":{"app":"test"}}' 2>/dev/null)
        resolve_status=$(echo "$resolve_response" | grep "HTTP_STATUS:" | cut -d: -f2)
        resolve_body=$(echo "$resolve_response" | sed '/HTTP_STATUS:/d')
        if [[ "$resolve_status" == "200" && "$resolve_body" == *'"level1"'* && "$resolve_body" == *'"level2"'* && "$resolve_body" == *'"level3"'* ]]; then
            echo "PASS - Nested resolution works"
        else
            echo "FAIL - Nested resolution failed"
        fi
    else
        echo "FAIL: Could not create nested includes"
    fi
}

# Cleanup
cleanup() {
    echo ""
    echo "Cleaning up..."
    stop_server
    kill $(lsof -t -i:18081) 2>/dev/null || true
    sleep 1
}

# Main test runner
trap cleanup EXIT

start_server
test_healthcheck
test_create_base
test_create_app_with_include
test_resolve
test_list_versions
test_create_second_version
test_get_active
test_activate
test_get_version
test_rollback
test_create_with_inherits
test_idempotency
test_error_handling
test_different_scope
test_resolve_with_version
test_resolve_graph
test_missing_version
test_missing_scope
test_cycle_detection
test_rollback_conflict
test_nested_includes

echo ""
echo "=== All tests completed ==="
