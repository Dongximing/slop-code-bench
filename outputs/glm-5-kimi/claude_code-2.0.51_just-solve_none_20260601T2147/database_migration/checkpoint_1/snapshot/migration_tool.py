#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 1: Basic Schema Migrations

A CLI tool that applies database schema migrations to SQLite databases.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional


# Valid SQLite data types
VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}

# Regex for valid SQLite identifiers (alphanumeric + underscore, starting with letter/underscore)
IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def is_valid_identifier(name: str) -> bool:
    """Check if a string is a valid SQLite identifier."""
    return bool(IDENTIFIER_PATTERN.match(name))


def quote_identifier(name: str) -> str:
    """Quote an identifier to prevent SQL injection."""
    # Double any double quotes and wrap in double quotes
    return f'"{name.replace(chr(34), chr(34) + chr(34))}"'


def error_exit(message: str) -> None:
    """Print error message to stderr and exit with code 1."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def warning(message: str) -> None:
    """Print warning message to stderr."""
    print(f"Warning: {message}", file=sys.stderr)


def output_json(event: Dict[str, Any]) -> None:
    """Print a JSON line to stdout."""
    print(json.dumps(event, separators=(',', ':')))


def validate_column(column: Dict[str, Any], operation_type: str) -> None:
    """Validate a column definition."""
    if "name" not in column:
        error_exit(f"invalid migration schema: missing required field 'name' in column definition")
    if "type" not in column:
        error_exit(f"invalid migration schema: missing required field 'type' in column definition")

    if not isinstance(column["name"], str):
        error_exit("invalid migration schema: column 'name' must be a string")
    if not isinstance(column["type"], str):
        error_exit("invalid migration schema: column 'type' must be a string")

    if not is_valid_identifier(column["name"]):
        error_exit(f"invalid column name: '{column['name']}'")

    col_type = column["type"].upper()
    if col_type not in VALID_TYPES:
        error_exit(f"invalid column type: '{column['type']}'")

    # Validate optional fields
    if "primary_key" in column and not isinstance(column["primary_key"], bool):
        error_exit("invalid migration schema: 'primary_key' must be a boolean")
    if "auto_increment" in column and not isinstance(column["auto_increment"], bool):
        error_exit("invalid migration schema: 'auto_increment' must be a boolean")
    if "not_null" in column and not isinstance(column["not_null"], bool):
        error_exit("invalid migration schema: 'not_null' must be a boolean")
    if "unique" in column and not isinstance(column["unique"], bool):
        error_exit("invalid migration schema: 'unique' must be a boolean")
    if "default" in column and column["default"] is not None and not isinstance(column["default"], str):
        error_exit("invalid migration schema: 'default' must be a string or null")

    # Validate auto_increment constraints
    if column.get("auto_increment", False):
        if column["type"].upper() != "INTEGER":
            error_exit(f"invalid column definition: auto_increment can only be used with INTEGER type")
        if not column.get("primary_key", False):
            error_exit(f"invalid column definition: auto_increment requires primary_key to be true")


def validate_create_table(operation: Dict[str, Any]) -> None:
    """Validate a create_table operation."""
    if "table" not in operation:
        error_exit("invalid migration schema: missing required field 'table'")
    if "columns" not in operation:
        error_exit("invalid migration schema: missing required field 'columns'")

    if not isinstance(operation["table"], str):
        error_exit("invalid migration schema: 'table' must be a string")
    if not isinstance(operation["columns"], list):
        error_exit("invalid migration schema: 'columns' must be an array")

    if not is_valid_identifier(operation["table"]):
        error_exit(f"invalid table name: '{operation['table']}'")

    if len(operation["columns"]) == 0:
        error_exit("invalid migration schema: 'columns' array cannot be empty")

    # Count primary keys
    pk_count = 0
    for col in operation["columns"]:
        validate_column(col, "create_table")
        if col.get("primary_key", False):
            pk_count += 1

    if pk_count > 1:
        error_exit("invalid table definition: at most one column can have primary_key: true")


def validate_add_column(operation: Dict[str, Any]) -> None:
    """Validate an add_column operation."""
    if "table" not in operation:
        error_exit("invalid migration schema: missing required field 'table'")
    if "column" not in operation:
        error_exit("invalid migration schema: missing required field 'column'")

    if not isinstance(operation["table"], str):
        error_exit("invalid migration schema: 'table' must be a string")
    if not isinstance(operation["column"], dict):
        error_exit("invalid migration schema: 'column' must be an object")

    if not is_valid_identifier(operation["table"]):
        error_exit(f"invalid table name: '{operation['table']}'")

    validate_column(operation["column"], "add_column")


def validate_drop_column(operation: Dict[str, Any]) -> None:
    """Validate a drop_column operation."""
    if "table" not in operation:
        error_exit("invalid migration schema: missing required field 'table'")
    if "column" not in operation:
        error_exit("invalid migration schema: missing required field 'column'")

    if not isinstance(operation["table"], str):
        error_exit("invalid migration schema: 'table' must be a string")
    if not isinstance(operation["column"], str):
        error_exit("invalid migration schema: 'column' must be a string")

    if not is_valid_identifier(operation["table"]):
        error_exit(f"invalid table name: '{operation['table']}'")
    if not is_valid_identifier(operation["column"]):
        error_exit(f"invalid column name: '{operation['column']}'")


def validate_operation(operation: Dict[str, Any]) -> None:
    """Validate a single operation."""
    if "type" not in operation:
        error_exit("invalid migration schema: missing required field 'type'")

    if not isinstance(operation["type"], str):
        error_exit("invalid migration schema: 'type' must be a string")

    op_type = operation["type"]

    if op_type == "create_table":
        validate_create_table(operation)
    elif op_type == "add_column":
        validate_add_column(operation)
    elif op_type == "drop_column":
        validate_drop_column(operation)
    else:
        error_exit(f"invalid operation type: '{op_type}'")


def validate_migration(migration: Dict[str, Any]) -> None:
    """Validate the migration file structure."""
    if "version" not in migration:
        error_exit("invalid migration schema: missing required field 'version'")
    if "description" not in migration:
        error_exit("invalid migration schema: missing required field 'description'")
    if "operations" not in migration:
        error_exit("invalid migration schema: missing required field 'operations'")

    if not isinstance(migration["version"], int):
        error_exit("invalid migration schema: 'version' must be an integer")
    if not isinstance(migration["description"], str):
        error_exit("invalid migration schema: 'description' must be a string")
    if not isinstance(migration["operations"], list):
        error_exit("invalid migration schema: 'operations' must be an array")

    if migration["version"] <= 0:
        error_exit("invalid migration schema: 'version' must be a positive integer")

    if len(migration["operations"]) == 0:
        error_exit("invalid migration schema: 'operations' array cannot be empty")

    for op in migration["operations"]:
        validate_operation(op)


def build_column_sql(column: Dict[str, Any]) -> str:
    """Build SQL for a column definition."""
    parts = [quote_identifier(column["name"]), column["type"].upper()]

    if column.get("primary_key", False):
        parts.append("PRIMARY KEY")

    if column.get("auto_increment", False):
        parts.append("AUTOINCREMENT")

    if column.get("not_null", False):
        parts.append("NOT NULL")

    if column.get("unique", False):
        parts.append("UNIQUE")

    if "default" in column and column["default"] is not None:
        parts.append(f"DEFAULT {column['default']}")

    return " ".join(parts)


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check if a table exists in the database."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    """Get the list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return [row[1] for row in cursor.fetchall()]


