#!/usr/bin/env python3
"""Test the nested types implementation."""

import json
import os
import subprocess
import tempfile


def test_basic_nested():
    """Test basic nested types with JSONL input."""
    # Create test data
    test_data = [
        {"id": 1, "user": {"name": "Alice", "age": 30}, "items": [{"sku": "A123", "qty": 2}]},
        {"id": 2, "user": {"name": "Bob", "age": 25}, "items": [{"sku": "B456", "qty": 1}, {"sku": "C789", "qty": 3}]},
        {"id": 3, "user": {"name": "Charlie", "age": 35, "prefs": {"theme": "dark", "lang": "en"}}, "items": []},
    ]

    # Create schema
    schema = {
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "user", "type": {"struct": {"fields": [
                {"name": "name", "type": "string"},
                {"name": "age", "type": "int"},
                {"name": "prefs", "type": {"map": {"key": "string", "value": "string"}}}
            ]}}},
            {"name": "items", "type": {"array": {"element": {"struct": {"fields": [
                {"name": "sku", "type": "string"},
                {"name": "qty", "type": "int"}
            ]}}}}},
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        # Write test JSONL
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            for row in test_data:
                f.write(json.dumps(row) + '\n')

        # Write schema
        schema_path = os.path.join(tmpdir, "schema.json")
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        # Run merge
        output_path = os.path.join(tmpdir, "output.csv")
        cmd = [
            "python", "merge_files.py",
            "--output", output_path,
            "--key", "id",
            "--schema", schema_path,
            jsonl_path
        ]

        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")
            return False

        # Read output
        with open(output_path, 'r') as f:
            lines = f.readlines()

        print(f"Output ({len(lines)} lines):")
        for i, line in enumerate(lines[:5]):  # Print first 5 lines
            print(f"  {i}: {line.strip()}")

        # Verify output
        if len(lines) == 4:  # header + 3 rows
            print("✓ Basic nested test PASSED")
            return True
        else:
            print("✗ Basic nested test FAILED")
            return False


