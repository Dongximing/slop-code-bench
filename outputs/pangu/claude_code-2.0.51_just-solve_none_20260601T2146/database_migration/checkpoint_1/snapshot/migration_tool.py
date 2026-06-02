#!/usr/bin/env python3
"""Database Migration Tool for SQLite - Basic Schema Migrations"""

import argparse
import json
import re
import sqlite3
import sys
from typing import Any


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


def validate_identifier(name: str) -> bool:
    """Validate that a name is a valid SQLite identifier."""
    if not name:
        return False
    # SQLite identifiers: alphanumeric + underscore, starting with letter/underscore
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def escape_identifier(name: str) -> str:
    """Escape an identifier by wrapping in double quotes."""
    if not validate_identifier(name):
        raise MigrationError(f"Invalid identifier: '{name}'")
    return f'"{name}"'


def get_table_columns(cursor: sqlite3.Cursor, table: str) -> list[str]:
    """Get list of column names for a table."""
    cursor.execute(f"PRAGMA table_info({escape_identifier(table)})")
    return [row[1] for row in cursor.fetchall()]


def get_primary_key_column(cursor: sqlite3.Cursor, table: str) -> str | None:
    """Get the name of the primary key column, if any."""
    cursor.execute(f"PRAGMA table_info({escape_identifier(table)})")
    for row in cursor.fetchall():
        if row[5]:  # pk column (1 if primary key)
            return row[1]
    return None


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    """Check if a table exists in the database."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    )
    return cursor.fetchone() is not None


def migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    if not table_exists(cursor, '_migrations'):
        return False
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def create_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Create the _migrations table if it doesn't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def validate_column_definition(col: dict[str, Any], operation_type: str) -> None:
    """Validate a column definition."""
    if 'name' not in col:
        raise MigrationError(f"Column definition missing required field 'name'")
    if 'type' not in col:
        raise MigrationError(f"Column '{col.get('name', 'unknown')}' missing required field 'type'")

    column_name = col['name']
    column_type = col['type'].upper()

    if not validate_identifier(column_name):
        raise MigrationError(f"Invalid column name: '{column_name}'")

    valid_types = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}
    if column_type not in valid_types:
        raise MigrationError(f"Invalid column type: '{column_type}'. Must be one of {valid_types}")

    # Check auto_increment constraints
    if col.get('auto_increment', False):
        if column_type != 'INTEGER':
            raise MigrationError(f"auto_increment can only be used with INTEGER columns, got '{column_type}'")
        if not col.get('primary_key', False):
            raise MigrationError(f"auto_increment can only be used with primary key columns")


def build_column_definition(col: dict[str, Any]) -> str:
    """Build the SQL column definition string from a column dict."""
    name = escape_identifier(col['name'])
    type_name = col['type'].upper()

    parts = [name, type_name]

    if col.get('primary_key', False):
        parts.append('PRIMARY KEY')
        if col.get('auto_increment', False):
            parts.append('AUTOINCREMENT')

    if col.get('not_null', False):
        parts.append('NOT NULL')

    if col.get('unique', False):
        parts.append('UNIQUE')

    if col.get('default') is not None:
        parts.append(f"DEFAULT {col['default']}")

    return ' '.join(parts)


def apply_create_table(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Apply a create_table operation."""
    table = op.get('table')
    columns = op.get('columns', [])

    if not table:
        raise MigrationError("create_table operation missing required field 'table'")
    if not columns:
        raise MigrationError(f"create_table operation for table '{table}' has no columns")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")

    # Check if table already exists
    if table_exists(cursor, table):
        raise MigrationError(f"table '{table}' already exists")

    # Validate column definitions and check for duplicate primary key
    primary_key_count = 0
    for col in columns:
        validate_column_definition(col, 'create_table')
        if col.get('primary_key', False):
            primary_key_count += 1

    if primary_key_count > 1:
        raise MigrationError("Only one column can be marked as primary_key")

    # Build and execute CREATE TABLE statement
    column_defs = [build_column_definition(col) for col in columns]
    sql = f"CREATE TABLE {escape_identifier(table)} ({', '.join(column_defs)})"

    cursor.execute(sql)

    # Output event
    event = {
        "event": "operation_applied",
        "type": "create_table",
        "table": table,
        "version": version
    }
    print(json.dumps(event))


