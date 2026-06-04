#!/usr/bin/env python3
"""Test script for checkpoint 3 - Foreign Keys, Indexes, and Constraints."""

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

def test_example_1_foreign_key():
    """Test Example 1: Create related tables with foreign key."""
    print("=" * 60)
    print("Testing Example 1: Foreign Key CASCADE")
    print("=" * 60)

    db_path = '/tmp/test_db_fk1.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table first
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT", "not_null": True}
    ], 5)

    # Insert a test user
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (name) VALUES ('Test User')")
    conn.commit()

    # Run migration to add posts table with foreign key
    migration = {
        "version": 6,
        "description": "Create posts table with foreign key to users",
        "operations": [
            {
                "type": "create_table",
                "table": "posts",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "user_id", "type": "INTEGER", "not_null": True},
                    {"name": "title", "type": "TEXT", "not_null": True},
                    {"name": "content", "type": "TEXT"}
                ]
            },
            {
                "type": "add_foreign_key",
                "table": "posts",
                "name": "fk_posts_user_id",
                "columns": ["user_id"],
                "references": {
                    "table": "users",
                    "columns": ["id"]
                },
                "on_delete": "CASCADE"
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")
    print(f"stderr: {result.stderr}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "create_table"' in result.stdout
    assert '"event": "operation_applied", "type": "add_foreign_key"' in result.stdout

    # Verify foreign key works by getting fresh connection with FK ON
    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA foreign_keys = ON")

    # Insert a valid post
    conn2.execute("INSERT INTO posts (user_id, title) VALUES (1, 'Test Post')")
    conn2.commit()

    # Try to insert violating data
    try:
        conn2.execute("INSERT INTO posts (user_id, title) VALUES (999, 'Invalid Post')")
        conn2.commit()
        assert False, "Should have raised FK violation"
    except sqlite3.IntegrityError:
        pass  # Expected

    # Test cascade delete
    conn2.execute("DELETE FROM users WHERE id = 1")
    conn2.commit()

    cursor = conn2.execute("SELECT COUNT(*) FROM posts WHERE user_id = 1")
    count = cursor.fetchone()[0]
    assert count == 0, f"Cascade delete did not work, {count} posts remain"

    print("✓ Foreign key with CASCADE works correctly")
    print()

def test_example_2_composite_index():
    """Test Example 2: Create composite index."""
    print("=" * 60)
    print("Testing Example 2: Composite Index")
    print("=" * 60)

    db_path = '/tmp/test_db_idx1.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create posts table
    tool.create_table("posts", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "user_id", "type": "INTEGER", "not_null": True},
        {"name": "created_at", "type": "TEXT", "not_null": True},
        {"name": "title", "type": "TEXT"}
    ], 7)

    # Run migration to create composite index
    migration = {
        "version": 7,
        "description": "Add index for user posts queries",
        "operations": [
            {
                "type": "create_index",
                "name": "idx_posts_user_created",
                "table": "posts",
                "columns": ["user_id", "created_at"],
                "unique": False
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "create_index"' in result.stdout

    # Verify index exists
    indexes = tool.get_indexes("posts")
    assert any(idx["name"] == "idx_posts_user_created" for idx in indexes), \
        "Index was not created"

    # Verify index columns
    idx_info = next(idx for idx in indexes if idx["name"] == "idx_posts_user_created")
    assert idx_info["columns"] == ["user_id", "created_at"], \
        f"Index columns mismatch: {idx_info['columns']}"

    print("✓ Composite index created correctly")
    print()

def test_example_3_check_constraint():
    """Test Example 3: Add check constraint."""
    print("=" * 60)
    print("Testing Example 3: Check Constraint")
    print("=" * 60)

    db_path = '/tmp/test_db_ck1.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT"},
        {"name": "age", "type": "INTEGER"}
    ], 8)

    # Insert valid data
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (name, age) VALUES ('Alice', 25)")
    conn.commit()

    # Run migration to add check constraint
    migration = {
        "version": 8,
        "description": "Add age validation constraint",
        "operations": [
            {
                "type": "add_check_constraint",
                "table": "users",
                "name": "chk_users_age",
                "expression": "age >= 0 AND age <= 150"
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "add_check_constraint"' in result.stdout

    # Verify constraint works - try to insert invalid age
    try:
        conn.execute("INSERT INTO users (name, age) VALUES ('Bob', 200)")
        conn.commit()
        assert False, "Should have raised constraint violation"
    except sqlite3.IntegrityError:
        pass  # Expected

    # Verify valid data can still be inserted
    conn.execute("INSERT INTO users (name, age) VALUES ('Charlie', 30)")
    conn.commit()

    cursor = conn.execute("SELECT COUNT(*) FROM users WHERE age = 30")
    assert cursor.fetchone()[0] == 1

    print("✓ Check constraint works correctly")
    print()

def test_example_4_complex_migration():
    """Test Example 4: Complex migration with multiple constraints."""
    print("=" * 60)
    print("Testing Example 4: Complex Migration")
    print("=" * 60)

    db_path = '/tmp/test_db_complex.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table first
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "username", "type": "TEXT", "unique": True}
    ], 8)

    # Insert test user
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (username) VALUES ('testuser')")
    conn.commit()

    # Run complex migration
    migration = {
        "version": 9,
        "description": "Add comments table with foreign keys and indexes",
        "operations": [
            {
                "type": "create_table",
                "table": "comments",
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
                    {"name": "post_id", "type": "INTEGER", "not_null": True},
                    {"name": "user_id", "type": "INTEGER", "not_null": True},
                    {"name": "content", "type": "TEXT", "not_null": True},
                    {"name": "created_at", "type": "TIMESTAMP"}
                ]
            },
            {
                "type": "add_foreign_key",
                "table": "comments",
                "name": "fk_comments_post_id",
                "columns": ["post_id"],
                "references": {
                    "table": "posts",
                    "columns": ["id"]
                },
                "on_delete": "CASCADE"
            },
            {
                "type": "add_foreign_key",
                "table": "comments",
                "name": "fk_comments_user_id",
                "columns": ["user_id"],
                "references": {
                    "table": "users",
                    "columns": ["id"]
                },
                "on_delete": "CASCADE"
            },
            {
                "type": "create_index",
                "name": "idx_comments_post_id",
                "table": "comments",
                "columns": ["post_id"]
            },
            {
                "type": "create_index",
                "name": "idx_comments_user_id",
                "table": "comments",
                "columns": ["user_id"]
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode != 0, "Migration should have failed - posts table doesn't exist"
    assert "does not exist" in result.stderr or "does not exist" in result.stdout

    print("✓ Correctly failed when referenced table doesn't exist")
    print()

def test_example_5_drop_foreign_key():
    """Test Example 5: Drop foreign key."""
    print("=" * 60)
    print("Testing Example 5: Drop Foreign Key")
    print("=" * 60)

    db_path = '/tmp/test_db_dropfk.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT", "not_null": True}
    ], 5)

    # Insert test user
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (name) VALUES ('Test User')")
    conn.commit()

    # Create posts table
    tool.create_table("posts", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "user_id", "type": "INTEGER", "not_null": True},
        {"name": "title", "type": "TEXT", "not_null": True}
    ], 6)

    # Add foreign key
    migration_add = {
        "version": 6,
        "description": "Add foreign key",
        "operations": [
            {
                "type": "add_foreign_key",
                "table": "posts",
                "name": "fk_posts_user_id",
                "columns": ["user_id"],
                "references": {
                    "table": "users",
                    "columns": ["id"]
                },
                "on_delete": "CASCADE"
            }
        ]
    }
    result = run_migration(migration_add, db_path)
    assert result.returncode == 0, f"Add FK failed: {result.stderr}"

    # Drop foreign key
    migration_drop = {
        "version": 7,
        "description": "Remove foreign key constraint",
        "operations": [
            {
                "type": "drop_foreign_key",
                "table": "posts",
                "name": "fk_posts_user_id"
            }
        ]
    }

    result = run_migration(migration_drop, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "drop_foreign_key"' in result.stdout

    # Verify table still has data
    cursor = conn.execute("SELECT COUNT(*) FROM posts")
    # Should be 0 since we haven't inserted anything, but table should exist
    assert cursor.fetchone()[0] == 0  # No posts inserted

    print("✓ Foreign key dropped correctly")
    print()

def test_unique_index():
    """Test creating a unique index."""
    print("=" * 60)
    print("Testing Unique Index")
    print("=" * 60)

    db_path = '/tmp/test_db_unique_idx.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "email", "type": "TEXT"}
    ], 1)

    # Run migration to create unique index
    migration = {
        "version": 2,
        "description": "Add unique index on email",
        "operations": [
            {
                "type": "create_index",
                "name": "idx_users_email",
                "table": "users",
                "columns": ["email"],
                "unique": True
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    # Verify unique constraint works
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (email) VALUES ('test@example.com')")
    conn.commit()

    try:
        conn.execute("INSERT INTO users (email) VALUES ('test@example.com')")
        conn.commit()
        assert False, "Should have raised unique constraint violation"
    except sqlite3.IntegrityError:
        pass  # Expected

    print("✓ Unique index works correctly")
    print()

def test_drop_index():
    """Test dropping an index."""
    print("=" * 60)
    print("Testing Drop Index")
    print("=" * 60)

    db_path = '/tmp/test_db_dropidx.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "email", "type": "TEXT"}
    ], 1)

    # Create index
    tool.create_index("idx_users_email", "users", ["email"], False, 2)

    # Drop index
    migration = {
        "version": 3,
        "description": "Drop index",
        "operations": [
            {
                "type": "drop_index",
                "name": "idx_users_email"
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "drop_index"' in result.stdout

    # Verify index is gone
    assert not tool.index_exists("idx_users_email"), "Index still exists"

    print("✓ Index dropped correctly")
    print()

def test_drop_check_constraint():
    """Test dropping a check constraint."""
    print("=" * 60)
    print("Testing Drop Check Constraint")
    print("=" * 60)

    db_path = '/tmp/test_db_dropck.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table with age column
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT"},
        {"name": "age", "type": "INTEGER"}
    ], 1)

    # Add check constraint
    migration_add = {
        "version": 2,
        "description": "Add age check constraint",
        "operations": [
            {
                "type": "add_check_constraint",
                "table": "users",
                "name": "chk_users_age",
                "expression": "age >= 0 AND age <= 150"
            }
        ]
    }
    result = run_migration(migration_add, db_path)
    assert result.returncode == 0, f"Add check constraint failed: {result.stderr}"

    # Drop check constraint
    migration_drop = {
        "version": 3,
        "description": "Drop age check constraint",
        "operations": [
            {
                "type": "drop_check_constraint",
                "table": "users",
                "name": "chk_users_age"
            }
        ]
    }

    result = run_migration(migration_drop, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"
    assert '"event": "operation_applied", "type": "drop_check_constraint"' in result.stdout

    # Now we should be able to insert invalid age
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (name, age) VALUES ('Bob', 200)")
    conn.commit()

    cursor = conn.execute("SELECT COUNT(*) FROM users WHERE age = 200")
    assert cursor.fetchone()[0] == 1

    print("✓ Check constraint dropped correctly")
    print()

def test_foreign_key_violation():
    """Test foreign key violation detection."""
    print("=" * 60)
    print("Testing Foreign Key Violation")
    print("=" * 60)

    db_path = '/tmp/test_db_fk_violation.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT"}
    ], 1)

    # Create posts table
    tool.create_table("posts", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "user_id", "type": "INTEGER", "not_null": True},
        {"name": "title", "type": "TEXT"}
    ], 2)

    # Insert a post with invalid user_id (user_id 1 doesn't exist)
    conn = tool._get_connection()
    conn.execute("INSERT INTO posts (user_id, title) VALUES (999, 'Invalid Post')")
    conn.commit()

    # Try to add foreign key - should detect violation
    migration = {
        "version": 3,
        "description": "Add foreign key with existing data",
        "operations": [
            {
                "type": "add_foreign_key",
                "table": "posts",
                "name": "fk_posts_user_id",
                "columns": ["user_id"],
                "references": {
                    "table": "users",
                    "columns": ["id"]
                }
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stderr: {result.stderr}")

    assert result.returncode != 0, "Should have failed with foreign key violation"

    print("✓ Foreign key violation correctly detected")
    print()

def test_duplicate_index_name():
    """Test duplicate index name detection."""
    print("=" * 60)
    print("Testing Duplicate Index Name")
    print("=" * 60)

    db_path = '/tmp/test_db_dup_idx.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "email", "type": "TEXT"}
    ], 1)

    # Create first index
    migration1 = {
        "version": 2,
        "description": "First index",
        "operations": [
            {
                "type": "create_index",
                "name": "idx_users_email",
                "table": "users",
                "columns": ["email"]
            }
        ]
    }
    result = run_migration(migration1, db_path)
    assert result.returncode == 0, f"First migration failed: {result.stderr}"

    # Try to create index with same name
    migration2 = {
        "version": 3,
        "description": "Duplicate index",
        "operations": [
            {
                "type": "create_index",
                "name": "idx_users_email",
                "table": "users",
                "columns": ["email"]
            }
        ]
    }

    result = run_migration(migration2, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stderr: {result.stderr}")

    assert result.returncode != 0, "Should have failed with duplicate index name"
    assert "already exists" in result.stderr.lower() or "already exists" in result.stdout.lower()

    print("✓ Duplicate index name correctly detected")
    print()

def test_on_delete_actions():
    """Test different ON DELETE actions."""
    print("=" * 60)
    print("Testing ON DELETE Actions")
    print("=" * 60)

    db_path = '/tmp/test_db_fk_actions.sqlite'
    cleanup_db(db_path)

    tool_module = __import__('migration_tool')
    tool = tool_module.MigrationTool(db_path)

    # Create users table
    tool.create_table("users", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "name", "type": "TEXT"}
    ], 1)

    # Insert test user
    conn = tool._get_connection()
    conn.execute("INSERT INTO users (name) VALUES ('User1')")
    conn.commit()

    # Create posts table with RESTRICT
    tool.create_table("posts", [
        {"name": "id", "type": "INTEGER", "primary_key": True, "auto_increment": True},
        {"name": "user_id", "type": "INTEGER", "not_null": True},
        {"name": "title", "type": "TEXT"}
    ], 2)

    migration = {
        "version": 3,
        "description": "Add foreign key with RESTRICT",
        "operations": [
            {
                "type": "add_foreign_key",
                "table": "posts",
                "name": "fk_posts_user_id",
                "columns": ["user_id"],
                "references": {
                    "table": "users",
                    "columns": ["id"]
                },
                "on_delete": "RESTRICT"
            }
        ]
    }

    result = run_migration(migration, db_path)
    print(f"Migration result: {result.returncode}")
    print(f"stdout: {result.stdout}")

    assert result.returncode == 0, f"Migration failed: {result.stderr}"

    # Insert a post
    conn.execute("INSERT INTO posts (user_id, title) VALUES (1, 'Post')")
    conn.commit()

    # Try to delete user with RESTRICT - should fail
    try:
        conn.execute("DELETE FROM users WHERE id = 1")
        conn.commit()
        assert False, "Should have failed with RESTRICT"
    except sqlite3.IntegrityError:
        pass  # Expected

    print("✓ ON DELETE RESTRICT works correctly")
    print()

if __name__ == "__main__":
    # Run all tests
    test_example_1_foreign_key()
    test_example_2_composite_index()
    test_example_3_check_constraint()
    test_example_4_complex_migration()
    test_example_5_drop_foreign_key()
    test_unique_index()
    test_drop_index()
    test_drop_check_constraint()
    test_foreign_key_violation()
    test_duplicate_index_name()
    test_on_delete_actions()

    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)

    # Clean up
    for f in [
        '/tmp/test_db_fk1.sqlite',
        '/tmp/test_db_idx1.sqlite',
        '/tmp/test_db_ck1.sqlite',
        '/tmp/test_db_complex.sqlite',
        '/tmp/test_db_dropfk.sqlite',
        '/tmp/test_db_unique_idx.sqlite',
        '/tmp/test_db_dropidx.sqlite',
        '/tmp/test_db_dropck.sqlite',
        '/tmp/test_db_fk_violation.sqlite',
        '/tmp/test_db_dup_idx.sqlite',
        '/tmp/test_db_fk_actions.sqlite'
    ]:
        try:
            os.unlink(f)
        except Exception:
            pass
