#!/usr/bin/env python3
"""Database Migration Tool - Applies schema migrations to SQLite databases."""

import argparse
import json
import os
import re
import sqlite3
import sys


VALID_SQLITE_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}


def error_exit(message):
    """Print error to stderr and exit with code 1."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def is_valid_identifier(name):
    """Check if a name is a valid SQLite identifier (alphanumeric + underscore, starts with letter/underscore)."""
    if not name or not isinstance(name, str):
        return False
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def quote_identifier(name):
    """Quote a SQLite identifier to prevent SQL injection."""
    # Double any existing double quotes, then wrap in double quotes
    return '"' + name.replace('"', '""') + '"'


def validate_column(col, operation_type="create_table"):
    """Validate a column definition. Returns error message or None."""
    if not isinstance(col, dict):
        return "column must be an object"

    if "name" not in col:
        return "missing required field 'name' in column definition"
    if "type" not in col:
        return "missing required field 'type' in column definition"

    if not isinstance(col["name"], str) or not is_valid_identifier(col["name"]):
        return f"invalid column name: '{col.get('name')}'"
    if not isinstance(col["type"], str) or col["type"].upper() not in VALID_SQLITE_TYPES:
        return f"invalid column type: '{col.get('type')}'"

    # Validate optional boolean fields
    for bool_field in ("primary_key", "auto_increment", "not_null", "unique"):
        if bool_field in col and not isinstance(col[bool_field], bool):
            return f"field '{bool_field}' must be a boolean"

    # auto_increment must be on INTEGER primary key
    if col.get("auto_increment", False):
        if col["type"].upper() != "INTEGER":
            return "auto_increment can only be used with INTEGER type"
        if not col.get("primary_key", False):
            return "auto_increment can only be used with primary_key"

    return None


def validate_migration(migration):
    """Validate the migration JSON structure. Returns error message or None."""
    if not isinstance(migration, dict):
        return "migration must be a JSON object"

    if "version" not in migration:
        return "missing required field 'version'"
    if "description" not in migration:
        return "missing required field 'description'"
    if "operations" not in migration:
        return "missing required field 'operations'"

    if not isinstance(migration["version"], int) or migration["version"] <= 0:
        return "version must be a positive integer"
    if not isinstance(migration["description"], str):
        return "description must be a string"
    if not isinstance(migration["operations"], list):
        return "operations must be an array"
    if len(migration["operations"]) == 0:
        return "operations array must not be empty"

    pk_count_in_create = 0
    for i, op in enumerate(migration["operations"]):
        if not isinstance(op, dict):
            return f"operation {i} must be an object"
        if "type" not in op:
            return f"operation {i}: missing required field 'type'"

        op_type = op["type"]

        if op_type == "create_table":
            if "table" not in op:
                return f"operation {i}: missing required field 'table'"
            if "columns" not in op:
                return f"operation {i}: missing required field 'columns'"

            if not isinstance(op["table"], str) or not is_valid_identifier(op["table"]):
                return f"operation {i}: invalid table name '{op.get('table')}'"
            if not isinstance(op["columns"], list) or len(op["columns"]) == 0:
                return f"operation {i}: columns must be a non-empty array"

            pk_count = 0
            for j, col in enumerate(op["columns"]):
                err = validate_column(col, "create_table")
                if err:
                    return f"operation {i}, column {j}: {err}"
                if col.get("primary_key", False):
                    pk_count += 1
            if pk_count > 1:
                return f"operation {i}: at most one column can have primary_key=true"

        elif op_type == "add_column":
            if "table" not in op:
                return f"operation {i}: missing required field 'table'"
            if "column" not in op:
                return f"operation {i}: missing required field 'column'"

            if not isinstance(op["table"], str) or not is_valid_identifier(op["table"]):
                return f"operation {i}: invalid table name '{op.get('table')}'"

            err = validate_column(op["column"], "add_column")
            if err:
                return f"operation {i}: {err}"

        elif op_type == "drop_column":
            if "table" not in op:
                return f"operation {i}: missing required field 'table'"
            if "column" not in op:
                return f"operation {i}: missing required field 'column'"

            if not isinstance(op["table"], str) or not is_valid_identifier(op["table"]):
                return f"operation {i}: invalid table name '{op.get('table')}'"
            if not isinstance(op["column"], str) or not is_valid_identifier(op["column"]):
                return f"operation {i}: invalid column name '{op.get('column')}'"

        else:
            return f"operation {i}: unknown operation type '{op_type}'"

    return None


def build_column_sql(col):
    """Build the SQL fragment for a column definition."""
    parts = [quote_identifier(col["name"]), col["type"].upper()]

    if col.get("primary_key", False):
        parts.append("PRIMARY KEY")
        if col.get("auto_increment", False):
            parts.append("AUTOINCREMENT")

    if col.get("not_null", False):
        parts.append("NOT NULL")

    if col.get("unique", False):
        parts.append("UNIQUE")

    if "default" in col and col["default"] is not None:
        parts.append(f"DEFAULT {col['default']}")

    return " ".join(parts)


def get_table_info(conn, table_name):
    """Get column info for a table. Returns list of column info dicts."""
    cursor = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    columns = []
    for row in cursor.fetchall():
        columns.append({
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "not_null": bool(row[3]),
            "default_value": row[4],
            "primary_key": bool(row[5]),
        })
    return columns


def table_exists(conn, table_name):
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def column_exists(conn, table_name, column_name):
    """Check if a column exists in a table."""
    columns = get_table_info(conn, table_name)
    return any(c["name"] == column_name for c in columns)


def get_column_names(conn, table_name):
    """Get list of column names for a table."""
    columns = get_table_info(conn, table_name)
    return [c["name"] for c in columns]


def init_migrations_table(conn):
    """Create the _migrations tracking table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def is_migration_applied(conn, version):
    """Check if a migration version has already been applied."""
    cursor = conn.execute(
        "SELECT version FROM _migrations WHERE version = ?",
        (version,)
    )
    return cursor.fetchone() is not None