def apply_add_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Apply an add_column operation."""
    table = op.get('table')
    column = op.get('column')

    if not table:
        raise MigrationError("add_column operation missing required field 'table'")
    if not column:
        raise MigrationError("add_column operation missing required field 'column'")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    # Validate column definition
    validate_column_definition(column, 'add_column')

    # Check if column already exists
    existing_columns = get_table_columns(cursor, table)
    if column['name'] in existing_columns:
        raise MigrationError(f"column '{column['name']}' already exists in table '{table}'")

    # Check for duplicate primary key (if adding a primary key to non-empty table)
    existing_pk = get_primary_key_column(cursor, table)
    if column.get('primary_key', False) and existing_pk:
        raise MigrationError(f"Table '{table}' already has a primary key column '{existing_pk}'")

    # Build and execute ALTER TABLE ADD COLUMN statement
    column_def = build_column_definition(column)
    sql = f"ALTER TABLE {escape_identifier(table)} ADD COLUMN {column_def}"
    cursor.execute(sql)

    # Output event
    event = {
        "event": "operation_applied",
        "type": "add_column",
        "table": table,
        "column": column['name'],
        "version": version
    }
    print(json.dumps(event))


def apply_drop_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Apply a drop_column operation."""
    table = op.get('table')
    column = op.get('column')

    if not table:
        raise MigrationError("drop_column operation missing required field 'table'")
    if not column:
        raise MigrationError("drop_column operation missing required field 'column'")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")
    if not validate_identifier(column):
        raise MigrationError(f"Invalid column name: '{column}'")

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    # Check if column exists
    existing_columns = get_table_columns(cursor, table)
    if column not in existing_columns:
        raise MigrationError(f"column '{column}' does not exist in table '{table}'")

    # Check if this is the only column
    if len(existing_columns) <= 1:
        raise MigrationError(f"Cannot drop column '{column}' from table '{table}' - it is the only column")

    # Check if it's a primary key
    pk_column = get_primary_key_column(cursor, table)
    if column == pk_column:
        raise MigrationError(f"Cannot drop primary key column '{column}' from table '{table}'")

    # Get all columns except the one to drop
    columns_to_keep = [col for col in existing_columns if col != column]

    # Create temporary table name
    temp_table = f"__temp_{table}"

    # Build CREATE TABLE statement for new table without the dropped column
    # Get column info for the table to preserve constraints
    cursor.execute(f"PRAGMA table_info({escape_identifier(table)})")
    column_info = {row[1]: row for row in cursor.fetchall()}

    new_column_defs = []
    for col_name in columns_to_keep:
        info = column_info[col_name]
        col_def_parts = [escape_identifier(info[1]), info[2]]  # name, type

        if info[3]:  # notnull (1 = NOT NULL)
            col_def_parts.append('NOT NULL')
        if info[5]:  # pk (1 if primary key)
            col_def_parts.append('PRIMARY KEY')
            # Check for AUTOINCREMENT - if column has default value of 1 (AUTOINCREMENT indicator)
            if info[4] == 1:  # SQLite indicates AUTOINCREMENT with default_value = 1
                col_def_parts.append('AUTOINCREMENT')

        new_column_defs.append(' '.join(col_def_parts))

    # Copy any unique constraints/foreign keys as comments (simplified approach)
    # Create new table
    create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(new_column_defs)})"
    cursor.execute(create_sql)

    # Copy data - select all columns except the dropped one
    columns_select = ', '.join([escape_identifier(col) for col in columns_to_keep])
    insert_sql = f"INSERT INTO {escape_identifier(temp_table)} SELECT {columns_select} FROM {escape_identifier(table)}"
    cursor.execute(insert_sql)

    # Drop old table
    cursor.execute(f"DROP TABLE {escape_identifier(table)}")

    # Rename new table to original name
    cursor.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(table)}")

    # Output event
    event = {
        "event": "operation_applied",
        "type": "drop_column",
        "table": table,
        "column": column,
        "version": version
    }
    print(json.dumps(event))


def validate_migration_file(migration: dict[str, Any]) -> None:
    """Validate the migration file structure."""
    if 'version' not in migration:
        raise MigrationError("Missing required field 'version'")
    if 'description' not in migration:
        raise MigrationError("Missing required field 'description'")
    if 'operations' not in migration:
        raise MigrationError("Missing required field 'operations'")

    version = migration['version']
    if not isinstance(version, int) or version < 1:
        raise MigrationError("Version must be a positive integer")

    if not isinstance(migration['description'], str):
        raise MigrationError("Description must be a string")

    if not isinstance(migration['operations'], list):
        raise MigrationError("Operations must be a list")

    valid_operations = {'create_table', 'add_column', 'drop_column'}
    for i, op in enumerate(migration['operations']):
        if not isinstance(op, dict):
            raise MigrationError(f"Operation {i} must be an object")
        if 'type' not in op:
            raise MigrationError(f"Operation {i} missing required field 'type'")
        if op['type'] not in valid_operations:
            raise MigrationError(f"Operation {i} has invalid type '{op['type']}'. Must be one of {valid_operations}")


def apply_migration(migration: dict[str, Any], db_path: str) -> int:
    """Apply a migration to the database. Returns exit code."""
    version = migration['version']
    description = migration['description']
    operations = migration['operations']

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        cursor = conn.cursor()

        # Create _migrations table if it doesn't exist
        create_migrations_table(cursor)

        # Check if migration already applied
        if migration_applied(cursor, version):
            print(json.dumps({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            }))
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            conn.close()
            return 0

        # Apply operations in order
        operation_count = 0
        for op in operations:
            op_type = op['type']
            try:
                if op_type == 'create_table':
                    apply_create_table(cursor, op, version)
                elif op_type == 'add_column':
                    apply_add_column(cursor, op, version)
                elif op_type == 'drop_column':
                    apply_drop_column(cursor, op, version)
                operation_count += 1
            except MigrationError as e:
                conn.rollback()
                conn.close()
                raise e
            except sqlite3.Error as e:
                conn.rollback()
                conn.close()
                raise MigrationError(f"SQL error: {str(e)}") from e

        # Record successful migration
        cursor.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )

        conn.commit()
        conn.close()

        # Output completion event
        print(json.dumps({
            "event": "migration_complete",
            "version": version,
            "operations_count": operation_count
        }))

        return 0

    except (MigrationError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def load_migration_file(file_path: str) -> dict[str, Any]:
    """Load and parse a migration file."""
    try:
        with open(file_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        raise MigrationError(f"migration file not found: {file_path}")
    except IOError as e:
        raise MigrationError(f"Error reading migration file: {e}")

    try:
        migration = json.loads(content)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")

    if not isinstance(migration, dict):
        raise MigrationError("Migration file must contain a JSON object")

    validate_migration_file(migration)

    return migration


def main():
    parser = argparse.ArgumentParser(
        description='Database Migration Tool for SQLite'
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()

    if args.command == 'migrate':
        try:
            migration = load_migration_file(args.migration_file)
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

        return apply_migration(migration, args.database)

    return 1


if __name__ == '__main__':
    sys.exit(main())
