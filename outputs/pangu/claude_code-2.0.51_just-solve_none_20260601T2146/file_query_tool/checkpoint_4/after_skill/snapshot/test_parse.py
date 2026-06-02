#!/usr/bin/env python3
"""Simple test to verify parsing still works."""
import sys
import os
import tempfile

# Create a test CSV
csv_content = """id,name,age
1,Alice,30
2,Bob,25
3,Charlie,35"""

with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
    f.write(csv_content)
    csv_path = f.name

try:
    # Create test data directory
    with tempfile.TemporaryDirectory() as tmpdir:
        import shutil
        shutil.copy(csv_path, os.path.join(tmpdir, 'users.csv'))

        os.chdir(tmpdir)
        sys.path.insert(0, tmpdir)

        try:
            from query_parser import parse_query

            # Test various SQL queries
            test_queries = [
                "SELECT id, name FROM users",
                "SELECT * FROM users WHERE age > 25",
                "SELECT name, age FROM users ORDER BY age DESC",
                "SELECT COUNT(*) FROM users",
                "SELECT age, COUNT(*) FROM users GROUP BY age",
                "SELECT name, age FROM users WHERE name LIKE '%al%'",
            ]

            for sql in test_queries:
                try:
                    result = parse_query(sql)
                    print(f"✓ Parsed: {sql}")
                    print(f"  -> {result['select']}")
                except Exception as e:
                    print(f"✗ Failed to parse: {sql}")
                    print(f"  Error: {e}")

        except ImportError as e:
            print(f"Import failed: {e}")
            print("Dependencies may need to be installed: sqlparse, pandas")
finally:
    os.unlink(csv_path)