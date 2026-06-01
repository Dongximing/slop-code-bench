#!/usr/bin/env python3
"""Test cache functionality"""
import json
import subprocess
import time
import http.client

def post_request(data):
    conn = http.client.HTTPConnection('localhost', 8080)
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(data) if data is not None else None
    conn.request('POST', '/v1/execute', body, headers)
    response = conn.getresponse()
    result = response.read().decode('utf-8')
    conn.close()
    return response.status, result

def test_cache_miss_then_hit():
    print("Test: Cache miss followed by cache hit")
    
    # First request - should be cached
    status, body = post_request({"command": "echo 'test'", "timeout": 5})
    assert status == 201
    first = json.loads(body)
    assert first['cached'] == False, "First request should be a cache miss"
    first_id = first['id']
    
    # Second request - identical, should be cached
    status, body = post_request({"command": "echo 'test'", "timeout": 5})
    assert status == 201
    second = json.loads(body)
    assert second['cached'] == True, "Second request should be a cache hit"
    second_id = second['id']
    
    # IDs should be different
    assert first_id != second_id, "IDs should be different even on cache hit"
    
    print("  PASSED")
    return True

def test_different_files():
    print("Test: Different files = cache miss")
    
    post_request({"command": "cat input.txt", "files": {"input.txt": "version 1"}})
    status, body = post_request({"command": "cat input.txt", "files": {"input.txt": "version 2"}})
    assert status == 201
    result = json.loads(body)
    assert result['cached'] == False, "Different file content should be cache miss"
    assert result['stdout'] == "version 2"
    
    print("  PASSED")
    return True

def test_force_bypass():
    print("Test: Force bypass cache")
    
    # Run command with track to see it changes
    status, body = post_request({"command": "date +%s > t.txt && cat t.txt", "track": ["*.txt"]})
    assert status == 201
    first = json.loads(body)
    first_time = first['stdout'].strip()
    first_file = first.get('files', {}).get('t.txt', '').strip()
    
    # Same command - cache hit
    status, body = post_request({"command": "date +%s > t.txt && cat t.txt", "track": ["*.txt"]})
    assert status == 201
    second = json.loads(body)
    assert second['cached'] == True
    assert second['stdout'] == first_time
    
    # Force re-run
    status, body = post_request({"command": "date +%s > t.txt && cat t.txt", "track": ["*.txt"], "force": True})
    assert status == 201
    third = json.loads(body)
    assert third['cached'] == False
    
    # Cache should be updated
    status, body = post_request({"command": "date +%s > t.txt && cat t.txt", "track": ["*.txt"]})
    assert status == 201
    fourth = json.loads(body)
    assert fourth['cached'] == True
    
    print("  PASSED")
    return True

def test_command_chain_cache():
    print("Test: Command chains with cache")
    
    chain = [{"cmd": "echo 'step 1' > out.txt"}, {"cmd": "echo 'step 2' >> out.txt"}]
    
    # First run
    status, body = post_request({"command": chain, "track": ["*.txt"]})
    assert status == 201
    first = json.loads(body)
    assert first['cached'] == False
    
    # Cache hit
    status, body = post_request({"command": chain, "track": ["*.txt"]})
    assert status == 201
    second = json.loads(body)
    assert second['cached'] == True
    assert len(second.get('commands', [])) == 2
    
    print("  PASSED")
    return True

def test_stats_with_cache():
    print("Test: Stats endpoint with cache metrics")
    
    conn = http.client.HTTPConnection('localhost', 8080)
    headers = {'Accept': 'application/json'}
    conn.request('GET', '/v1/stats/execution', None, headers)
    response = conn.getresponse()
    body = response.read().decode('utf-8')
    conn.close()
    
    assert response.status == 200
    stats = json.loads(body)
    assert 'cache' in stats
    assert 'hits' in stats['cache']
    assert 'misses' in stats['cache']
    assert 'hit_rate' in stats['cache']
    
    print(f"  Cache stats: {stats['cache']}")
    print("  PASSED")
    return True

def main():
    try:
        test_cache_miss_then_hit()
        test_different_files()
        test_force_bypass()
        test_command_chain_cache()
        test_stats_with_cache()
        print("\n=== All cache tests PASSED ===")
    except Exception as e:
        print(f"\n=== Test FAILED: {e} ===")
        raise

if __name__ == "__main__":
    main()
