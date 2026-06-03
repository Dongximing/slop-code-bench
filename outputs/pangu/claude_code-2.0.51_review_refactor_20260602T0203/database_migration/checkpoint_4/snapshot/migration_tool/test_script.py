#!/usr/bin/env python3
"""Test script to verify data migrations work correctly."""

import sqlite3
import os

# Create the database by applying version 1 migration
os.system('python migration_tool.py migrate migration_v1.json test.db')

# Insert some sample data directly
conn = sqlite3.connect('test.db')
cursor = conn.cursor()
cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('John', 'Doe', 'john@example.com')")
cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('Jane', 'Smith', 'jane@example.com')")
cursor.execute("INSERT INTO users (first_name, last_name, email) VALUES ('Bob', NULL, 'bob@example.com')")
conn.commit()

# Verify the data
print("Data inserted:")
cursor.execute("SELECT * FROM users")
for row in cursor.fetchall():
    print(f"  {row}")
conn.close()

print("\n--- Running migration 3: Add and backfill status column ---")
os.system('python migration_tool.py migrate migration_v3.json test.db')

# Verify the result
conn = sqlite3.connect('test.db')
cursor = conn.cursor()
print("\nAfter backfilling:")
cursor.execute("SELECT id, first_name, last_name, status FROM users")
for row in cursor.fetchall():
    print(f"  {row}")
conn.close()