def record_migration(conn, version, description):
    """Record a successfully applied migration."""
    conn.execute(
        "INSERT INTO _migrations (version, description) VALUES (?, ?)",
        (version, description)
    )


def apply_create_table(conn, op, version):
    """Apply a create_table operation."""
    table_name = op["table"]

    if table_exists(conn, table_name):
        error_exit(f"invalid operation: table '{table_name}' already exists")

    col_defs = [build_column_sql(col) for col in op["columns"]]
    sql = f"CREATE TABLE {quote_identifier(table_name)} ({', '.join(col_defs)})"
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as e:
        error_exit(f"SQL error: {e}")

    print(json.dumps({
        "event": "operation_applied",
        "type": "create_table",
        "table": table_name,
        "version": version
    }))


def apply_add_column(conn, op, version):
    """Apply an add_column operation."""
    table_name = op["table"]
    col = op["column"]
    col_name = col["name"]

    if not table_exists(conn, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    if column_exists(conn, table_name, col_name):
        error_exit(f"invalid operation: column '{col_name}' already exists in table '{table_name}'")

    col_sql = build_column_sql(col)
    sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {col_sql}"
    try:
        conn.execute(sql)
    except sqlite3.OperationalError as e:
        error_exit(f"SQL error: {e}")

    print(json.dumps({
        "event": "operation_applied",
        "type": "add_column",
        "table": table_name,
        "column": col_name,
        "version": version
    }))


def apply_drop_column(conn, op, version):
    """Apply a drop_column operation by recreating the table."""
    table_name = op["table"]
    col_name = op["column"]

    if not table_exists(conn, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    columns = get_table_info(conn, table_name)
    col_names = [c["name"] for c in columns]

    if col_name not in col_names:
        error_exit(f"invalid operation: column '{col_name}' does not exist in table '{table_name}'")

    # Cannot drop if it's the only column
    if len(columns) <= 1:
        error_exit(f"invalid operation: cannot drop the only column in table '{table_name}'")

    # Cannot drop a PRIMARY KEY column
    col_info = next(c for c in columns if c["name"] == col_name)
    if col_info["primary_key"]:
        error_exit(f"invalid operation: cannot drop PRIMARY KEY column '{col_name}'")

    # Get columns to keep
    keep_columns = [c for c in columns if c["name"] != col_name]
    keep_names = [c["name"] for c in keep_columns]

    # Build column definitions for new table
    col_defs = []
    for c in keep_columns:
        parts = [quote_identifier(c["name"]), c["type"]]
        if c["primary_key"]:
            parts.append("PRIMARY KEY")
        if c["not_null"]:
            parts.append("NOT NULL")
        if c["default_value"] is not None:
            parts.append(f"DEFAULT {c['default_value']}")
        col_defs.append(" ".join(parts))

    temp_table_name = f"_temp_migration_{table_name}"

    try:
        # Create new table
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(col_defs)})"
        conn.execute(create_sql)

        # Copy data
        cols_str = ", ".join(quote_identifier(c) for c in keep_names)
        copy_sql = (
            f"INSERT INTO {quote_identifier(temp_table_name)} ({cols_str}) "
            f"SELECT {cols_str} FROM {quote_identifier(table_name)}"
        )
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(
            f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}"
        )
    except sqlite3.OperationalError as e:
        error_exit(f"SQL error: {e}")

    print(json.dumps({
        "event": "operation_applied",
        "type": "drop_column",
        "table": table_name,
        "column": col_name,
        "version": version
    }))


