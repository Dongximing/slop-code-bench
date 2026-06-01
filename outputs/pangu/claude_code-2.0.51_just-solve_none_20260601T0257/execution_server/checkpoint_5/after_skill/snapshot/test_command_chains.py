#!/usr/bin/env python3
"""Test command chains implementation"""
import json
import subprocess
import time
import http.client
import threading

def start_server():
    """Start the execution server in background"""
    proc = subprocess.Popen(
        ['python3', 'execution_server.py', '--port', '18080'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    # Wait for server to start
    time.sleep(1)
    return proc

def post_request(path, data):
    """Make a POST request using http.client"""
    conn = http.client.HTTPConnection('localhost', 18080)
    headers = {'Content-Type': 'application/json'}
    body = json.dumps(data) if data is not None else None
    conn.request('POST', path, body, headers)
    response = conn.getresponse()
    result = response.read().decode('utf-8')
    conn.close()
    return response.status, result

def get_request(path):
    """Make a GET request using http.client"""
    conn = http.client.HTTPConnection('localhost', 18080)
    headers = {'Accept': 'application/json'}
    conn.request('GET', path, None, headers)
    response = conn.getresponse()
    result = response.read().decode('utf-8')
    conn.close()
    return response.status, result

def test_simple_chain():
    """Test 1: Simple chain, stop on first failure"""
    print("Test 1: Simple chain, stop on first failure")
    status, body = post_request('/v1/execute', {
        "command": [
            {"cmd": "echo 'step 1' && exit 0"},
            {"cmd": "echo 'step 2' && exit 1"},
            {"cmd": "echo 'step 3'"}
        ]
    })
    assert status == 201, f"Expected 201, got {status}: {body}"
    data = json.loads(body)
    print(f"  Exit code: {data['exit_code']}")
    print(f"  Commands ran: {len(data.get('commands', []))}")
    assert data['exit_code'] == 1, f"Expected exit_code 1, got {data['exit_code']}"
    assert len(data['commands']) == 2, f"Expected 2 commands ran, got {len(data['commands'])}"
    assert data['commands'][0]['cmd'] == "echo 'step 1' && exit 0"
    assert data['commands'][0]['exit_code'] == 0
    assert data['commands'][1]['cmd'] == "echo 'step 2' && exit 1"
    assert data['commands'][1]['exit_code'] == 1
    print("  PASSED")
    return True

def test_required_command():
    """Test 2: Required command runs despite failure"""
    print("\nTest 2: Required command runs despite failure")
    status, body = post_request('/v1/execute', {
        "command": [
            {"cmd": "sh -c 'echo build > artifact.txt'"},
            {"cmd": "sh -c 'cat nope.txt'"},
            {"cmd": "sh -c 'echo cleanup >> artifact.txt'", "required": True}
        ],
        "track": ["*.txt"]
    })
    assert status == 201, f"Expected 201, got {status}: {body}"
    data = json.loads(body)
    print(f"  Exit code: {data['exit_code']}")
    print(f"  Commands ran: {len(data.get('commands', []))}")
    assert data['exit_code'] == 1, f"Expected exit_code 1, got {data['exit_code']}"
    assert len(data['commands']) == 3, f"Expected 3 commands ran, got {len(data['commands'])}"
    assert data['commands'][2]['required'] == True, "Third command should be required"
    assert 'artifact.txt' in data.get('files', {}), "artifact.txt should be tracked"
    assert 'build' in data['files']['artifact.txt'], "artifact.txt should contain 'build'"
    assert 'cleanup' in data['files']['artifact.txt'], "artifact.txt should contain 'cleanup'"
    print("  PASSED")
    return True

def test_continue_on_error():
    """Test 3: Continue on error"""
    print("\nTest 3: Continue on error")
    status, body = post_request('/v1/execute', {
        "command": [
            {"cmd": "echo 'test 1' && exit 1"},
            {"cmd": "echo 'test 2' && exit 0"},
            {"cmd": "echo 'test 3' && exit 1"}
        ],
        "continue_on_error": True
    })
    assert status == 201, f"Expected 201, got {status}: {body}"
    data = json.loads(body)
    print(f"  Exit code: {data['exit_code']}")
    print(f"  Commands ran: {len(data.get('commands', []))}")
    assert data['exit_code'] == 1, f"Expected exit_code 1, got {data['exit_code']}"
    assert len(data['commands']) == 3, f"Expected 3 commands ran, got {len(data['commands'])}"
    print("  PASSED")
    return True

def test_timeout():
    """Test 4: Per-command timeout with chaining"""
    print("\nTest 4: Per-command timeout with chaining")
    status, body = post_request('/v1/execute', {
        "command": [
            {"cmd": "echo fast"},
            {"cmd": "sleep 5", "timeout": 1},
            {"cmd": "echo 'never runs'", "required": True}
        ],
        "timeout": 10
    })
    assert status == 201, f"Expected 201, got {status}: {body}"
    data = json.loads(body)
    print(f"  Exit code: {data['exit_code']}")
    print(f"  Timed out: {data.get('timed_out')}")
    print(f"  Commands ran: {len(data.get('commands', []))}")
    assert data['exit_code'] == -1, f"Expected exit_code -1, got {data['exit_code']}"
    assert data['timed_out'] == True, f"Expected timed_out True, got {data['timed_out']}"
    assert len(data['commands']) == 3, f"Expected 3 commands ran, got {len(data['commands'])}"
    assert data['commands'][0]['timed_out'] == False
    assert data['commands'][1]['timed_out'] == True, "Second command should have timed out"
    assert data['commands'][1]['exit_code'] == -1, "Second command should have exit_code -1"
    print("  PASSED")
    return True

def test_backward_compatibility():
    """Test 5: Backward compatibility (string command)"""
    print("\nTest 5: Backward compatibility (string command)")
    status, body = post_request('/v1/execute', {
        "command": "echo 'hello world'"
    })
    assert status == 201, f"Expected 201, got {status}: {body}"
    data = json.loads(body)
    print(f"  Keys: {list(data.keys())}")
    assert 'stdout' in data, "Should have stdout field"
    assert 'commands' not in data, "Should NOT have commands field for string command"
    assert data['stdout'] == "hello world\n", f"Expected stdout 'hello world\\n', got {data['stdout']}"
    assert data['exit_code'] == 0
    print("  PASSED")
    return True

def test_stats():
    """Test stats endpoint"""
    print("\nTest 6: Stats endpoint")
    status, body = get_request('/v1/stats/execution')
    assert status == 200, f"Expected 200, got {status}: {body}"
    data = json.loads(body)
    print(f"  Stats: {json.dumps(data, indent=2)}")
    assert 'ran' in data
    assert 'commands' in data
    assert 'duration' in data
    assert 'total' in data['commands']
    assert 'ran' in data['commands']
    assert 'average' in data['commands']
    assert 'average_ran' in data['commands']
    assert 'duration' in data['commands']
    print("  PASSED")
    return True

def main():
    server = start_server()
    try:
        test_simple_chain()
        test_required_command()
        test_continue_on_error()
        test_timeout()
        test_backward_compatibility()
        test_stats()
        print("\n=== All tests PASSED ===")
    except Exception as e:
        print(f"\n=== Test FAILED: {e} ===")
        # Print server output for debugging
        import io
        raise
    finally:
        server.terminate()
        stdout, stderr = server.communicate(timeout=2)
        print("\nServer stdout:", stdout.decode() if stdout else "")
        print("Server stderr:", stderr.decode() if stderr else "")

if __name__ == "__main__":
    main()