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
    """Check if identifier matches SQL identifier pattern."""
    return bool(name and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def _table_info(cursor: sqlite3.Cursor, table: str) -> list[tuple]:
    """Return PRAGMA table_info rows for a table."""
    cursor.execute(f'PRAGMA table_info("{table}")')
    return cursor.fetchall()


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


def validate_column(col: dict[str, Any]) -> None:
    """Validate column definition, raising MigrationError on any issue."""
    name = col.get('name')
    if not name:
        raise MigrationError("Column definition missing required field 'name'")

    col_type = col.get('type')
    if not col_type:
        raise MigrationError(f"Column '{name}' missing required field 'type'")

    if not validate_identifier(name):
        raise MigrationError(f"Invalid column name: '{name}'")

    upper_type = col_type.upper()
    if upper_type not in VALID_COLUMN_TYPES:
        raise MigrationError(f"Invalid column type: '{upper_type}'. Must be one of {VALID_COLUMN_TYPES}")

    if col.get('auto_increment'):
        if upper_type != 'INTEGER':
            raise MigrationError(f"auto_increment can only be used with INTEGER columns, got '{upper_type}'")
        if not col.get('primary_key'):
            raise MigrationError("auto_increment can only be used with primary key columns")


def build_column(col: dict[str, Any]) -> str:
    """Build a column definition fragment from a column spec."""
    parts = [f'"{col["name"]}"', col['type'].upper()]

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
        parts.append(f'DEFAULT {default}')

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

    pk_count = 0
    for col in columns:
        validate_column(col)
        if col.get('primary_key'):
            pk_count += 1

    if pk_count > 1:
        raise MigrationError("Only one column can be marked as primary_key")

    cursor.execute(f'CREATE TABLE "{table}"({", ".join(build_column(c) for c in columns)})')
    print(json.dumps({"event": "operation_applied", "type": "create_table", "table": table, "version": version}))


def apply_add_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("add_column operation missing required 'table' or 'column'")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    validate_column(column)

    info = _table_info(cursor, table)
    existing = [row[1] for row in info]

    name = column['name']
    if name in existing:
        raise MigrationError(f"column '{name}' already exists in table '{table}'")

    if column.get('primary_key'):
        if any(row[5] for row in info):
            raise MigrationError(f"Table '{table}' already has a primary key column")

    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN {build_column(column)}')
    print(json.dumps({"event": "operation_applied", "type": "add_column", "table": table, "column": name, "version": version}))


def apply_drop_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("drop_column operation missing required 'table' or 'column'")

    if not validate_identifier(table) or not validate_identifier(column):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    info = _table_info(cursor, table)
    existing = [row[1] for row in info]

    if column not in existing:
        raise MigrationError(f"column '{column}' does not exist in table '{table}'")
    if len(existing) <= 1:
        raise MigrationError(f"Cannot drop column '{column}' from table '{table}'")

    if any(row[5] and row[1] == column for row in info):
        raise MigrationError(f"Cannot drop primary key column '{column}' from table '{table}'")

    # Rebuild table without the column using temp table swap
    temp = f'"__temp_{table}"'
    keep = [c for c in existing if c != column]

    col_map = {row[1]: row for row in info}

    new_defs = []
    for cn in keep:
        r = col_map[cn]
        p = [f'"{r[1]}"', r[2]]
        if r[3]:
            p.append('NOT NULL')
        if r[5]:
            p.append('PRIMARY KEY')
            if r[4] == 1:
                p.append('AUTOINCREMENT')
        new_defs.append(' '.join(p))

    cols_joined = ', '.join(f'"{c}"' for c in keep)
    cursor.execute(f'CREATE TABLE {temp}({", ".join(new_defs)})')
    cursor.execute(f'INSERT INTO {temp} SELECT {cols_joined} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')
    print(json.dumps({"event": "operation_applied", "type": "drop_column", "table": table, "column": column, "version": version}))


def apply_transform_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    transformations = op.get('transformations', [])

    if not table:
        raise MigrationError("transform_data operation missing required field 'table'")
    if not transformations:
        raise MigrationError("transform_data operation must have at least one transformation")

    if not validate_identifier(table):
        raise MigrationError(f"Invalid table name: '{table}'")
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]

    for i, t in enumerate(transformations):
        col = t.get('column')
        expr = t.get('expression')
        if not col or not expr:
            raise MigrationError(f"Transformation {i} missing required field 'column' or 'expression'")
        if not validate_identifier(col):
            raise MigrationError(f"Invalid column name: '{col}'")
        if col not in existing:
            raise MigrationError(f"column '{col}' does not exist in table '{table}'")

    for t in transformations:
        cursor.execute(f'UPDATE "{table}" SET "{t["column"]}" = ({t["expression"]})')

    print(json.dumps({"event": "operation_applied", "type": "transform_data", "table": table, "transformations_count": len(transformations), "version": version}))