def get_column_info(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    """Get detailed column information for a table."""
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    columns = []
    for row in cursor.fetchall():
        columns.append({
            "cid": row[0],
            "name": row[1],
            "type": row[2],
            "notnull": row[3],
            "dflt_value": row[4],
            "pk": row[5]
        })
    return columns


def is_primary_key_column(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> bool:
    """Check if a column is the primary key of its table."""
    columns = get_column_info(cursor, table_name)
    for col in columns:
        if col["name"] == column_name and col["pk"] > 0:
            return True
    return False


def execute_create_table(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute a create_table operation."""
    table_name = operation["table"]

    if table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' already exists")

    columns_sql = ", ".join(build_column_sql(col) for col in operation["columns"])
    sql = f"CREATE TABLE {quote_identifier(table_name)} ({columns_sql})"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "create_table",
        "table": table_name,
        "version": version
    })


def execute_add_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute an add_column operation."""
    table_name = operation["table"]
    column = operation["column"]
    column_name = column["name"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    if column_name in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' already exists in table '{table_name}'")

    column_sql = build_column_sql(column)
    sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {column_sql}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "add_column",
        "table": table_name,
        "column": column_name,
        "version": version
    })


def execute_drop_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute a drop_column operation."""
    table_name = operation["table"]
    column_name = operation["column"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    if column_name not in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

    if len(existing_columns) == 1:
        error_exit(f"invalid operation: cannot drop the only column in table '{table_name}'")

    if is_primary_key_column(cursor, table_name, column_name):
        error_exit(f"invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    # Get columns to keep
    columns_to_keep = [col for col in existing_columns if col != column_name]

    # Get column info for preserving structure
    column_info = get_column_info(cursor, table_name)

    # Create new table name (temporary)
    new_table_name = f"_migration_temp_{table_name}"

    # Build column definitions for new table
    new_columns_sql_parts = []
    for col_name in columns_to_keep:
        for col in column_info:
            if col["name"] == col_name:
                col_def = f"{quote_identifier(col['name'])} {col['type']}"
                if col['pk'] > 0:
                    col_def += " PRIMARY KEY"
                if col['notnull']:
                    col_def += " NOT NULL"
                if col['dflt_value'] is not None:
                    col_def += f" DEFAULT {col['dflt_value']}"
                new_columns_sql_parts.append(col_def)
                break

    new_columns_sql = ", ".join(new_columns_sql_parts)

    try:
        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Create new table
        create_sql = f"CREATE TABLE {quote_identifier(new_table_name)} ({new_columns_sql})"
        cursor.execute(create_sql)

        # Copy data
        columns_select = ", ".join(quote_identifier(col) for col in columns_to_keep)
        copy_sql = f"INSERT INTO {quote_identifier(new_table_name)} SELECT {columns_select} FROM {quote_identifier(table_name)}"
        cursor.execute(copy_sql)

        # Drop old table
        drop_sql = f"DROP TABLE {quote_identifier(table_name)}"
        cursor.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {quote_identifier(new_table_name)} RENAME TO {quote_identifier(table_name)}"
        cursor.execute(rename_sql)

        # Re-enable foreign keys
        cursor.execute("PRAGMA foreign_keys = ON")

    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "drop_column",
        "table": table_name,
        "column": column_name,
        "version": version
    })


def execute_operation(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute a single operation."""
    op_type = operation["type"]

    if op_type == "create_table":
        execute_create_table(cursor, operation, version)
    elif op_type == "add_column":
        execute_add_column(cursor, operation, version)
    elif op_type == "drop_column":
        execute_drop_column(cursor, operation, version)


def init_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Initialize the _migrations table if it doesn't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str) -> None:
    """Record a successful migration in the _migrations table."""
    cursor.execute(
        "INSERT INTO _migrations (version, description) VALUES (?, ?)",
        (version, description)
    )


def run_migration(migration_file: str, db_file: str) -> None:
    """Run a migration from a file."""
    # Check if migration file exists
    if not os.path.exists(migration_file):
        error_exit(f"migration file not found: {migration_file}")

    # Read and parse migration file
    try:
        with open(migration_file, 'r') as f:
            migration = json.load(f)
    except json.JSONDecodeError as e:
        error_exit(f"invalid JSON in migration file: {e}")
    except IOError as e:
        error_exit(f"error reading migration file: {e}")

    # Validate migration structure
    validate_migration(migration)

    version = migration["version"]
    description = migration["description"]
    operations = migration["operations"]

    # Connect to database (creates file if it doesn't exist)
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        error_exit(f"error connecting to database: {e}")

    try:
        # Initialize migrations table
        init_migrations_table(cursor)

        # Check if migration already applied
        if is_migration_applied(cursor, version):
            warning(f"Migration version {version} already applied, skipping")
            output_json({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            })
            conn.close()
            return

        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Execute operations
        for operation in operations:
            execute_operation(cursor, operation, version)

        # Record migration
        record_migration(cursor, version, description)

        # Re-enable foreign keys
        cursor.execute("PRAGMA foreign_keys = ON")

        # Commit transaction
        conn.commit()

        output_json({
            "event": "migration_complete",
            "version": version,
            "operations_count": len(operations)
        })

    except sqlite3.Error as e:
        conn.rollback()
        error_exit(f"SQL error: {e}")
    finally:
        conn.close()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Database Migration Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # migrate command
    migrate_parser = subparsers.add_parser("migrate", help="Apply a migration")
    migrate_parser.add_argument("migration_file", help="Path to the migration JSON file")
    migrate_parser.add_argument("database", help="Path to the SQLite database file")

    args = parser.parse_args()

    if args.command == "migrate":
        run_migration(args.migration_file, args.database)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