def test_nested_key():
    """Test sorting by nested key path."""
    test_data = [
        {"id": 1, "user": {"name": "Charlie", "age": 30}},
        {"id": 2, "user": {"name": "Alice", "age": 25}},
        {"id": 3, "user": {"name": "Bob", "age": 35}},
    ]

    schema = {
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "user", "type": {"struct": {"fields": [
                {"name": "name", "type": "string"},
                {"name": "age", "type": "int"}
            ]}}},
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            for row in test_data:
                f.write(json.dumps(row) + '\n')

        schema_path = os.path.join(tmpdir, "schema.json")
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        output_path = os.path.join(tmpdir, "output.csv")
        cmd = [
            "python", "merge_files.py",
            "--output", output_path,
            "--key", "user.name",  # Sort by nested field
            "--schema", schema_path,
            jsonl_path
        ]

        print(f"\nRunning: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            return False

        # Read output - should be sorted by user.name (Alice, Bob, Charlie)
        with open(output_path, 'r') as f:
            lines = f.readlines()

        # Check order: id 2 (Alice), 3 (Bob), 1 (Charlie)
        if "Alice" in lines[2] and "Bob" in lines[3] and "Charlie" in lines[4]:
            print("✓ Nested key sorting test PASSED")
            return True
        else:
            print(f"Rows not in expected order. Lines:")
            for i, line in enumerate(lines):
                print(f"  {i}: {line.strip()}")
            return False


def test_error_nested_without_schema():
    """Test that nested inputs without schema trigger error 6."""
    test_data = [
        {"id": 1, "user": {"name": "Alice", "age": 30}},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            f.write(json.dumps(test_data[0]) + '\n')

        output_path = os.path.join(tmpdir, "output.csv")
        cmd = [
            "python", "merge_files.py",
            "--output", output_path,
            "--key", "id",
            jsonl_path
        ]

        print(f"\nRunning: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Should fail with exit code 6 for error 6
        if result.returncode == 6 and "ERR 6" in result.stderr:
            print("✓ Error 6 for nested without schema test PASSED")
            return True
        else:
            print(f"Expected exit code 6, got {result.returncode}")
            print(f"STDERR: {result.stderr}")
            return False


def test_type_aliases():
    """Test type aliases."""
    schema = {
        "columns": [
            {"name": "id", "type": "integer"},  # alias for int
            {"name": "price", "type": "double"},  # alias for float
            {"name": "active", "type": "boolean"},  # alias for bool
            {"name": "created", "type": "datetime"},  # alias for timestamp
        ]
    }

    test_data = [
        {"id": 1, "price": 99.99, "active": True, "created": "2024-01-15T10:30:00Z"},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            for row in test_data:
                f.write(json.dumps(row) + '\n')

        schema_path = os.path.join(tmpdir, "schema.json")
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        output_path = os.path.join(tmpdir, "output.csv")
        cmd = [
            "python", "merge_files.py",
            "--output", output_path,
            "--key", "id",
            "--schema", schema_path,
            jsonl_path
        ]

        print(f"\nRunning: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            return False

        with open(output_path, 'r') as f:
            lines = f.readlines()

        print(f"Output: {lines[1].strip()}")
        print("✓ Type aliases test PASSED")
        return True


def test_json_type():
    """Test json type accepts arbitrary JSON."""
    schema = {
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "data", "type": "json"},
        ]
    }

    test_data = [
        {"id": 1, "data": {"anything": "goes", "here": [1, 2, 3]}},
        {"id": 2, "data": "just a string"},
        {"id": 3, "data": ["array", "of", "things"]},
    ]

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            for row in test_data:
                f.write(json.dumps(row) + '\n')

        schema_path = os.path.join(tmpdir, "schema.json")
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        output_path = os.path.join(tmpdir, "output.csv")
        cmd = [
            "python", "merge_files.py",
            "--output", output_path,
            "--key", "id",
            "--schema", schema_path,
            jsonl_path
        ]

        print(f"\nRunning: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            return False

        with open(output_path, 'r') as f:
            lines = f.readlines()

        print(f"Output lines:")
        for i, line in enumerate(lines[1:4], 1):
            print(f"  {i}: {line.strip()[:100]}...")
        print("✓ JSON type test PASSED")
        return True


def test_partition_by_nested():
    """Test partition by with nested field paths."""
    test_data = [
        {"id": 1, "region": "us", "user": {"name": "Alice", "country": "USA"}},
        {"id": 2, "region": "eu", "user": {"name": "Bob", "country": "UK"}},
        {"id": 3, "region": "us", "user": {"name": "Charlie", "country": "Canada"}},
    ]

    schema = {
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "region", "type": "string"},
            {"name": "user", "type": {"struct": {"fields": [
                {"name": "name", "type": "string"},
                {"name": "country", "type": "string"}
            ]}}},
        ]
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "test.jsonl")
        with open(jsonl_path, 'w') as f:
            for row in test_data:
                f.write(json.dumps(row) + '\n')

        schema_path = os.path.join(tmpdir, "schema.json")
        with open(schema_path, 'w') as f:
            json.dump(schema, f)

        output_dir = os.path.join(tmpdir, "output")
        cmd = [
            "python", "merge_files.py",
            "--output", output_dir,
            "--key", "id",
            "--partition-by", "region,user.country",  # Partition by nested path
            "--schema", schema_path,
            jsonl_path
        ]

        print(f"\nRunning: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"STDERR: {result.stderr}")
            return False

        # Check partitioned directories exist
        import glob
        partitions = glob.glob(os.path.join(output_dir, "*"))
        print(f"Partitions: {[os.path.basename(p) for p in partitions]}")

        if len(partitions) >= 2:  # At least us and eu
            print("✓ Partition by nested test PASSED")
            return True
        else:
            print("✗ Expected at least 2 partitions")
            return False


if __name__ == "__main__":
    tests = [
        test_basic_nested,
        test_nested_key,
        test_error_nested_without_schema,
        test_type_aliases,
        test_json_type,
        test_partition_by_nested,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"✗ Test {test.__name__} EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed")