def apply_migrate_column_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    from_col = op.get('from_column')
    to_col = op.get('to_column')
    default = op.get('default_value')

    if not table or not from_col or not to_col:
        raise MigrationError("migrate_column_data operation missing required 'table', 'from_column', or 'to_column'")

    if not (validate_identifier(table) and validate_identifier(from_col) and validate_identifier(to_col)):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]

    if from_col not in existing:
        raise MigrationError(f"column '{from_col}' does not exist in table '{table}'")
    if to_col not in existing:
        raise MigrationError(f"column '{to_col}' does not exist in table '{table}'")

    if default is not None:
        sql = f'UPDATE "{table}" SET "{to_col}" = COALESCE("{from_col}", {default})'
    else:
        sql = f'UPDATE "{table}" SET "{to_col}" = "{from_col}"'

    cursor.execute(sql)
    print(json.dumps({"event": "operation_applied", "type": "migrate_column_data", "table": table, "from_column": from_col, "to_column": to_col, "version": version}))


def apply_backfill_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')
    value = op.get('value')
    where = op.get('where')

    if not table or not column or value is None:
        raise MigrationError("backfill_data operation missing required 'table', 'column', or 'value'")

    if not (validate_identifier(table) and validate_identifier(column)):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]
    if column not in existing:
        raise MigrationError(f"column '{column}' does not exist in table '{table}'")

    sql = f'UPDATE "{table}" SET "{column}" = {value}'
    if where:
        sql += f' WHERE {where}'

    cursor.execute(sql)
    print(json.dumps({"event": "operation_applied", "type": "backfill_data", "table": table, "column": column, "version": version}))


OPERATION_HANDLERS = {
    'create_table': apply_create_table,
    'add_column': apply_add_column,
    'drop_column': apply_drop_column,
    'transform_data': apply_transform_data,
    'migrate_column_data': apply_migrate_column_data,
    'backfill_data': apply_backfill_data,
}


def validate_migration_file(migration: dict[str, Any]) -> None:
    """Validate the migration file structure."""
    if 'version' not in migration:
        raise MigrationError("Missing required field 'version'")
    if 'description' not in migration:
        raise MigrationError("Missing required field 'description'")
    if 'operations' not in migration:
        raise MigrationError("Missing required field 'operations'")

    ver = migration['version']
    if not isinstance(ver, int) or ver < 1:
        raise MigrationError("Version must be a positive integer")
    if not isinstance(migration['description'], str):
        raise MigrationError("Description must be a string")
    if not isinstance(migration['operations'], list):
        raise MigrationError("Operations must be a list")

    valid_ops = {'create_table', 'add_column', 'drop_column', 'transform_data', 'migrate_column_data', 'backfill_data'}
    for i, op in enumerate(migration['operations']):
        if not isinstance(op, dict):
            raise MigrationError(f"Operation {i} must be an object")
        if 'type' not in op:
            raise MigrationError(f"Operation {i} missing required field 'type'")
        if op['type'] not in valid_ops:
            raise MigrationError(f"Operation {i} has invalid type '{op['type']}'")


def load_migration_file(path: str) -> dict[str, Any]:
    try:
        with open(path) as f:
            m = json.load(f)
    except FileNotFoundError:
        raise MigrationError(f"migration file not found: {path}")
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")

    if not isinstance(m, dict):
        raise MigrationError("Migration file must contain a JSON object")
    validate_migration_file(m)
    return m


def apply_migration(migration: dict[str, Any], db_path: str) -> int:
    version = migration['version']
    description = migration['description']
    operations = migration['operations']

    applied = []

    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = OFF")
        c = conn.cursor()

        create_migrations_table(c)

        if migration_applied(c, version):
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            conn.close()
            return 0

        c.execute("BEGIN IMMEDIATE")

        try:
            count = 0
            for op in operations:
                try:
                    OPERATION_HANDLERS[op['type']](c, op, version)
                    applied.append(op['type'])
                except KeyError:
                    raise MigrationError(f"Unknown operation type: '{op['type']}'")
                count += 1

            c.execute("INSERT INTO _migrations (version, description) VALUES (?, ?)", (version, description))
            conn.commit()
            conn.close()
            return 0

        except (MigrationError, sqlite3.Error) as e:
            conn.rollback()
            raise e

    except (MigrationError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')
    subparsers = parser.add_subparsers(dest='command', required=True)
    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()
    try:
        migration = load_migration_file(args.migration_file)
        sys.exit(apply_migration(migration, args.database))
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
