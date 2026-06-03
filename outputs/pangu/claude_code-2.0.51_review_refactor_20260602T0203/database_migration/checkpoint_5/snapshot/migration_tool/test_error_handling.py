#!/usr/bin/env python3
"""Test error handling for new data migration operations."""

import subprocess
import json
import os

def run_migration(migration_file, db_file='test_error.db'):
    """Run a migration and return the result."""
    result = subprocess.run(
        ['python', 'migration_tool.py', 'migrate', migration_file, db_file],
        capture_output=True,
        text=True
    )
    return result.returncode, result.stdout, result.stderr

def test_error_missing_table():
    """Test that transform_data fails when table doesn't exist."""
    print("\n" + "="*60)
    print("TEST: transform_data with non-existent table")
    print("="*60)

    migration = {
        "version": 1,
        "description": "Transform on non-existent table",
        "operations": [
            {
                "type": "transform_data",
                "table": "nonexistent",
                "transformations": [
                    {
                        "column": "id",
                        "expression": "1"
                    }
                ]
            }
        ]
    }

    with open('migration_error_1.json', 'w') as f:
        json.dump(migration, f, indent=2)

    code, out, err = run_migration('migration_error_1.json')

    print(f"Return code: {code}")
    print(f"Stderr: {err}")

    if code != 0 and "table" in err.lower():
        print("✓ Error handling test PASSED (correctly rejected non-existent table)")
        result = True
    else:
        print("✗ Error handling test FAILED (expected error for non-existent table)")
        result = False

    os.remove('migration_error_1.json')
    return result

def test_error_missing_column():
    """Test that transform_data fails when column doesn't exist."""
    print("\n" + "="*60)
    print("TEST: transform_data with non-existent column")
    print("="*60)

    migration = {
        "version": 1,
        "description": "Transform non-existent column",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {
                        "name": "id",
                        "type": "INTEGER",
                        "primary_key": True
                    }
                ]
            },
            {
                "type": "transform_data",
                "table": "users",
                "transformations": [
                    {
                        "column": "nonexistent",
                        "expression": "1"
                    }
                ]
            }
        ]
    }

    with open('migration_error_2.json', 'w') as f:
        json.dump(migration, f, indent=2)

    code, out, err = run_migration('migration_error_2.json')

    print(f"Return code: {code}")
    print(f"Stderr: {err}")

    if code != 0 and "column" in err.lower():
        print("✓ Error handling test PASSED (correctly rejected non-existent column)")
        result = True
    else:
        print("✗ Error handling test FAILED (expected error for non-existent column)")
        result = False

    os.remove('migration_error_2.json')
    return result

def test_error_migrate_nonexistent():
    """Test that migrate_column_data fails when from/to column doesn't exist."""
    print("\n" + "="*60)
    print("TEST: migrate_column_data with non-existent columns")
    print("="*60)

    migration = {
        "version": 1,
        "description": "Migrate non-existent columns",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {
                        "name": "id",
                        "type": "INTEGER",
                        "primary_key": True
                    }
                ]
            },
            {
                "type": "migrate_column_data",
                "table": "users",
                "from_column": "old_email",
                "to_column": "new_email"
            }
        ]
    }

    with open('migration_error_3.json', 'w') as f:
        json.dump(migration, f, indent=2)

    code, out, err = run_migration('migration_error_3.json')

    print(f"Return code: {code}")
    print(f"Stderr: {err}")

    if code != 0 and "column" in err.lower():
        print("✓ Error handling test PASSED (correctly rejected non-existent columns)")
        result = True
    else:
        print("✗ Error handling test FAILED (expected error for non-existent columns)")
        result = False

    os.remove('migration_error_3.json')
    return result

def test_error_backfill_nonexistent():
    """Test that backfill_data fails when column doesn't exist."""
    print("\n" + "="*60)
    print("TEST: backfill_data with non-existent column")
    print("="*60)

    migration = {
        "version": 1,
        "description": "Backfill non-existent column",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {
                        "name": "id",
                        "type": "INTEGER",
                        "primary_key": True
                    }
                ]
            },
            {
                "type": "backfill_data",
                "table": "users",
                "column": "status",
                "value": "'active'"
            }
        ]
    }

    with open('migration_error_4.json', 'w') as f:
        json.dump(migration, f, indent=2)

    code, out, err = run_migration('migration_error_4.json')

    print(f"Return code: {code}")
    print(f"Stderr: {err}")

    if code != 0 and "column" in err.lower():
        print("✓ Error handling test PASSED (correctly rejected non-existent column)")
        result = True
    else:
        print("✗ Error handling test FAILED (expected error for non-existent column)")
        result = False

    os.remove('migration_error_4.json')
    return result

def cleanup():
    """Clean up test files."""
    for f in ['test_error.db']:
        if os.path.exists(f):
            os.remove(f)

if __name__ == '__main__':
    print("Database Migration Tool - Error Handling Tests")
    print("="*60)

    # Run error handling tests
    results = []
    results.append(test_error_missing_table())
    results.append(test_error_missing_column())
    results.append(test_error_migrate_nonexistent())
    results.append(test_error_backfill_nonexistent())

    # Summary
    print("\n" + "="*60)
    print("ERROR HANDLING TEST SUMMARY")
    print("="*60)
    tests = [
        "transform_data on non-existent table",
        "transform_data on non-existent column",
        "migrate_column_data with non-existent columns",
        "backfill_data on non-existent column"
    ]
    for i, (test_name, result) in enumerate(zip(tests, results)):
        status = "PASSED" if result else "FAILED"
        print(f"  Test {i+1} ({test_name}): {status}")

    all_passed = all(results)
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")

    # Cleanup
    cleanup()

    exit(0 if all_passed else 1)
