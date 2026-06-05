#!/usr/bin/env python3
"""Test script for datagate configuration features."""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

def wait_for_server(port, timeout=15):
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/")
            req.add_header("Referer", "http://example.com/")
            urllib.request.urlopen(req, timeout=2)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                return True
        except:
            time.sleep(0.5)
    return False

def run_server_test(port, env=None, check_func=None):
    """Run a server test with optional environment and check function."""
    env = env or os.environ.copy()

    proc = subprocess.Popen(
        [sys.executable, "datagate.py", "start", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )

    try:
        if not wait_for_server(port):
            stdout, stderr = proc.communicate(timeout=1)
            print(f"Server failed to start. stdout: {stdout.decode()}, stderr: {stderr.decode()}")
            return None, False

        if check_func:
            result = check_func(port)
        else:
            result = True

        return proc, result
    except Exception as e:
        proc.kill()
        proc.wait()
        print(f"Exception: {e}")
        return None, False

def test_basic_convert():
    """Test basic /convert endpoint without config."""
    print("Testing basic /convert...")

    def check(port):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/convert?source=https://example.com/test.csv"
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except urllib.error.HTTPError as e:
            # 404 is expected for invalid URLs
            if e.code in (404, 400):
                return True
        return False

    proc, result = run_server_test(9001, check_func=check)
    if proc:
        proc.kill()
        proc.wait()

    if result:
        print("PASS: Basic convert endpoint works")
    else:
        print("FAIL: Basic convert endpoint failed")
    return result

def test_config_file():
    """Test configuration file loading."""
    print("\nTesting config file...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("MAX_SOURCE_SIZE=500\n")
        f.write("CACHE_ENABLED=false\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file

        proc, result = run_server_test(9002, env=env)
        if proc:
            proc.kill()
            proc.wait()

        if result:
            print("PASS: Server started with config file")
        else:
            print("FAIL: Server failed with config file")
        return result
    finally:
        os.unlink(config_file)

def test_env_override():
    """Test environment variable override of config."""
    print("\nTesting env override...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("CACHE_ENABLED=true\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file
        env["CACHE_ENABLED"] = "false"

        proc, result = run_server_test(9003, env=env)
        if proc:
            proc.kill()
            proc.wait()

        if result:
            print("PASS: Server started with env override")
        else:
            print("FAIL: Server failed with env override")
        return result
    finally:
        os.unlink(config_file)

def test_invalid_config():
    """Test invalid config causes startup failure."""
    print("\nTesting invalid config...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("CACHE_ENABLED=invalid\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file

        proc = subprocess.Popen(
            [sys.executable, "datagate.py", "start", "--port", "9004"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Should fail to start
        time.sleep(3)
        if proc.poll() is not None and proc.poll() != 0:
            print("PASS: Server correctly failed with invalid config")
            return True
        else:
            proc.kill()
            proc.wait()
            print("FAIL: Server should have failed with invalid config")
            return False
    finally:
        os.unlink(config_file)

def test_max_source_size():
    """Test MAX_SOURCE_SIZE enforcement."""
    print("\nTesting MAX_SOURCE_SIZE...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("MAX_SOURCE_SIZE=100\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file

        proc, result = run_server_test(9005, env=env)
        if proc:
            proc.kill()
            proc.wait()

        if result:
            print("PASS: Server started with MAX_SOURCE_SIZE config")
        else:
            print("FAIL: Server failed with MAX_SOURCE_SIZE")
        return result
    finally:
        os.unlink(config_file)

def test_origin_allowlist():
    """Test ORIGIN_ALLOWLIST enforcement."""
    print("\nTesting ORIGIN_ALLOWLIST...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("ORIGIN_ALLOWLIST=example.com,test.org\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file

        def check(port):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/")
                urllib.request.urlopen(req, timeout=2)
                return False  # Should have been rejected
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    data = json.loads(e.read())
                    if "Missing Referer header" in data.get("error", ""):
                        return True
            return False

        proc, result = run_server_test(9006, env=env, check_func=check)
        if proc:
            proc.kill()
            proc.wait()

        if result:
            print("PASS: Origin allowlist working")
        else:
            print("FAIL: Origin allowlist not working")
        return result
    finally:
        os.unlink(config_file)

def test_require_tls():
    """Test REQUIRE_TLS endpoint URL generation."""
    print("\nTesting REQUIRE_TLS...")

    with tempfile.NamedTemporaryFile(mode='w', suffix='.conf', delete=False) as f:
        f.write("REQUIRE_TLS=true\n")
        config_file = f.name

    try:
        env = os.environ.copy()
        env["DATAGATE_CONFIG"] = config_file

        proc, result = run_server_test(9007, env=env)
        if proc:
            proc.kill()
            proc.wait()

        if result:
            print("PASS: Server started with REQUIRE_TLS=true")
        else:
            print("FAIL: Server failed with REQUIRE_TLS")
        return result
    finally:
        os.unlink(config_file)

def test_storage_dir():
    """Test STORAGE_DIR persistence."""
    print("\nTesting STORAGE_DIR...")

    storage_dir = tempfile.mkdtemp(prefix="datagate_test_")

    try:
        env = os.environ.copy()
        env["STORAGE_DIR"] = storage_dir

        proc, result = run_server_test(9008, env=env)
        if proc:
            proc.kill()
            proc.wait()

        if result and os.path.exists(storage_dir):
            print(f"PASS: Storage directory created at {storage_dir}")
        else:
            print("FAIL: Storage directory not created")
        return result and os.path.exists(storage_dir)
    finally:
        import shutil
        shutil.rmtree(storage_dir, ignore_errors=True)

def main():
    """Run all tests."""
    tests = [
        test_basic_convert,
        test_config_file,
        test_env_override,
        test_invalid_config,
        test_max_source_size,
        test_origin_allowlist,
        test_require_tls,
        test_storage_dir,
    ]

    results = []
    for test in tests:
        try:
            results.append(test())
        except Exception as e:
            print(f"FAIL: {test.__name__} raised {e}")
            import traceback
            traceback.print_exc()
            results.append(False)

    print("\n" + "="*50)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")

    return 0 if all(results) else 1

if __name__ == "__main__":
    sys.exit(main())