#!/usr/bin/env python3
"""Comprehensive test for data migrations."""

import sqlite3
import os
import subprocess
import json

def run_migration(migration_file, db_file='test.db'):
    """Run a migration and return the result."""
    result = subprocess.run(
        ['python', 'migration_tool.py', 'migrate', migration_file, db_file],
        capture_output=True,
        text=True
    )
    return result.returncode, result.stdout, result.stderr

def setup_database():
    """Create the database with version 1 and insert test data."""
    # Run version 1 migration
    code, out, err = run_migration('migration_v1.json')
    if code != 0:
        print(f"Failed to apply v1 migration: {err}")
        return False
    print("✓ Version 1 applied (create table)")

    # Insert test data
    conn = sqlite3.connect('test.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('John', 'Doe', 'john@example.com')")
    cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('Jane', 'Smith', 'jane@example.com')")
    cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('Bob', NULL, 'bob@example.com')")
    conn.commit()

    print("\nInitial data:")
    cursor.execute("SELECT * FROM users")
    for row in cursor.fetchall():
        print(f"  {row}")
    conn.close()
    return True

def test_backfill():
    """Test backfill_data operation."""
    print("\n" + "="*60)
    print("TEST 1: backfill_data")
    print("="*60)

    # Apply version 3 migration
    code, out, err = run_migration('migration_v3.json')
    if code != 0:
        print(f"✗ Failed: {err}")
        return False

    print(f"Migration output:\n{out}")

    # Verify results
    conn = sqlite3.connect('test.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, first_name, last_name, status FROM users")
    rows = cursor.fetchall()
    print("\nAfter backfilling status='active':")
    for row in rows:
        print(f"  {row}")
    conn.close()

    # Check all rows have status='active'
    all_active = all(row[3] == 'active' for row in rows)
    if all_active:
        print("\n✓ backfill_data test PASSED")
        return True
    else:
        print("\n✗ backfill_data test FAILED")
        return False

def test_transform_data():
    """Test transform_data operation."""
    print("\n" + "="*60)
    print("TEST 2: transform_data")
    print("="*60)

    # First, create migration v4 that adds full_name and transforms it
    migration_v4 = {
        "version": 4,
        "description": "Compute full_name from first and last",
        "operations": [
            {
                "type": "add_column",
                "table": "users",
                "column": {
                    "name": "full_name",
                    "type": "TEXT",
                    "not_null": False
                }
            },
            {
                "type": "transform_data",
                "table": "users",
                "transformations": [
                    {
                        "column": "full_name",
                        "expression": "first_name || ' ' || last_name"
                    }
                ]
            }
        ]
    }

    with open('migration_v4.json', 'w') as f:
        json.dump(migration_v4, f, indent=2)

    code, out, err = run_migration('migration_v4.json')
    if code != 0:
        print(f"✗ Failed: {err}")
        return False

    print(f"Migration output:\n{out}")

    # Verify results
    conn = sqlite3.connect('test.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, first_name, last_name, full_name FROM users")
    rows = cursor.fetchall()
    print("\nAfter transforming full_name:")
    for row in rows:
        print(f"  {row}")
    conn.close()

    # Check full_name values
    # Note: In SQLite, expression || NULL results in NULL
    expected = {
        1: 'John Doe',
        2: 'Jane Smith',
        3: None  # Bob has NULL last_name, so 'Bob ' || NULL = NULL
    }

    success = True
    for row in rows:
        id_val = row[0]
        full_name = row[3]
        exp = expected.get(id_val)
        if full_name != exp:
            print(f"  Expected {id_val} to have full_name='{exp}', got '{full_name}'")
            success = False

    if success:
        print("\n✓ transform_data test PASSED")
    else:
        print("\n✗ transform_data test FAILED")

    os.remove('migration_v4.json')
    return success

def test_migrate_column_data():
    """Test migrate_column_data operation."""
    print("\n" + "="*60)
    print("TEST 3: migrate_column_data")
    print("="*60)

    # Create migration v5 that migrates email to email_address
    migration_v5 = {
        "version": 5,
        "description": "Migrate email to email_address",
        "operations": [
            {
                "type": "add_column",
                "table": "users",
                "column": {
                    "name": "email_address",
                    "type": "TEXT",
                    "not_null": False
                }
            },
            {
                "type": "migrate_column_data",
                "table": "users",
                "from_column": "email",
                "to_column": "email_address"
            }
        ]
    }

    with open('migration_v5.json', 'w') as f:
        json.dump(migration_v5, f, indent=2)

    code, out, err = run_migration('migration_v5.json')
    if code != 0:
        print(f"✗ Failed: {err}")
        return False

    print(f"Migration output:\n{out}")

    # Verify results
    conn = sqlite3.connect('test.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, email_address FROM users")
    rows = cursor.fetchall()
    print("\nAfter migrating email to email_address:")
    for row in rows:
        print(f"  id={row[0]}, email='{row[1]}', email_address='{row[2]}'")
    conn.close()

    # Check both columns have same values
    success = True
    for row in rows:
        if row[1] != row[2]:
            print(f"  Email and email_address differ for user {row[0]}")
            success = False

    if success:
        print("\n✓ migrate_column_data test PASSED")
    else:
        print("\n✗ migrate_column_data test FAILED")

    os.remove('migration_v5.json')
    return success

def test_complete_example():
    """Test the complete example from the spec."""
    print("\n" + "="*60)
    print("TEST 4: Complete column migration example")
    print("="*60)

    # Create a new database for this test
    if os.path.exists('test_complete.db'):
        os.remove('test_complete.db')

    # Version 1
    code, out, err = run_migration('migration_v1.json', 'test_complete.db')
    # Insert test data
    conn = sqlite3.connect('test_complete.db')
    cursor = conn.cursor()
    cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('Test', 'User', 'test@example.com')")
    conn.commit()

    # The complete example from the spec
    migration_complete = {
        "version": 2,
        "description": "Complete email migration example",
        "operations": [
            {
                "type": "add_column",
                "table": "users",
                "column": {
                    "name": "email_address",
                    "type": "TEXT",
                    "not_null": False
                }
            },
            {
                "type": "migrate_column_data",
                "table": "users",
                "from_column": "email",
                "to_column": "email_address"
            },
            {
                "type": "drop_column",
                "table": "users",
                "column": "email"
            }
        ]
    }

    with open('migration_complete.json', 'w') as f:
        json.dump(migration_complete, f, indent=2)

    code, out, err = run_migration('migration_complete.json', 'test_complete.db')
    if code != 0:
        print(f"✗ Failed: {err}")
        conn.close()
        return False

    print(f"Migration output:\n{out}")

    # Verify results - email column should be gone, email_address should have data
    cursor.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"\nColumns after migration: {columns}")

    cursor.execute("SELECT * FROM users")
    row = cursor.fetchone()
    print(f"Data: {row}")
    conn.close()

    success = 'email_address' in columns and 'email' not in columns
    if success:
        print("\n✓ Complete example test PASSED")
    else:
        print("\n✗ Complete example test FAILED")

    os.remove('migration_complete.json')
    os.remove('test_complete.db')
    return success

def cleanup():
    """Clean up test files."""
    for f in ['test.db', 'test_complete.db']:
        if os.path.exists(f):
            os.remove(f)

if __name__ == '__main__':
    print("Database Migration Tool - Data Migrations Test Suite")
    print("="*60)

    # Clean up any previous test
    cleanup()

    # Setup
    if not setup_database():
        print("Failed to setup database. Exiting.")
        exit(1)

    # Run all tests
    results = []
    results.append(test_backfill())
    results.append(test_transform_data())
    results.append(test_migrate_column_data())
    results.append(test_complete_example())

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    tests = ["backfill_data", "transform_data", "migrate_column_data", "complete_example"]
    for i, (test_name, result) in enumerate(zip(tests, results)):
        status = "PASSED" if result else "FAILED"
        print(f"  Test {i+1} ({test_name}): {status}")

    all_passed = all(results)
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")

    # Cleanup
    cleanup()

    exit(0 if all_passed else 1)
