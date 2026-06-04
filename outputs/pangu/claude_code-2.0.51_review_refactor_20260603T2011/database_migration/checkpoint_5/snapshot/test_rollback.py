#!/usr/bin/env python3
"""Test script for rollback functionality."""

import subprocess
import sqlite3
import json
import os
import tempfile

def cleanup_db(db_path):
    """Remove database file if it exists."""
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass

def run_migration(migration_content, db_path):
    """Run a migration with the given content."""
    # Write migration to temp file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(migration_content, f)
        migration_file = f.name

    try:
        result = subprocess.run(
            ['python', 'migration_tool.py', 'migrate', migration_file, db_path],
            capture_output=True,
            text=True
        )
        return result
    finally:
        os.unlink(migration_file)

def run_rollback(db_path, args):
    """Run a rollback with the given arguments."""
    result = subprocess.run(
        ['python', 'migration_tool.py', 'rollback', db_path] + args,
        capture_output=True,
        text=True
    )
    return result

def test_simple_rollback():
    """Test simple rollback of last migration."""
    print("=" * 60)
    print("Testing Simple Rollback")
    print("=" * 60)

    db_path = '/tmp/test_rollback_simple.sqlite'
    cleanup_db(db_path)

    # Apply migrations using migration files
    migration1 = {
        "version": 1,
        "description": "Create users table",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "name", "type": "TEXT"}
                ]
            }
        ]
    }

    migration2 = {
        "version": 2,
        "description": "Create posts table",
        "operations": [
            {
                "type": "create_table",
                "table": "posts",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "user_id", "type": "INTEGER", "not_null": True},
                    {"name": "title", "type": "TEXT"}
                ]
            }
        ]
    }

    result = run_migration(migration1, db_path)
    assert result.returncode == 0, f"Migration 1 failed: {result.stderr}"

    result = run_migration(migration2, db_path)
    assert result.returncode == 0, f"Migration 2 failed: {result.stderr}"

    # Check migrations were applied
    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)
    assert tool.is_migration_applied(1)
    assert tool.is_migration_applied(2)
    print("✓ Both migrations applied")

    # Rollback last migration
    result = run_rollback(db_path, [])
    print(f"Rollback result: {result.returncode}")
    print(f"stdout: {result.stdout}")
    print(f"stderr: {result.stderr}")

    assert result.returncode == 0, f"Rollback failed: {result.stderr}"
    assert '"event": "rollback_started"' in result.stdout
    assert '"event": "operation_rolled_back"' in result.stdout
    assert '"event": "rollback_complete"' in result.stdout
    assert '"event": "rollback_finished"' in result.stdout

    # Verify posts table is gone, users table remains
    assert not tool.table_exists("posts")
    assert tool.table_exists("users")
    assert tool.is_migration_applied(1)
    assert not tool.is_migration_applied(2)

    latest = tool.get_latest_migration_version()
    assert latest == 1, f"Expected latest version 1, got {latest}"

    print("✓ Simple rollback works correctly")
    print()

