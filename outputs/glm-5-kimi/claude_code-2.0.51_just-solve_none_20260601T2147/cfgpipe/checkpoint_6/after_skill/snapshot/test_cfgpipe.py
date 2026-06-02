#!/usr/bin/env python3
"""Test script for cfgpipe with mock servers."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from flask import Flask, jsonify, request

primary_store_app = Flask(__name__)
primary_store_data = {}
primary_store_events = []
primary_store_cursor = 0

@primary_store_app.route('/v1/primary/kv', methods=['GET'])
def primary_kv():
    key = request.args.get('key')
    if key not in primary_store_data:
        return jsonify({"found": False}), 404
    return jsonify({"found": True, "value": primary_store_data[key]})

@primary_store_app.route('/v1/primary/watch', methods=['GET'])
def primary_watch():
    global primary_store_cursor
    cursor = int(request.args.get('cursor', 0))
    keys = request.args.getlist('key')
    events = [
        {"key": evt['key'], "value": evt['value'], "version": evt['version']}
        for evt in primary_store_events
        if evt['version'] > cursor and evt['key'] in keys
    ]
    return jsonify({"cursor": primary_store_cursor, "events": events})

def run_primary_store():
    primary_store_app.run(port=8500, threaded=True)

secondary_store_app = Flask(__name__)
secondary_store_data = {}

@secondary_store_app.route('/v1/secondary/kv', methods=['GET'])
def secondary_kv():
    key = request.args.get('key')
    if key not in secondary_store_data:
        return jsonify({"found": False})
    return jsonify({"found": True, "value": secondary_store_data[key]})

@secondary_store_app.route('/v1/secondary/batch-read', methods=['POST'])
def secondary_batch_read():
    keys = request.get_json().get('keys', [])
    items = [
        {"key": key, "status": "ok", "value": secondary_store_data[key]}
        if key in secondary_store_data else {"key": key, "status": "missing"}
        for key in keys
    ]
    return jsonify({"items": items})

def run_secondary_store():
    secondary_store_app.run(port=6400, threaded=True)


VENV_PYTHON = '/workspace/venv/bin/python'


def _run_cfgpipe(*args, timeout=5):
    """Run cfgpipe with args, returning (stdout, stderr, returncode)."""
    result = subprocess.run(
        [VENV_PYTHON, '/workspace/cfgpipe.py'] + list(args),
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout, result.stderr, result.returncode


def _write_schema(schema):
    """Write schema to temp file, returning path. Caller must unlink."""
    f = tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False)
    json.dump(schema, f)
    f.close()
    return f.name


def test_non_watch_mode():
    """Test secondary-store seeding in non-watch mode."""
    global secondary_store_data
    secondary_store_data = {"server_host": "10.0.0.5", "server_port": "9090"}

    schema = {
        "server": {
            "host": {"type": "string", "default": "localhost", "secondary-store": "server_host"},
            "port": {"type": "integer", "default": "8080", "secondary-store": "server_port"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe('--secondary-store', 'http://localhost:6400', schema_path)
        output = json.loads(stdout.strip())
        expected = {"server": {"host": "10.0.0.5", "port": 9090}}
        if output == expected:
            print("PASS: Test 6.3 Secondary-Store Seeding")
            return True
        print(f"FAIL: Test 6.3 - Got {output}, expected {expected}")
        print(f"stderr: {stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_watch_mode_primary():
    """Test watch mode seed events with primary store."""
    global primary_store_data, primary_store_events, primary_store_cursor
    primary_store_data = {"app/host": "prod.example.com"}
    primary_store_events = []
    primary_store_cursor = 0

    schema = {
        "server": {
            "host": {"type": "string", "default": "localhost", "primary-store": "app/host"},
            "port": {"type": "integer", "default": "8080"}
        },
        "verbose": {"type": "boolean", "default": "false", "primary-store": "app/verbose"}
    }
    schema_path = _write_schema(schema)
    try:
        proc = subprocess.Popen(
            [VENV_PYTHON, '-u', '/workspace/cfgpipe.py', '--primary-store', 'http://localhost:8500', '--watch', schema_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        lines = []
        start = time.time()
        while time.time() - start < 2:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
                if len(lines) >= 4:
                    break
        proc.terminate()
        proc.wait(timeout=1)

        if len(lines) < 4:
            print(f"FAIL: Test 6.1 - Expected 4 lines, got {len(lines)}")
            print(f"Output: {lines}")
            return False

        events = [json.loads(line) for line in lines[:3]]
        config = json.loads(lines[3])

        for evt in events:
            if not all(k in evt for k in ('path', 'type', 'previous', 'current')):
                print(f"FAIL: Test 6.1 - Event missing required field: {evt}")
                return False
            if evt['previous'] != '':
                print(f"FAIL: Test 6.1 - Seed event should have previous='': {evt}")
                return False

        expected_config = {"server": {"host": "prod.example.com", "port": 8080}, "verbose": False}
        if config != expected_config:
            print(f"FAIL: Test 6.1 - Config mismatch: {config} vs {expected_config}")
            return False

        print("PASS: Test 6.1 Watch Mode Seed Events")
        return True
    finally:
        os.unlink(schema_path)


def test_priority():
    """Test that secondary-store overrides primary-store."""
    global primary_store_data, secondary_store_data
    primary_store_data = {"key1": "primary_value"}
    secondary_store_data = {"key2": "secondary_value"}

    schema = {
        "a": {"type": "string", "default": "default_a", "primary-store": "key1"},
        "b": {"type": "string", "default": "default_b", "secondary-store": "key2"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(
            '--primary-store', 'http://localhost:8500',
            '--secondary-store', 'http://localhost:6400', schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"a": "primary_value", "b": "secondary_value"}
        if output == expected:
            print("PASS: Priority test")
            return True
        print(f"FAIL: Priority test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_arg_override():
    """Test that arg overrides all other sources."""
    global primary_store_data, secondary_store_data
    primary_store_data = {"key1": "primary_value"}
    secondary_store_data = {"key1": "secondary_value"}

    schema = {"a": {"type": "string", "default": "default", "primary-store": "key1", "secondary-store": "key1", "arg": "a"}}
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(
            '--primary-store', 'http://localhost:8500',
            '--secondary-store', 'http://localhost:6400',
            schema_path, '--a=arg_value'
        )
        output = json.loads(stdout.strip())
        if output == {"a": "arg_value"}:
            print("PASS: Arg override test")
            return True
        print(f"FAIL: Arg override test - Got {output}")
        return False
    finally:
        os.unlink(schema_path)


def test_port_type():
    """Test port type parsing and validation."""
    schema = {
        "http_port": {"type": "port", "default": "80"},
        "https_port": {"type": "port", "default": "443"},
        "custom_port": {"type": "port", "default": "8080"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {"http_port": "80", "https_port": "443", "custom_port": "8080"}
        if output == expected:
            print("PASS: Port type test")
            return True
        print(f"FAIL: Port type test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_float_formatting():
    """Test float formatting with 6 decimal places."""
    schema = {"ratio": {"type": "float", "default": "0.5"}, "price": {"type": "float", "default": "19.99"}}
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {"ratio": 0.500000, "price": 19.990000}
        if output == expected:
            print("PASS: Float formatting test")
            return True
        print(f"FAIL: Float formatting test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_error_cases():
    """Test various error conditions."""
    schema = {"a": {"type": "string", "secondary-store": "key1"}}
    schema_path = _write_schema(schema)
    try:
        _, stderr, rc = _run_cfgpipe(schema_path)
        if rc != 0 and 'secondary-store' in stderr:
            print("PASS: Missing secondary-store URL error")
        else:
            print(f"FAIL: Missing secondary-store URL error - got code {rc}, stderr: {stderr}")
            return False
    finally:
        os.unlink(schema_path)

    schema = {
        "a": {"type": "string", "secondary-store": "dup_key", "default": "x"},
        "b": {"type": "string", "secondary-store": "dup_key", "default": "y"}
    }
    schema_path = _write_schema(schema)
    try:
        _, stderr, rc = _run_cfgpipe('--secondary-store', 'http://localhost:6400', schema_path)
        if rc != 0 and 'duplicate secondary-store key' in stderr:
            print("PASS: Duplicate secondary-store key error")
        else:
            print(f"FAIL: Duplicate secondary-store key error - got code {rc}, stderr: {stderr}")
            return False
    finally:
        os.unlink(schema_path)

    return True


def test_duration_type():
    """Test duration type parsing and normalization."""
    schema = {
        "timeout": {"type": "duration", "default": "90s"},
        "long_wait": {"type": "duration", "default": "2h1m30s"},
        "quick": {"type": "duration", "default": "5s"},
        "zero": {"type": "duration", "default": "0s"},
        "big": {"type": "duration", "default": "1h90m"},
        "odd": {"type": "duration", "default": "3661s"},
        "drop_zero": {"type": "duration", "default": "2h0m30s"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {
            "timeout": "1m30s",
            "long_wait": "2h1m30s",
            "quick": "5s",
            "zero": "0s",
            "big": "2h30m",
            "odd": "1h1m1s",
            "drop_zero": "2h30s"
        }
        if output == expected:
            print("PASS: Duration type test")
            return True
        print(f"FAIL: Duration type test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_duration_parse_failures():
    """Test duration type parse failures."""
    test_cases = [
        ("5", "bare number without unit"),
        ("", "empty string"),
        ("5x", "unknown unit"),
        ("1h-2m", "malformed concatenation"),
        ("1h2", "trailing number without unit"),
    ]

    for default_val, desc in test_cases:
        schema = {"t": {"type": "duration", "default": default_val}}
        schema_path = _write_schema(schema)
        try:
            _, stderr, rc = _run_cfgpipe(schema_path)
            if rc != 0:
                print(f"PASS: Duration {desc} rejected")
            else:
                print(f"FAIL: Duration {desc} should be rejected")
                return False
        finally:
            os.unlink(schema_path)

    return True


def test_pattern_type():
    """Test pattern type with valid regex."""
    schema = {
        "filter": {"type": "pattern", "default": "^https://.*\\.example\\.com$"},
        "simple": {"type": "pattern", "default": "abc.*"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {
            "filter": "^https://.*\\.example\\.com$",
            "simple": "abc.*"
        }
        if output == expected:
            print("PASS: Pattern type test")
            return True
        print(f"FAIL: Pattern type test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_pattern_parse_failure():
    """Test pattern type rejects invalid regex."""
    schema = {"filter": {"type": "pattern", "default": "[unclosed"}}
    schema_path = _write_schema(schema)
    try:
        _, stderr, rc = _run_cfgpipe(schema_path)
        if rc != 0 and "unclosed" in stderr:
            print("PASS: Pattern invalid regex rejected")
            return True
        print(f"FAIL: Pattern invalid regex should be rejected - got rc={rc}, stderr={stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_map_type():
    """Test map type parsing and ordering."""
    schema = {
        "labels": {"type": "map", "default": "region:us-east,env:prod"},
        "empty_map": {"type": "map", "default": ""}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {
            "labels": {"region": "us-east", "env": "prod"},
            "empty_map": {}
        }
        if output == expected:
            print("PASS: Map type test")
            return True
        print(f"FAIL: Map type test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_map_parse_failure():
    """Test map type rejects entries without colon."""
    schema = {"tags": {"type": "map", "default": "badentry"}}
    schema_path = _write_schema(schema)
    try:
        _, stderr, rc = _run_cfgpipe(schema_path)
        if rc != 0:
            print("PASS: Map no-colon entry rejected")
            return True
        print(f"FAIL: Map no-colon entry should be rejected")
        return False
    finally:
        os.unlink(schema_path)


def test_list_type():
    """Test list type parsing."""
    schema = {
        "features": {"type": "list", "default": "auth,logging,metrics"},
        "empty_list": {"type": "list", "default": ""}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        expected = {
            "features": ["auth", "logging", "metrics"],
            "empty_list": []
        }
        if output == expected:
            print("PASS: List type test")
            return True
        print(f"FAIL: List type test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_redacted_type():
    """Test redacted type masks values in output."""
    schema = {
        "api_key": {"type": "redacted", "default": "sk-secret-12345"},
        "db_password": {"type": "redacted", "env": "DB_PASSWORD"}
    }
    schema_path = _write_schema(schema)
    try:
        env = os.environ.copy()
        env["DB_PASSWORD"] = "super-secret-password"
        result = subprocess.run(
            [VENV_PYTHON, '/workspace/cfgpipe.py', schema_path],
            capture_output=True, text=True, timeout=5, env=env
        )
        output = json.loads(result.stdout.strip())
        if output.get("api_key") == "<masked>" and output.get("db_password") == "<masked>":
            print("PASS: Redacted type test")
            return True
        print(f"FAIL: Redacted type test - Got {output}")
        return False
    finally:
        os.unlink(schema_path)


def test_redacted_change_events():
    """Test redacted type uses masked values in change events."""
    global primary_store_data, primary_store_events, primary_store_cursor
    primary_store_data = {"secret_key": "initial-secret"}
    primary_store_events = []
    primary_store_cursor = 0

    schema = {
        "db_password": {"type": "redacted", "primary-store": "secret_key"}
    }
    schema_path = _write_schema(schema)
    try:
        proc = subprocess.Popen(
            [VENV_PYTHON, '-u', '/workspace/cfgpipe.py',
             '--primary-store', 'http://localhost:8500', '--watch', schema_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        lines = []
        start = time.time()
        while time.time() - start < 2:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
                if len(lines) >= 2:
                    break
        proc.terminate()
        proc.wait(timeout=1)

        if len(lines) < 2:
            print(f"FAIL: Redacted change events - Expected 2 lines, got {len(lines)}")
            return False

        seed_event = json.loads(lines[0])
        if seed_event["previous"] != "":
            print(f"FAIL: Redacted seed event previous should be empty string: {seed_event}")
            return False
        if seed_event["current"] != "<masked>":
            print(f"FAIL: Redacted seed event current should be masked: {seed_event}")
            return False

        config = json.loads(lines[1])
        if config.get("db_password") != "<masked>":
            print(f"FAIL: Redacted config should be masked: {config}")
            return False

        print("PASS: Redacted change events test")
        return True
    finally:
        os.unlink(schema_path)


def test_combined_types():
    """Test combined types from the spec example."""
    global secondary_store_data
    secondary_store_data = {}

    schema = {
        "server": {
            "timeout": {"type": "duration", "default": "90s", "env": "SERVER_TIMEOUT"},
            "allowed_origins": {"type": "pattern", "default": "^https://.*\\.example\\.com$"}
        },
        "labels": {"type": "map", "default": "region:us-east,env:prod"},
        "features": {"type": "list", "default": "auth,logging,metrics"},
        "api_key": {"type": "redacted", "env": "API_KEY"}
    }
    schema_path = _write_schema(schema)
    try:
        env = os.environ.copy()
        env["SERVER_TIMEOUT"] = "2h1m30s"
        env["API_KEY"] = "sk-secret-12345"
        result = subprocess.run(
            [VENV_PYTHON, '/workspace/cfgpipe.py', schema_path],
            capture_output=True, text=True, timeout=5, env=env
        )
        output = json.loads(result.stdout.strip())
        expected = {
            "server": {
                "timeout": "2h1m30s",
                "allowed_origins": "^https://.*\\.example\\.com$"
            },
            "labels": {"region": "us-east", "env": "prod"},
            "features": ["auth", "logging", "metrics"],
            "api_key": "<masked>"
        }
        if output == expected:
            print("PASS: Combined types test")
            return True
        print(f"FAIL: Combined types test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_integer_json_native():
    """Test that integer outputs as JSON number."""
    schema = {"count": {"type": "integer", "default": "42"}}
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        if output == {"count": 42} and isinstance(output["count"], int):
            print("PASS: Integer JSON native test")
            return True
        print(f"FAIL: Integer JSON native test - Got {output}")
        return False
    finally:
        os.unlink(schema_path)


def test_boolean_json_native():
    """Test that boolean outputs as JSON literal."""
    schema = {"flag": {"type": "boolean", "default": "true"}}
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        output = json.loads(stdout.strip())
        if output == {"flag": True} and isinstance(output["flag"], bool):
            print("PASS: Boolean JSON native test")
            return True
        print(f"FAIL: Boolean JSON native test - Got {output}")
        return False
    finally:
        os.unlink(schema_path)


def test_float_json_native():
    """Test that float outputs as JSON number with 6 decimals."""
    schema = {"ratio": {"type": "float", "default": "0.5"}}
    schema_path = _write_schema(schema)
    try:
        stdout, _, _ = _run_cfgpipe(schema_path)
        raw = stdout.strip()
        if '"ratio": 0.500000' in raw:
            output = json.loads(raw)
            if abs(output["ratio"] - 0.5) < 1e-10:
                print("PASS: Float JSON native test")
                return True
        print(f"FAIL: Float JSON native test - Got raw: {raw}")
        return False
    finally:
        os.unlink(schema_path)


def test_change_events_use_string_repr():
    """Test that change events use string representations."""
    global primary_store_data, primary_store_events, primary_store_cursor
    primary_store_data = {}
    primary_store_events = []
    primary_store_cursor = 0

    schema = {
        "labels": {"type": "map", "default": "b:2,a:1", "primary-store": "labels_key"},
        "features": {"type": "list", "default": "x,y", "primary-store": "features_key"}
    }
    schema_path = _write_schema(schema)
    try:
        proc = subprocess.Popen(
            [VENV_PYTHON, '-u', '/workspace/cfgpipe.py',
             '--primary-store', 'http://localhost:8500', '--watch', schema_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        lines = []
        start = time.time()
        while time.time() - start < 2:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
                if len(lines) >= 3:
                    break
        proc.terminate()
        proc.wait(timeout=1)

        if len(lines) < 3:
            print(f"FAIL: Change events string repr - Expected 3 lines, got {len(lines)}")
            return False

        events = [json.loads(lines[0]), json.loads(lines[1])]

        map_evt = [e for e in events if e["type"] == "map"][0]
        if map_evt["current"] != "a:1,b:2":
            print(f"FAIL: Map change event should use sorted string repr: {map_evt}")
            return False

        list_evt = [e for e in events if e["type"] == "list"][0]
        if list_evt["current"] != "x,y":
            print(f"FAIL: List change event should use insertion order: {list_evt}")
            return False

        print("PASS: Change events string repr test")
        return True
    finally:
        os.unlink(schema_path)


def test_primary_store_prefix():
    """Test primary-store folder prefix composition."""
    global primary_store_data
    primary_store_data = {
        "staging/port": "8080",
        "staging/host": "api.example.com"
    }

    schema = {
        "server": {
            "port": {"type": "integer", "primary-store": "port"},
            "host": {"type": "string", "primary-store": "host"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--primary-store', 'http://localhost:8500',
            '--primary-store-prefix', 'staging',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"server": {"port": 8080, "host": "api.example.com"}}
        if output == expected:
            print("PASS: Primary-store prefix test")
            return True
        print(f"FAIL: Primary-store prefix test - Got {output}, expected {expected}")
        print(f"stderr: {stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_primary_store_prefix_with_slashes():
    """Test primary-store prefix with leading/trailing slashes."""
    global primary_store_data
    primary_store_data = {
        "production/db/host": "db.prod.example.com"
    }

    schema = {
        "db": {
            "host": {"type": "string", "primary-store": "db/host"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--primary-store', 'http://localhost:8500',
            '--primary-store-prefix', '/production/',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"db": {"host": "db.prod.example.com"}}
        if output == expected:
            print("PASS: Primary-store prefix with slashes test")
            return True
        print(f"FAIL: Primary-store prefix with slashes test - Got {output}, expected {expected}")
        print(f"stderr: {stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_primary_store_empty_prefix():
    """Test primary-store with empty prefix uses key as-is."""
    global primary_store_data
    primary_store_data = {
        "mykey": "myvalue"
    }

    schema = {
        "a": {"type": "string", "primary-store": "mykey"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--primary-store', 'http://localhost:8500',
            '--primary-store-prefix', '',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"a": "myvalue"}
        if output == expected:
            print("PASS: Primary-store empty prefix test")
            return True
        print(f"FAIL: Primary-store empty prefix test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_secondary_store_key_prefix():
    """Test secondary-store dynamic key prefixing."""
    global secondary_store_data
    secondary_store_data = {
        "myapp:ttl": "300",
        "myapp:max_size": "1024"
    }

    schema = {
        "cache": {
            "ttl": {"type": "integer", "secondary-store": "ttl"},
            "max_size": {"type": "integer", "secondary-store": "max_size"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--secondary-store', 'http://localhost:6400',
            '--secondary-store-key-prefix', 'myapp',
            '--secondary-store-key-separator', ':',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"cache": {"ttl": 300, "max_size": 1024}}
        if output == expected:
            print("PASS: Secondary-store key prefix test")
            return True
        print(f"FAIL: Secondary-store key prefix test - Got {output}, expected {expected}")
        print(f"stderr: {stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_secondary_store_key_prefix_dot_separator():
    """Test secondary-store prefix with dot separator."""
    global secondary_store_data
    secondary_store_data = {
        "myapp.config.timeout": "30"
    }

    schema = {
        "config": {
            "timeout": {"type": "integer", "secondary-store": "config.timeout"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--secondary-store', 'http://localhost:6400',
            '--secondary-store-key-prefix', 'myapp',
            '--secondary-store-key-separator', '.',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"config": {"timeout": 30}}
        if output == expected:
            print("PASS: Secondary-store dot separator test")
            return True
        print(f"FAIL: Secondary-store dot separator test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_secondary_store_empty_separator():
    """Test secondary-store with empty separator (direct concatenation)."""
    global secondary_store_data
    secondary_store_data = {
        "myappkey": "value123"
    }

    schema = {
        "a": {"type": "string", "secondary-store": "key"}
    }
    schema_path = _write_schema(schema)
    try:
        stdout, stderr, rc = _run_cfgpipe(
            '--secondary-store', 'http://localhost:6400',
            '--secondary-store-key-prefix', 'myapp',
            '--secondary-store-key-separator', '',
            schema_path
        )
        output = json.loads(stdout.strip())
        expected = {"a": "value123"}
        if output == expected:
            print("PASS: Secondary-store empty separator test")
            return True
        print(f"FAIL: Secondary-store empty separator test - Got {output}, expected {expected}")
        return False
    finally:
        os.unlink(schema_path)


def test_secondary_store_prefix_requires_separator():
    """Test that --secondary-store-key-prefix requires --secondary-store-key-separator."""
    schema = {"a": {"type": "string", "default": "x"}}
    schema_path = _write_schema(schema)
    try:
        _, stderr, rc = _run_cfgpipe(
            '--secondary-store-key-prefix', 'myapp',
            schema_path
        )
        if rc != 0 and 'requires --secondary-store-key-separator' in stderr:
            print("PASS: Secondary prefix requires separator test")
            return True
        print(f"FAIL: Secondary prefix requires separator test - got rc={rc}, stderr={stderr}")
        return False
    finally:
        os.unlink(schema_path)


def test_primary_store_prefix_watch():
    """Test primary-store prefix in watch mode maps runtime key back to parameter."""
    global primary_store_data, primary_store_events, primary_store_cursor
    primary_store_data = {"staging/port": "8080"}
    primary_store_events = []
    primary_store_cursor = 0

    schema = {
        "server": {
            "port": {"type": "integer", "primary-store": "port"}
        }
    }
    schema_path = _write_schema(schema)
    try:
        proc = subprocess.Popen(
            [VENV_PYTHON, '-u', '/workspace/cfgpipe.py',
             '--primary-store', 'http://localhost:8500',
             '--primary-store-prefix', 'staging',
             '--watch', schema_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        lines = []
        start = time.time()
        while time.time() - start < 2:
            line = proc.stdout.readline()
            if line:
                lines.append(line.strip())
                if len(lines) >= 2:
                    break
        proc.terminate()
        proc.wait(timeout=1)

        if len(lines) < 2:
            print(f"FAIL: Primary-store prefix watch - Expected 2 lines, got {len(lines)}")
            return False

        event = json.loads(lines[0])
        # The path should be the schema-declared parameter path, not the composed store key
        if event["path"] != "server.port":
            print(f"FAIL: Primary-store prefix watch - Expected path 'server.port', got '{event['path']}'")
            return False

        config = json.loads(lines[1])
        if config != {"server": {"port": 8080}}:
            print(f"FAIL: Primary-store prefix watch - Config mismatch: {config}")
            return False

        print("PASS: Primary-store prefix watch test")
        return True
    finally:
        os.unlink(schema_path)


def main():
    primary_thread = threading.Thread(target=run_primary_store, daemon=True)
    secondary_thread = threading.Thread(target=run_secondary_store, daemon=True)
    primary_thread.start()
    secondary_thread.start()
    time.sleep(1)

    tests = [
        ("Non-watch mode", test_non_watch_mode),
        ("Watch mode primary", test_watch_mode_primary),
        ("Priority", test_priority),
        ("Arg override", test_arg_override),
        ("Port type", test_port_type),
        ("Float formatting", test_float_formatting),
        ("Error cases", test_error_cases),
        ("Duration type", test_duration_type),
        ("Duration parse failures", test_duration_parse_failures),
        ("Pattern type", test_pattern_type),
        ("Pattern parse failure", test_pattern_parse_failure),
        ("Map type", test_map_type),
        ("Map parse failure", test_map_parse_failure),
        ("List type", test_list_type),
        ("Redacted type", test_redacted_type),
        ("Redacted change events", test_redacted_change_events),
        ("Combined types", test_combined_types),
        ("Integer JSON native", test_integer_json_native),
        ("Boolean JSON native", test_boolean_json_native),
        ("Float JSON native", test_float_json_native),
        ("Change events string repr", test_change_events_use_string_repr),
        ("Primary-store prefix", test_primary_store_prefix),
        ("Primary-store prefix with slashes", test_primary_store_prefix_with_slashes),
        ("Primary-store empty prefix", test_primary_store_empty_prefix),
        ("Secondary-store key prefix", test_secondary_store_key_prefix),
        ("Secondary-store dot separator", test_secondary_store_key_prefix_dot_separator),
        ("Secondary-store empty separator", test_secondary_store_empty_separator),
        ("Secondary-store prefix requires separator", test_secondary_store_prefix_requires_separator),
        ("Primary-store prefix watch", test_primary_store_prefix_watch),
    ]

    passed, failed = 0, 0
    for name, test_fn in tests:
        try:
            if test_fn():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"FAIL: {name} - Exception: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
