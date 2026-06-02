#!/usr/bin/env python3
"""Database Migration Tool for SQLite - Basic Schema Migrations"""

import argparse
import json
import re
import sqlite3
import sys
from typing import Any


class MigrationError(Exception):
    pass


def validate_identifier(name: str) -> bool:
    if not name:
        return False
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def escape_identifier(name: str) -> str:
    if not validate_identifier(name):
        raise MigrationError(f"Invalid identifier: '{name}'")
    return f'"{name}"'


def _get_table_info(cursor: sqlite3.Cursor, table: str) -> list[tuple]:
    """Fetch PRAGMA table_info for a table as list of rows."""
    cursor.execute(f"PRAGMA table_info({escape_identifier(table)})")
    return cursor.fetchall()


def get_table_columns(cursor: sqlite3.Cursor, table: str) -> list[str]:
    return [row[1] for row in _get_table_info(cursor, table)]


def get_primary_key_column(cursor: sqlite3.Cursor, table: str) -> str | None:
    for row in _get_table_info(cursor, table):
        if row[5]:
            return row[1]
    return None


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    if not table_exists(cursor, '_migrations'):
        return False
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def create_migrations_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


VALID_COLUMN_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}


def validate_column_definition(col: dict[str, Any]) -> None:
    if 'name' not in col:
        raise MigrationError("Column definition missing required field 'name'")
    if 'type' not in col:
        raise MigrationError(f"Column '{col.get('name', 'unknown')}' missing required field 'type'")

    name = col['name']
    if not validate_identifier(name):
        raise MigrationError(f"Invalid column name: '{name}'")

    col_type = col['type'].upper()
    if col_type not in VALID_COLUMN_TYPES:
        raise MigrationError(f"Invalid column type: '{col_type}'. Must be one of {VALID_COLUMN_TYPES}")

    if col.get('auto_increment') and col_type != 'INTEGER':
        raise MigrationError(f"auto_increment can only be used with INTEGER columns, got '{col_type}'")
    if col.get('auto_increment') and not col.get('primary_key'):
        raise MigrationError("auto_increment can only be used with primary key columns")


def build_column_definition(col: dict[str, Any]) -> str:
    parts = [escape_identifier(col['name']), col['type'].upper()]

    if col.get('primary_key'):
        parts.append('PRIMARY KEY')
        if col.get('auto_increment'):
            parts.append('AUTOINCREMENT')

    if col.get('not_null'):
        parts.append('NOT NULL')

    if col.get('unique'):
        parts.append('UNIQUE')

    default = col.get('default')
    if default is not None:
        parts.append(f"DEFAULT {default}")

    return ' '.join(parts)


def apply_create_table(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    columns = op.get('columns', [])

    if not table:
        raise MigrationError("create_table operation missing required field 'table'")
    if not columns:
        raise MigrationError(f"create_table operation for table '{table}' has no columns")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")

    if table_exists(cursor, table):
        raise MigrationError(f"table '{table}' already exists")

    primary_key_count = 0
    for col in columns:
        validate_column_definition(col)
        if col.get('primary_key'):
            primary_key_count += 1

    if primary_key_count > 1:
        raise MigrationError("Only one column can be marked as primary_key")

    # Build and execute CREATE TABLE statement
    column_defs = [build_column_definition(col) for col in columns]
    sql = f"CREATE TABLE {escape_identifier(table)} ({', '.join(column_defs)})"

    cursor.execute(sql)
    emit_event("operation_applied", type="create_table", table=table, version=version)


def apply_add_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("add_column operation missing required 'table' or 'column'")
    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    validate_column_definition(column)

    existing_columns = get_table_columns(cursor, table)
    if column['name'] in existing_columns:
        raise MigrationError(f"column '{column['name']}' already exists in table '{table}'")

    if column.get('primary_key') and get_primary_key_column(cursor, table):
        raise MigrationError(f"Table '{table}' already has a primary key column")

    # Build and execute ALTER TABLE ADD COLUMN statement
    column_def = build_column_definition(column)
    sql = f"ALTER TABLE {escape_identifier(table)} ADD COLUMN {column_def}"
    cursor.execute(sql)
    emit_event("operation_applied", type="add_column", table=table, column=column['name'], version=version)


def _rebuild_table_without_column(cursor: sqlite3.Cursor, table: str, column: str) -> None:
    """Rebuild table excluding a column using temp table swap."""
    temp_table = f"__temp_{table}"
    columns_to_keep = get_table_columns(cursor, table)
    columns_to_keep = [col for col in columns_to_keep if col != column]

    column_info = {row[1]: row for row in _get_table_info(cursor, table)}

    new_defs = []
    for col_name in columns_to_keep:
        info = column_info[col_name]
        parts = [escape_identifier(info[1]), info[2]]
        if info[3]:
            parts.append('NOT NULL')
        if info[5]:
            parts.append('PRIMARY KEY')
            if info[4] == 1:
                parts.append('AUTOINCREMENT')
        new_defs.append(' '.join(parts))

    cursor.execute(f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(new_defs)})")
    select = ', '.join([escape_identifier(col) for col in columns_to_keep])
    cursor.execute(f"INSERT INTO {escape_identifier(temp_table)} SELECT {select} FROM {escape_identifier(table)}")
    cursor.execute(f"DROP TABLE {escape_identifier(table)}")
    cursor.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(table)}")


def apply_drop_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("drop_column operation missing required 'table' or 'column'")
    if not validate_identifier(table) or not validate_identifier(column):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing_columns = get_table_columns(cursor, table)
    if column not in existing_columns:
        raise MigrationError(f"column '{column}' does not exist in table '{table}'")

    if len(existing_columns) <= 1:
        raise MigrationError(f"Cannot drop column '{column}' from table '{table}'")

    pk_column = get_primary_key_column(cursor, table)
    if column == pk_column:
        raise MigrationError(f"Cannot drop primary key column '{column}' from table '{table}'")

    _rebuild_table_without_column(cursor, table, column)
    emit_event("operation_applied", type="drop_column", table=table, column=column, version=version)


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

        create_migrations_table(cursor)

        if migration_applied(cursor, version):
            emit_event("migration_skipped", version=version, reason="already_applied")
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            conn.close()
            return 0

        operation_count = 0
        for op in operations:
            try:
                OPERATION_HANDLERS[op['type']](cursor, op, version)
            except KeyError:
                raise MigrationError(f"Unknown operation type: '{op['type']}'")
            operation_count += 1

        cursor.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )
        conn.commit()
        conn.close()
        emit_event("migration_complete", version=version, operations_count=operation_count)
        return 0

    except (MigrationError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def load_migration_file(file_path: str) -> dict[str, Any]:
    try:
        with open(file_path) as f:
            migration = json.load(f)
    except FileNotFoundError:
        raise MigrationError(f"migration file not found: {file_path}")
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")

    if not isinstance(migration, dict):
        raise MigrationError("Migration file must contain a JSON object")
    validate_migration_file(migration)
    return migration


OPERATION_HANDLERS = {
    'create_table': apply_create_table,
    'add_column': apply_add_column,
    'drop_column': apply_drop_column,
}


def emit_event(event_type: str, **data) -> None:
    """Output a JSON event."""
    print(json.dumps({**data, "event": event_type}))


def main():
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')
    subparsers = parser.add_subparsers(dest='command', required=True)
    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()
    if args.command != 'migrate':
        return 1

    try:
        migration = load_migration_file(args.migration_file)
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return apply_migration(migration, args.database)


if __name__ == '__main__':
    sys.exit(main())