def test_rollback_to_version():
    """Test rollback to a specific version."""
    print("=" * 60)
    print("Testing Rollback to Specific Version")
    print("=" * 60)

    db_path = '/tmp/test_rollback_version.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Apply migrations using migration files
    migration1 = {
        "version": 1,
        "description": "Create users table",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "name", "type": "TEXT"}
                ]
            }
        ]
    }

    migration2 = {
        "version": 2,
        "description": "Add age column",
        "operations": [
            {
                "type": "add_column",
                "table": "users",
                "column": {
                    "name": "age",
                    "type": "INTEGER"
                }
            }
        ]
    }

    migration3 = {
        "version": 3,
        "description": "Create posts table",
        "operations": [
            {
                "type": "create_table",
                "table": "posts",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "title", "type": "TEXT"}
                ]
            }
        ]
    }

    migration4 = {
        "version": 4,
        "description": "Add content column to posts",
        "operations": [
            {
                "type": "add_column",
                "table": "posts",
                "column": {
                    "name": "content",
                    "type": "TEXT"
                }
            }
        ]
    }

    for migration in [migration1, migration2, migration3, migration4]:
        result = run_migration(migration, db_path)
        assert result.returncode == 0, f"Migration failed: {result.stderr}"

    # Verify all migrations applied
    assert tool.is_migration_applied(1)
    assert tool.is_migration_applied(2)
    assert tool.is_migration_applied(3)
    assert tool.is_migration_applied(4)
    print("✓ All 4 migrations applied")

    # Rollback to version 2 (should rollback 3 and 4)
    result = run_rollback(db_path, ["--to-version", "2"])
    print(f"Rollback result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Rollback failed: {result.stderr}"
    # The versions should be in descending order (4, then 3)
    assert '"versions_rolled_back": [4, 3]' in result.stdout or '"versions_rolled_back": [4, 3]' in result.stdout

    # Verify correct state after rollback
    assert tool.table_exists("users")
    assert not tool.table_exists("posts")
    assert tool.is_migration_applied(1)
    assert tool.is_migration_applied(2)
    assert not tool.is_migration_applied(3)
    assert not tool.is_migration_applied(4)

    latest = tool.get_latest_migration_version()
    assert latest == 2, f"Expected latest version 2, got {latest}"

    print("✓ Rollback to version works correctly")
    print()

def test_rollback_with_count():
    """Test rollback with --count option."""
    print("=" * 60)
    print("Testing Rollback with --count")
    print("=" * 60)

    db_path = '/tmp/test_rollback_count.sqlite'
    cleanup_db(db_path)

    # Apply migrations 1-5
    for i in range(1, 6):
        migration = {
            "version": i,
            "description": f"Create table {i}",
            "operations": [
                {
                    "type": "create_table",
                    "table": f"table_{i}",
                    "columns": [
                        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True}
                    ]
                }
            ]
        }
        result = run_migration(migration, db_path)
        assert result.returncode == 0, f"Migration {i} failed: {result.stderr}"

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)
    assert len(tool.get_migrations()) == 5
    print("✓ 5 migrations applied")

    # Rollback last 2 migrations
    result = run_rollback(db_path, ["--count", "2"])
    print(f"Rollback result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Rollback failed: {result.stderr}"

    migrations = tool.get_migrations()
    assert len(migrations) == 3, f"Expected 3 migrations, got {len(migrations)}"
    versions = [m["version"] for m in migrations]
    assert versions == [1, 2, 3], f"Expected versions [1, 2, 3], got {versions}"

    print("✓ Rollback with --count works correctly")
    print()

def test_rollback_no_migrations():
    """Test rollback when no migrations have been applied."""
    print("=" * 60)
    print("Testing Rollback with No Migrations")
    print("=" * 60)

    db_path = '/tmp/test_rollback_none.sqlite'
    cleanup_db(db_path)

    result = run_rollback(db_path, [])
    print(f"Rollback result: {result.returncode}")
    print(f"stderr: {result.stderr}")

    assert result.returncode == 1, f"Should have failed with exit code 1"
    assert "no migrations to rollback" in result.stderr

    print("✓ Correctly handles no migrations case")
    print()

def test_rollback_explicit_operations():
    """Test rollback using explicit rollback_operations."""
    print("=" * 60)
    print("Testing Rollback with Explicit Operations")
    print("=" * 60)

    db_path = '/tmp/test_rollback_explicit.sqlite'
    cleanup_db(db_path)

    # First create the users table with first_name and last_name via migration
    migration0 = {
        "version": 0,
        "description": "Create users table for testing",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "first_name", "type": "TEXT"},
                    {"name": "last_name", "type": "TEXT"}
                ]
            }
        ]
    }
    result = run_migration(migration0, db_path)
    assert result.returncode == 0, f"Initial migration failed: {result.stderr}"

    # Insert test data
    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)
    conn = tool._get_connection()
    conn.executemany(
        "INSERT INTO users (first_name, last_name) VALUES (?, ?)",
        [('John', 'Doe'), ('Jane', 'Smith')]
    )
    conn.commit()

    # Run migration with explicit rollback_operations
    migration1 = {
        "version": 1,
        "description": "Add and compute computed column",
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
        ],
        "rollback_operations": [
            {
                "type": "drop_column",
                "table": "users",
                "column": "full_name"
            }
        ]
    }

    result = run_migration(migration1, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert tool.is_migration_applied(1)
    print("✓ Migration with explicit rollback_operations applied")

    # Rollback should use explicit operations
    result = run_rollback(db_path, [])
    print(f"Rollback result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Rollback failed: {result.stderr}"
    assert not tool.is_migration_applied(1)
    assert not tool.column_exists("users", "full_name")

    print("✓ Rollback with explicit operations works correctly")
    print()

def test_rollback_with_data_migration():
    """Test rollback of column data migration."""
    print("=" * 60)
    print("Testing Rollback of Column Data Migration")
    print("=" * 60)

    db_path = '/tmp/test_rollback_migrate.sqlite'
    cleanup_db(db_path)

    # Create users table with email column
    migration1 = {
        "version": 1,
        "description": "Create users table",
        "operations": [
            {
                "type": "create_table",
                "table": "users",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "name", "type": "TEXT"},
                    {"name": "email", "type": "TEXT"}
                ]
            }
        ]
    }

    result = run_migration(migration1, db_path)
    assert result.returncode == 0, f"Migration 1 failed: {result.stderr}"

    # Insert test data
    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)
    conn = tool._get_connection()
    conn.executemany(
        "INSERT INTO users (name, email) VALUES (?, ?)",
        [('Alice', 'alice@example.com'), ('Bob', 'bob@example.com'), ('Charlie', None)]
    )
    conn.commit()

    # Run migration: add email_address, migrate data, drop email
    migration2 = {
        "version": 2,
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
                "to_column": "email_address",
                "default_value": None
            },
            {
                "type": "drop_column",
                "table": "users",
                "column": "email"
            }
        ]
    }

    result = run_migration(migration2, db_path)
    print(f"Migration result: {result.returncode}")
    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert tool.is_migration_applied(2)
    assert tool.column_exists("users", "email_address")
    assert not tool.column_exists("users", "email")
    print("✓ Column migration applied")

    # Verify data was migrated
    cursor = conn.execute("SELECT email_address FROM users ORDER BY id")
    emails = [row[0] for row in cursor.fetchall()]
    assert emails == ['alice@example.com', 'bob@example.com', None]
    print("✓ Data migrated correctly")

    # Rollback
    result = run_rollback(db_path, [])
    print(f"Rollback result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Rollback failed: {result.stderr}"
    assert not tool.is_migration_applied(2)
    assert tool.column_exists("users", "email")
    assert tool.column_exists("users", "email_address")

    # Verify data was restored (email should have the data from email_address)
    cursor = conn.execute("SELECT email FROM users ORDER BY id")
    emails = [row[0] for row in cursor.fetchall()]
    assert emails == ['alice@example.com', 'bob@example.com', None]

    print("✓ Column data migration rollback works correctly")
    print()

if __name__ == "__main__":
    test_simple_rollback()
    test_rollback_to_version()
    test_rollback_with_count()
    test_rollback_no_migrations()
    test_rollback_explicit_operations()
    test_rollback_with_data_migration()

    # Clean up
    for f in [
        '/tmp/test_rollback_simple.sqlite',
        '/tmp/test_rollback_version.sqlite',
        '/tmp/test_rollback_count.sqlite',
        '/tmp/test_rollback_none.sqlite',
        '/tmp/test_rollback_explicit.sqlite',
        '/tmp/test_rollback_migrate.sqlite'
    ]:
        try:
            os.unlink(f)
        except Exception:
            pass

    print("=" * 60)
    print("All rollback tests passed!")
    print("=" * 60)