def apply_migration(migration_file, db_file):
    """Apply a migration from a JSON file to a SQLite database."""
    # Check migration file exists
    if not os.path.exists(migration_file):
        error_exit(f"migration file not found: {migration_file}")

    # Read and parse migration file
    try:
        with open(migration_file, 'r') as f:
            migration = json.load(f)
    except json.JSONDecodeError as e:
        error_exit(f"invalid JSON in migration file: {e}")
    except IOError as e:
        error_exit(f"cannot read migration file: {e}")

    # Validate migration schema
    err = validate_migration(migration)
    if err:
        error_exit(f"invalid migration schema: {err}")

    version = migration["version"]
    description = migration["description"]
    operations = migration["operations"]

    # Connect to database
    try:
        conn = sqlite3.connect(db_file)
    except sqlite3.Error as e:
        error_exit(f"cannot open database: {e}")

    try:
        # Disable foreign keys during migration
        conn.execute("PRAGMA foreign_keys = OFF")

        # Initialize migrations tracking table
        init_migrations_table(conn)

        # Check if migration already applied
        if is_migration_applied(conn, version):
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            print(json.dumps({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            }))
            conn.close()
            return

        # Apply operations
        for op in operations:
            op_type = op["type"]
            if op_type == "create_table":
                apply_create_table(conn, op, version)
            elif op_type == "add_column":
                apply_add_column(conn, op, version)
            elif op_type == "drop_column":
                apply_drop_column(conn, op, version)

        # Record the migration
        record_migration(conn, version, description)
        conn.commit()

        print(json.dumps({
            "event": "migration_complete",
            "version": version,
            "operations_count": len(operations)
        }))

    except Exception as e:
        conn.rollback()
        conn.close()
        error_exit(str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Database Migration Tool")
    subparsers = parser.add_subparsers(dest="command")

    migrate_parser = subparsers.add_parser("migrate", help="Apply a migration")
    migrate_parser.add_argument("migration_file", help="Path to migration JSON file")
    migrate_parser.add_argument("database", help="Path to SQLite database file")

    args = parser.parse_args()

    if args.command == "migrate":
        apply_migration(args.migration_file, args.database)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
