#!/usr/bin/env python3
"""Comprehensive test for all new operations."""

import subprocess
import json
import sys
import os

DB_PATH = "/tmp/test_db.db"
MIGRATIONS_DIR = "/workspace/test_migrations"

def run_migration(name):
    """Run a migration file and return success status and output."""
    migration_file = os.path.join(MIGRATIONS_DIR, name)
    result = subprocess.run(
        ["python", "/workspace/migration_tool.py", "migrate", migration_file, DB_PATH],
        capture_output=True,
        text=True
    )
    return result.returncode == 0, result.stdout, result.stderr

def test_full_workflow():
    """Test complete workflow with all features."""
    print("Running comprehensive test...")

    # Test 1: Create users table
    success, stdout, stderr = run_migration("migration_complex.json")
    if not success:
        print(f"FAIL: Complex migration failed\n{stdout}\n{stderr}")
        return False
    print("PASS: Complex migration succeeded")

    # Verify outputs contain expected events
    outputs = stdout.strip().split('\n')
    events = [json.loads(line) for line in outputs if line.strip()]
    event_types = [e['event'] for e in events]

    expected_events = ['operation_applied', 'operation_applied', 'operation_applied',
                       'operation_applied', 'operation_applied', 'operation_applied',
                       'operation_applied', 'operation_applied', 'operation_applied',
                       'operation_applied', 'migration_complete']

    if len(events) == len(expected_events) and event_types == expected_events:
        print("PASS: Correct sequence of events")
    else:
        print(f"FAIL: Expected {len(expected_events)} events but got {len(events)}")
        return False

    # Verify specific operations were recorded
    applied_ops = [e for e in events if e['event'] == 'operation_applied']

    # Check that all expected operations were applied
    op_types = [e['type'] for e in applied_ops]
    assert 'create_table' in op_types, "create_table not found"
    assert 'add_check_constraint' in op_types, "add_check_constraint not found"
    assert 'add_foreign_key' in op_types, "add_foreign_key not found"
    assert 'create_index' in op_types, "create_index not found"

    print("PASS: All operation types recorded")

    # Test 2: Create composite index
    print("\nTesting composite index creation...")

    # Test that indexes were created
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
    indexes = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert 'idx_posts_user_id' in indexes, "idx_posts_user_id not found"
    print("PASS: Index 'idx_posts_user_id' exists")

    # Test 3: Test foreign key violation
    print("\nTesting foreign key validation...")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("INSERT INTO posts (user_id, title, content) VALUES (999, 'Test', 'Content')")
        conn.commit()
        print("FAIL: Should have raised foreign key error")
        return False
    except sqlite3.IntegrityError:
        print("PASS: Foreign key constraint correctly enforced")
    finally:
        conn.close()

    # Test 4: Test check constraint
    print("\nTesting check constraint validation...")
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("INSERT INTO users (name, email, age) VALUES ('Test', 'test@test.com', 200)")
        conn.commit()
        print("FAIL: Should have raised check constraint error")
        return False
    except sqlite3.IntegrityError:
        print("PASS: Check constraint correctly enforced")
    finally:
        conn.close()

    print("\nALL TESTS PASSED!")
    return True

if __name__ == "__main__":
    success = test_full_workflow()
    sys.exit(0 if success else 1)
