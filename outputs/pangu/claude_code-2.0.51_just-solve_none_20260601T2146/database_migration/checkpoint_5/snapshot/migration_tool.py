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
    return bool(name and re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def column_info(cursor: sqlite3.Cursor, table: str) -> list[tuple]:
    """Get PRAGMA table_info result for a table."""
    cursor.execute(f'PRAGMA table_info("{table}")')
    return cursor.fetchall()


def column_names(cursor: sqlite3.Cursor, table: str) -> list[str]:
    """Get list of column names for a table."""
    return [row[1] for row in column_info(cursor, table)]


def get_columns_with_fks(cursor: sqlite3.Cursor, table: str) -> list[tuple]:
    """Get columns with foreign key info."""
    cursor.execute(f'PRAGMA foreign_key_list("{table}")')
    return cursor.fetchall()


def has_column(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    return column in column_names(cursor, table)


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
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operations TEXT NOT NULL DEFAULT '[]',
            rollback_operations TEXT,
            depends_on TEXT
        )
    """)


VALID_COLUMN_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}


def validate_column(col: dict[str, Any]) -> None:
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


def _col_def_from_row(row: tuple) -> dict[str, Any]:
    """Convert PRAGMA table_info row to column definition dict."""
    return {
        "name": row[1],
        "type": row[2],
        "not_null": bool(row[3]),
        "default": row[4],
        "primary_key": bool(row[5]),
    }


def _col_has_autoincrement(cursor: sqlite3.Cursor, table: str, col_name: str) -> bool:
    """Check if a column has AUTOINCREMENT."""
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
    result = cursor.fetchone()
    if result:
        create_sql = result[0]
        # Check if this column has AUTOINCREMENT
        # Pattern: look for column definition followed by AUTOINCREMENT
        pattern = rf'"{re.escape(col_name)}"\s+INTEGER\s+PRIMARY KEY\s+AUTOINCREMENT'
        return bool(re.search(pattern, create_sql, re.IGNORECASE))
    return False


def rebuild_table_without_columns(cursor: sqlite3.Cursor, table: str, exclude_cols: list[str]) -> None:
    """Rebuild table excluding specific columns, preserving all other data."""
    info = column_info(cursor, table)
    keep = [r for r in info if r[1] not in exclude_cols]
    if not keep:
        raise MigrationError(f"Cannot exclude all columns from table '{table}'")

    col_map = {r[1]: r for r in info}

    new_defs = []
    for r in keep:
        p = [f'"{r[1]}"', r[2]]
        if r[3]:
            p.append('NOT NULL')
        if r[5]:
            p.append('PRIMARY KEY')
            if _col_has_autoincrement(cursor, table, r[1]):
                p.append('AUTOINCREMENT')
        new_defs.append(' '.join(p))

    temp = f'"__temp_{table}"'
    keep_names = [r[1] for r in keep]
    cols_joined = ', '.join(f'"{c}"' for c in keep_names)

    cursor.execute(f'CREATE TABLE {temp}({", ".join(new_defs)})')
    cursor.execute(f'INSERT INTO {temp} SELECT {cols_joined} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')


def get_table_schema(cursor: sqlite3.Cursor, table: str) -> str:
    """Get CREATE TABLE SQL for a table."""
    cursor.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name="{table}"')
    result = cursor.fetchone()
    if result is None:
        raise MigrationError(f"table '{table}' does not exist")
    return result[0]


def rebuild_table_with_schema(cursor: sqlite3.Cursor, table: str, create_sql: str) -> None:
    temp = f'"__temp_{table}"'
    cursor.execute(f'CREATE TABLE {temp}({create_sql})')
    cols = ', '.join(f'"{c}"' for c in column_names(cursor, table))
    cursor.execute(f'INSERT INTO {temp} SELECT {cols} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')


def validate_identifiers(*names: str) -> None:
    """Validate one or more identifiers."""
    for name in names:
        if name and not validate_identifier(name):
            raise MigrationError(f"Invalid identifier: '{name}'")


def validate_table_and_columns(cursor: sqlite3.Cursor, table: str, columns: list[str]) -> None:
    """Validate table exists and columns exist within it."""
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")
    for col in columns:
        if not has_column(cursor, table, col):
            raise MigrationError(f"column '{col}' does not exist in table '{table}'")


def apply_create_table(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    columns = op.get('columns', [])

    if not table:
        raise MigrationError("create_table operation missing required field 'table'")
    if not columns:
        raise MigrationError(f"create_table operation for table '{table}' has no columns")

    validate_identifiers(table)
    if table_exists(cursor, table):
        raise MigrationError(f"table '{table}' already exists")

    pk_count = sum(1 for col in columns if col.get('primary_key'))
    for col in columns:
        validate_column(col)
    if pk_count > 1:
        raise MigrationError("Only one column can be marked as primary_key")

    cursor.execute(f'CREATE TABLE "{table}"({", ".join(build_column(c) for c in columns)})')
    print(json.dumps({"event": "operation_applied", "type": "create_table", "table": table, "version": version}))


def apply_add_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("add_column operation missing required 'table' or 'column'")

    validate_identifiers(table)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    validate_column(column)

    existing = column_names(cursor, table)
    name = column['name']
    if name in existing:
        raise MigrationError(f"column '{name}' already exists in table '{table}'")

    if column.get('primary_key') and any(r[5] for r in column_info(cursor, table)):
        raise MigrationError(f"Table '{table}' already has a primary key column")

    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN {build_column(column)}')
    print(json.dumps({"event": "operation_applied", "type": "add_column", "table": table, "column": name, "version": version}))


def apply_drop_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("drop_column operation missing required 'table' or 'column'")

    validate_identifiers(table, column)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = column_names(cursor, table)
    if column not in existing:
        raise MigrationError(f"column '{column}' does not exist in table '{table}'")
    if len(existing) <= 1:
        raise MigrationError(f"Cannot drop column '{column}' from table '{table}'")

    info = column_info(cursor, table)
    if any(row[5] and row[1] == column for row in info):
        raise MigrationError(f"Cannot drop primary key column '{column}' from table '{table}'")

    rebuild_table_without_columns(cursor, table, [column])

    # Store original definition for rollback
    col_row = next(r for r in info if r[1] == column)
    orig_def = {
        "table": table,
        "column": column,
        "type": col_row[2],
        "not_null": bool(col_row[3]),
        "default": col_row[4],
        "primary_key": bool(col_row[5]),
        "auto_increment": _col_has_autoincrement(cursor, table, column) if col_row[5] else False,
        "unique": False,  # SQLite doesn't store UNIQUE in PRAGMA table_info simply
    }

    print(json.dumps({"event": "operation_applied", "type": "drop_column", "table": table, "column": column, "version": version, "original_definition": orig_def}))


def apply_transform_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    transformations = op.get('transformations', [])

    if not table:
        raise MigrationError("transform_data operation missing required field 'table'")
    if not transformations:
        raise MigrationError("transform_data operation must have at least one transformation")

    validate_identifiers(table)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    for i, t in enumerate(transformations):
        col = t.get('column')
        expr = t.get('expression')
        if not col or not expr:
            raise MigrationError(f"Transformation {i} missing required field 'column' or 'expression'")
        validate_identifiers(col)
        if not has_column(cursor, table, col):
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

    validate_identifiers(table, from_col, to_col)
    validate_table_and_columns(cursor, table, [from_col, to_col])

    sql = f'UPDATE "{table}" SET "{to_col}" = {f"COALESCE(\"{from_col}\", {default})" if default is not None else f'"{from_col}"'}'
    cursor.execute(sql)
    print(json.dumps({"event": "operation_applied", "type": "migrate_column_data", "table": table, "from_column": from_col, "to_column": to_col, "version": version}))


def apply_backfill_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    column = op.get('column')
    value = op.get('value')
    where = op.get('where')

    if not table or not column or value is None:
        raise MigrationError("backfill_data operation missing required 'table', 'column', or 'value'")

    validate_identifiers(table, column)
    validate_table_and_columns(cursor, table, [column])

    sql = f'UPDATE "{table}" SET "{column}" = {value}'
    if where:
        sql += f' WHERE {where}'

    cursor.execute(sql)
    print(json.dumps({"event": "operation_applied", "type": "backfill_data", "table": table, "column": column, "version": version}))


def apply_add_foreign_key(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')
    columns = op.get('columns', [])
    ref_table = op.get('ref_table')
    ref_columns = op.get('ref_columns', [])
    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    if not table or not name or not columns or not ref_table or not ref_columns:
        raise MigrationError("add_foreign_key operation missing required fields: 'table', 'name', 'columns', 'ref_table', or 'ref_columns'")

    validate_identifiers(table, name, ref_table)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")
    if not table_exists(cursor, ref_table):
        raise MigrationError(f"referenced table '{ref_table}' does not exist")

    validate_table_and_columns(cursor, table, columns)
    validate_table_and_columns(cursor, ref_table, ref_columns)

    cols_str = ', '.join(f'"{c}"' for c in columns)
    ref_cols_str = ', '.join(f'"{c}"' for c in ref_columns)

    cursor.execute(f'ALTER TABLE "{table}" ADD FOREIGN KEY ({cols_str}) REFERENCES "{ref_table}"({ref_cols_str}) ON DELETE {on_delete} ON UPDATE {on_update}')
    print(json.dumps({"event": "operation_applied", "type": "add_foreign_key", "table": table, "name": name, "version": version}))


def apply_drop_foreign_key(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_foreign_key operation missing required 'table' or 'name'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    fks = get_columns_with_fks(cursor, table)
    fk_info = next((fk for fk in fks if fk[1] == name), None)
    if fk_info is None:
        raise MigrationError(f"foreign key '{name}' not found on table '{table}'")

    create_sql = get_table_schema(cursor, table)
    rebuild_table_with_schema(cursor, table, create_sql)

    # Store original definition for rollback
    orig_def = {
        "table": table,
        "name": name,
        "columns": fk_info[4],
        "ref_table": fk_info[2],
        "ref_columns": [fk_info[3]],
    }

    print(json.dumps({"event": "operation_applied", "type": "drop_foreign_key", "table": table, "name": name, "version": version, "original_definition": orig_def}))


def rollback_add_foreign_key(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the foreign key."""
    table = op.get('table')
    name = op.get('name')
    columns = op.get('columns', [])
    ref_table = op.get('ref_table')
    ref_columns = op.get('ref_columns', [])
    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    if not table or not name or not columns or not ref_table or not ref_columns:
        raise MigrationError("add_foreign_key_rollback operation missing required fields")

    create_sql = get_table_schema(cursor, table)
    rebuild_table_with_schema(cursor, table, create_sql)

    print(json.dumps({"event": "operation_rolled_back", "type": "add_foreign_key", "table": table, "name": name, "version": version}))


def rollback_drop_foreign_key(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the foreign key with original definition."""
    orig_def = op.get('original_definition', {})

    table = orig_def.get('table') or op.get('table')
    name = orig_def.get('name') or op.get('name')
    columns = orig_def.get('columns', []) or op.get('columns', [])
    ref_table = orig_def.get('ref_table') or op.get('ref_table')
    ref_columns = orig_def.get('ref_columns', []) or op.get('ref_columns')

    if not all([table, name, columns, ref_table, ref_columns]):
        raise MigrationError("drop_foreign_key_rollback missing required fields or original_definition")

    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    cols_str = ', '.join(f'"{c}"' for c in columns)
    ref_cols_str = ', '.join(f'"{c}"' for c in ref_columns)

    cursor.execute(f'ALTER TABLE "{table}" ADD FOREIGN KEY ({cols_str}) REFERENCES "{ref_table}"({ref_cols_str}) ON DELETE {on_delete} ON UPDATE {on_update}')

    print(json.dumps({"event": "operation_rolled_back", "type": "drop_foreign_key", "table": table, "name": name, "version": version}))


def apply_create_index(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')
    columns = op.get('columns', [])
    unique = op.get('unique', False)

    if not table or not name or not columns:
        raise MigrationError("create_index operation missing required fields: 'table', 'name', or 'columns'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    validate_table_and_columns(cursor, table, columns)

    cols_str = ', '.join(f'"{c}"' for c in columns)
    unique_str = 'UNIQUE' if unique else ''

    cursor.execute(f'CREATE {unique_str} INDEX "{name}" ON "{table}"({cols_str})')
    print(json.dumps({"event": "operation_applied", "type": "create_index", "table": table, "name": name, "version": version}))


def rollback_create_index(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Drop the index."""
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("create_index rollback missing required 'table' or 'name'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,))
    if cursor.fetchone() is None:
        raise MigrationError(f"index '{name}' does not exist")

    cursor.execute(f'DROP INDEX "{name}"')
    print(json.dumps({"event": "operation_rolled_back", "type": "create_index", "table": table, "name": name, "version": version}))


def apply_drop_index(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_index operation missing required 'table' or 'name'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,))
    if cursor.fetchone() is None:
        raise MigrationError(f"index '{name}' does not exist")

    cursor.execute(f'DROP INDEX "{name}"')
    print(json.dumps({"event": "operation_applied", "type": "drop_index", "table": table, "name": name, "version": version}))


def rollback_drop_index(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-create the index with original definition."""
    orig_def = op.get('original_definition', {})

    table = orig_def.get('table') or op.get('table')
    name = orig_def.get('name') or op.get('name')
    columns = orig_def.get('columns', []) or op.get('columns', [])
    unique = orig_def.get('unique', False) or op.get('unique', False)

    if not all([table, name, columns]):
        raise MigrationError("drop_index_rollback missing required fields or original_definition")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cols_str = ', '.join(f'"{c}"' for c in columns)
    unique_str = 'UNIQUE' if unique else ''

    cursor.execute(f'CREATE {unique_str} INDEX "{name}" ON "{table}"({cols_str})')
    print(json.dumps({"event": "operation_rolled_back", "type": "drop_index", "table": table, "name": name, "version": version}))


def apply_add_check_constraint(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')
    expression = op.get('expression')

    if not table or not name or not expression:
        raise MigrationError("add_check_constraint operation missing required fields: 'table', 'name', or 'expression'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})')
    print(json.dumps({"event": "operation_applied", "type": "add_check_constraint", "table": table, "name": name, "version": version}))


def rollback_add_check_constraint(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Remove the check constraint by rebuilding table."""
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("add_check_constraint rollback missing required 'table' or 'name'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    create_sql = get_table_schema(cursor, table)
    rebuild_table_with_schema(cursor, table, create_sql)

    print(json.dumps({"event": "operation_rolled_back", "type": "add_check_constraint", "table": table, "name": name, "version": version}))


def apply_drop_check_constraint(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_check_constraint operation missing required 'table' or 'name'")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    create_sql = get_table_schema(cursor, table)
    rebuild_table_with_schema(cursor, table, create_sql)

    # Store original definition for rollback
    orig_def = {
        "table": table,
        "name": name,
        "expression": op.get('expression', ''),
    }

    print(json.dumps({"event": "operation_applied", "type": "drop_check_constraint", "table": table, "name": name, "version": version, "original_definition": orig_def}))


def rollback_drop_check_constraint(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the check constraint with original definition."""
    orig_def = op.get('original_definition', {})

    table = orig_def.get('table') or op.get('table')
    name = orig_def.get('name') or op.get('name')
    expression = orig_def.get('expression', '') or op.get('expression', '')

    if not all([table, name, expression]):
        raise MigrationError("drop_check_constraint_rollback missing required fields or original_definition")

    validate_identifiers(table, name)
    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})')

    print(json.dumps({"event": "operation_rolled_back", "type": "drop_check_constraint", "table": table, "name": name, "version": version}))


def get_rollback_handler(op_type: str) -> Any:
    handlers = {
        'create_table': rollback_create_table,
        'add_column': rollback_add_column,
        'drop_column': rollback_drop_column,
        'add_foreign_key': rollback_add_foreign_key,
        'drop_foreign_key': rollback_drop_foreign_key,
        'create_index': rollback_create_index,
        'drop_index': rollback_drop_index,
        'add_check_constraint': rollback_add_check_constraint,
        'drop_check_constraint': rollback_drop_check_constraint,
        'migrate_column_data': rollback_migrate_column_data,
    }
    return handlers.get(op_type)


def rollback_migration(cursor: sqlite3.Cursor, version: int, description: str, operations: list, rollback_ops: list = None) -> None:
    print(json.dumps({"event": "rollback_started", "version": version, "description": description}))

    # Use explicit rollback_operations if provided, otherwise use operations from migration
    ops_to_rollback = rollback_ops if rollback_ops is not None else operations

    # Process operations in reverse order
    for op in reversed(ops_to_rollback):
        op_type = op.get('type')
        handler = get_rollback_handler(op_type)
        if handler is None:
            raise MigrationError(f"No rollback handler for operation type: '{op_type}'")

        handler(cursor, op, version)

    # Remove from _migrations table
    cursor.execute("DELETE FROM _migrations WHERE version = ?", (version,))

    print(json.dumps({"event": "rollback_complete", "version": version}))


def get_applied_migrations(cursor: sqlite3.Cursor) -> list[dict]:
    cursor.execute("SELECT version, description, operations FROM _migrations ORDER BY version ASC")
    migrations = []
    for row in cursor.fetchall():
        migrations.append({
            "version": row[0],
            "description": row[1],
            "operations": json.loads(row[2]) if row[2] else []
        })
    return migrations


def get_applied_migrations_with_deps(cursor: sqlite3.Cursor) -> list[dict]:
    """Get applied migrations with their dependencies."""
    cursor.execute("SELECT version, description, operations, rollback_operations, depends FROM _migrations ORDER BY version ASC")
    migrations = []
    for row in cursor.fetchall():
        depends = json.loads(row[4]) if row[4] else []
        migrations.append({
            "version": row[0],
            "description": row[1],
            "operations": json.loads(row[2]) if row[2] else [],
            "rollback_operations": json.loads(row[3]) if row[3] else None,
            "depends_on": depends
        })
    return migrations


def rollback_command(db_path: str, to_version: int = None, count: int = None) -> int:
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        create_migrations_table(c)

        migrations = get_applied_migrations_with_deps(c)

        if not migrations:
            print("Error: no migrations to rollback", file=sys.stderr)
            return 1

        if to_version is not None:
            # Check if version exists
            versions = [m['version'] for m in migrations]
            if to_version not in versions:
                print(f"Error: version {to_version} not found", file=sys.stderr)
                return 1

            # Find migrations to rollback (those with version > to_version)
            migrations_to_rollback = [m for m in migrations if m['version'] > to_version]
        else:
            # Default: rollback last N migrations
            if count is None:
                count = 1
            migrations_to_rollback = migrations[-count:]

        if not migrations_to_rollback:
            print("Error: no migrations to rollback", file=sys.stderr)
            return 1

        # Build dependency graph and sort for proper rollback order
        graph = MigrationGraph()
        for m in migrations:
            graph.add_migration(m['version'], {'depends_on': m['depends_on']})

        # Build reverse adjacency for rollback order
        graph.build_reverse_adjacency()
        graph.adjacency = {m['version']: m['depends_on'] for m in migrations}

        # For rollback, we need to rollback dependents before their dependencies
        # Use topological sort on reversed graph (dependents -> dependencies)
        # The sort gives us order where dependencies come before dependents
        # For rollback, we need the reverse: dependents first

        # Get the set of versions to rollback
        versions_to_rollback = set(m['version'] for m in migrations_to_rollback)

        # Build subgraph only for migrations to rollback
        rollback_graph = MigrationGraph()
        for m in migrations:
            if m['version'] in versions_to_rollback:
                rollback_graph.add_migration(m['version'], {'depends_on': [d for d in m['depends_on'] if d in versions_to_rollback]})

        # Get rollback order - dependents first, dependencies later
        # Using topological sort on the original gives us: dependencies first, dependents later
        # So we reverse it for rollback
        try:
            rollback_order = list(reversed(rollback_graph.topological_sort()))
        except MigrationError as e:
            # If topological sort fails (cycle), fall back to reverse version order
            rollback_order = sorted(versions_to_rollback, reverse=True)

        versions_rolled_back = []
        try:
            c.execute("BEGIN IMMEDIATE")

            for version in rollback_order:
                migration = next(m for m in migrations_to_rollback if m['version'] == version)
                rollback_migration(c, migration['version'], migration['description'], migration['operations'], migration.get('rollback_operations'))
                versions_rolled_back.append(migration['version'])

            final_version = migrations[0]['version'] if migrations else 0

            conn.commit()
            conn.close()

            print(json.dumps({"event": "rollback_finished", "versions_rolled_back": sorted(versions_rolled_back), "final_version": final_version}))
            return 0

        except (MigrationError, sqlite3.Error) as e:
            conn.rollback()
            conn.close()
            print(f"Error: {e}", file=sys.stderr)
            return 1

    except sqlite3.Error as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


OPERATION_HANDLERS = {
    'create_table': apply_create_table,
    'add_column': apply_add_column,
    'drop_column': apply_drop_column,
    'transform_data': apply_transform_data,
    'migrate_column_data': apply_migrate_column_data,
    'backfill_data': apply_backfill_data,
    'add_foreign_key': apply_add_foreign_key,
    'drop_foreign_key': apply_drop_foreign_key,
    'create_index': apply_create_index,
    'drop_index': apply_drop_index,
    'add_check_constraint': apply_add_check_constraint,
    'drop_check_constraint': apply_drop_check_constraint,
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

    # Validate depends_on if present
    if 'depends_on' in migration:
        depends_on = migration['depends_on']
        if not isinstance(depends_on, list):
            raise MigrationError("depends_on must be a list")
        for i, dep in enumerate(depends_on):
            if not isinstance(dep, int) or dep < 1:
                raise MigrationError(f"depends_on[{i}] must be a positive integer")

    valid_ops = {'create_table', 'add_column', 'drop_column', 'transform_data', 'migrate_column_data', 'backfill_data',
                 'add_foreign_key', 'drop_foreign_key', 'create_index', 'drop_index', 'add_check_constraint', 'drop_check_constraint'}
    for i, op in enumerate(migration['operations']):
        if not isinstance(op, dict):
            raise MigrationError(f"Operation {i} must be an object")
        if 'type' not in op:
            raise MigrationError(f"Operation {i} missing required field 'type'")
        if op['type'] not in valid_ops:
            raise MigrationError(f"Operation {i} has invalid type '{op['type']}'")

    # Validate rollback_operations if present
    if 'rollback_operations' in migration:
        if not isinstance(migration['rollback_operations'], list):
            raise MigrationError("rollback_operations must be a list")
        for i, op in enumerate(migration['rollback_operations']):
            if not isinstance(op, dict):
                raise MigrationError(f"rollback_operation {i} must be an object")
            if 'type' not in op:
                raise MigrationError(f"rollback_operation {i} missing required field 'type'")
            # rollback operations must be valid operation types
            rollback_valid_ops = {'create_table', 'add_column', 'drop_column', 'add_foreign_key', 'drop_foreign_key',
                                  'create_index', 'drop_index', 'add_check_constraint', 'drop_check_constraint',
                                  'migrate_column_data'}
            if op['type'] not in rollback_valid_ops:
                raise MigrationError(f"rollback_operation {i} has invalid type '{op['type']}'")


class MigrationGraph:
    """Manages migration dependencies and provides topological sorting."""

    def __init__(self) -> None:
        self.migrations: dict[int, dict[str, Any]] = {}  # version -> migration info
        self.adjacency: dict[int, list[int]] = {}  # version -> list of dependencies
        self.reverse_adjacency: dict[int, list[int]] = {}  # version -> list dependents

    def add_migration(self, version: int, migration: dict[str, Any]) -> None:
        """Add a migration to the graph."""
        if version in self.migrations:
            raise MigrationError(f"Duplicate migration version: {version}")
        self.migrations[version] = migration
        self.adjacency[version] = migration.get('depends_on', [])[:]
        self.reverse_adjacency[version] = []

    def build_reverse_adjacency(self) -> None:
        """Build reverse adjacency list for efficient cycle detection."""
        self.reverse_adjacency = {v: [] for v in self.migrations}
        for version, deps in self.adjacency.items():
            for dep in deps:
                if dep in self.reverse_adjacency:
                    self.reverse_adjacency[dep].append(version)

    def validate_dependencies_exist(self) -> None:
        """Validate all dependencies reference existing migration versions."""
        for version, deps in self.adjacency.items():
            for dep in deps:
                if dep not in self.migrations:
                    raise MigrationError(f"dependency version {dep} not found")

    def validate_no_forward_dependencies(self) -> None:
        """Validate that migrations don't depend on future versions."""
        for version, deps in self.adjacency.items():
            for dep in deps:
                if dep > version:
                    raise MigrationError(f"migration version {version} cannot depend on future version {dep}")

    def detect_circular_dependency(self) -> list[int] | None:
        """Detect circular dependencies using DFS with color marking.
        Returns a cycle if found, None otherwise.
        """
        # Colors: 0 = white (unvisited), 1 = gray (visiting), 2 = black (visited)
        color = {v: 0 for v in self.migrations}
        parent = {v: None for v in self.migrations}
        cycle_start = None

        def dfs(u: int) -> bool:
            nonlocal cycle_start
            color[u] = 1  # Gray

            for v in self.adjacency.get(u, []):
                if color.get(v, 0) == 0:  # White
                    parent[v] = u
                    if dfs(v):
                        return True
                elif color.get(v, 0) == 1:  # Gray - cycle detected
                    cycle_start = v
                    return True

            color[u] = 2  # Black
            return False

        for version in self.migrations:
            if color.get(version, 0) == 0:
                if dfs(version):
                    # Reconstruct cycle
                    cycle = [cycle_start]
                    current = parent.get(cycle_start)
                    while current is not None and current != cycle_start:
                        cycle.append(current)
                        current = parent.get(current)
                    cycle.append(cycle_start)
                    return list(reversed(cycle))

        return None

    def topological_sort(self) -> list[int]:
        """Topological sort using DFS-based approach.
        Returns ordered list of version numbers.
        """
        visited = set()
        temp_visited = set()
        result = []

        def dfs(version: int) -> bool:
            if version in temp_visited:
                return False  # Cycle detected
            if version in visited:
                return True

            visited.add(version)
            temp_visited.add(version)

            for dep in self.adjacency.get(version, []):
                if not dfs(dep):
                    return False

            temp_visited.remove(version)
            result.append(version)
            return True

        for version in sorted(self.migrations.keys()):
            if version not in visited:
                if not dfs(version):
                    raise MigrationError("dependency resolution failed: conflicting requirements")

        return list(reversed(result))

    def validate_all(self) -> tuple[list[int], list[int]]:
        """Run all validations and return topological order.
        Returns (order, cycle) where cycle is empty if no cycle found.
        """
        self.validate_dependencies_exist()
        self.validate_no_forward_dependencies()
        self.build_reverse_adjacency()

        cycle = self.detect_circular_dependency()
        if cycle:
            return [], cycle

        order = self.topological_sort()
        return order, []


def discover_migrations_in_dir(directory: str) -> dict[int, tuple[str, dict[str, Any]]]:
    """Scan directory for migration JSON files and return version -> (filepath, migration) mapping.
    """
    import os
    migrations = {}

    for filename in os.listdir(directory):
        if filename.endswith('.json'):
            filepath = os.path.join(directory, filename)
            try:
                migration = load_migration_file(filepath)
                version = migration['version']
                if version in migrations:
                    raise MigrationError(f"Duplicate version {version} found in {filename} and {migrations[version][0]}")
                migrations[version] = (filename, migration)
            except (MigrationError, json.JSONDecodeError) as e:
                if isinstance(e, MigrationError):
                    raise
                raise MigrationError(f"invalid migration file: {filename}")

    return migrations


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


def get_column_definition(cursor: sqlite3.Cursor, table: str, column: str) -> dict[str, Any]:
    """Get the full definition of a column for later restoration."""
    for row in column_info(cursor, table):
        if row[1] == column:
            col_def = _col_def_from_row(row)
            if col_def.get('primary_key') and col_def.get('type', '').upper() == 'INTEGER':
                create_sql = get_table_schema(cursor, table)
                if 'AUTOINCREMENT' in create_sql.upper():
                    col_def['auto_increment'] = True
            return col_def
    return None


def apply_migration(migration: dict[str, Any], db_path: str) -> int:
    version = migration['version']
    description = migration['description']
    operations = migration['operations']

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
        ops_metadata = []

        for op in operations:
            handler = OPERATION_HANDLERS.get(op['type'])
            if handler is None:
                raise MigrationError(f"Unknown operation type: '{op['type']}'")

            if op['type'] == 'drop_column':
                table = op.get('table')
                column = op.get('column')
                if table and column:
                    orig_def = get_column_definition(c, table, column)
                    if orig_def:
                        op_with_orig = op.copy()
                        op_with_orig['original_definition'] = orig_def
                        handler(c, op_with_orig, version)
                        ops_metadata.append(op_with_orig)
                        continue

            handler(c, op, version)
            ops_metadata.append(op)

        rollback_ops = migration.get('rollback_operations')
        depends_on = migration.get('depends_on', [])
        c.execute("INSERT INTO _migrations (version, description, operations, rollback_operations, depends_on) VALUES (?, ?, ?, ?, ?)",
                  (version, description, json.dumps(ops_metadata), json.dumps(rollback_ops) if rollback_ops else None, json.dumps(depends_on)))
        conn.commit()
        conn.close()
        return 0

    except (MigrationError, sqlite3.Error) as e:
        if 'conn' in locals():
            conn.rollback()
            conn.close()
        print(f"Error: {e}", file=sys.stderr)
        return 1


def validate_command(migration_file: str, migrations_dir: str = None) -> int:
    """Validate a migration file, optionally checking dependencies against other migrations."""
    try:
        print(json.dumps({"event": "validation_started", "migration_file": migration_file}))

        migration = load_migration_file(migration_file)
        version = migration['version']
        depends_on = migration.get('depends_on', [])

        # Check dependencies if directory provided
        if depends_on and migrations_dir:
            print(json.dumps({"event": "dependency_check", "version": version, "depends_on": depends_on, "status": "ok"}))

            # Load all migrations from directory
            all_migrations = discover_migrations_in_dir(migrations_dir)

            # Build graph and validate
            graph = MigrationGraph()
            for v, (_, m) in all_migrations.items():
                graph.add_migration(v, m)

            order, cycle = graph.validate_all()

            if cycle:
                print(json.dumps({"event": "circular_dependency_detected", "cycle": cycle, "status": "error"}))
                print(json.dumps({"event": "validation_complete", "version": version, "status": "invalid"}))
                print(f"Error: circular dependency detected: cycle {cycle}", file=sys.stderr)
                return 1
        elif depends_on:
            print(json.dumps({"event": "dependency_check", "version": version, "depends_on": depends_on,
                           "status": "warning", "message": "cannot verify dependencies without --migrations-dir"}))

        print(json.dumps({"event": "validation_complete", "version": version, "status": "valid"}))
        return 0

    except MigrationError as e:
        print(json.dumps({"event": "validation_complete", "version": migration.get('version', 'unknown'), "status": "invalid"}))
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(json.dumps({"event": "validation_complete", "version": migration.get('version', 'unknown'), "status": "invalid"}))
        print(f"Error: {e}", file=sys.stderr)
        return 1


def migrate_all_command(migrations_dir: str, database: str) -> int:
    """Apply all migrations from a directory in dependency order."""
    try:
        print(json.dumps({"event": "scan_started", "directory": migrations_dir}))

        # Discover all migrations
        all_migrations = discover_migrations_in_dir(migrations_dir)

        for version, (file, _) in all_migrations.items():
            print(json.dumps({"event": "migration_discovered", "file": file, "version": version}))

        print(json.dumps({"event": "scan_complete", "migrations_found": len(all_migrations)}))

        # Build dependency graph
        print(json.dumps({"event": "dependency_resolution_started"}))
        graph = MigrationGraph()
        for version, (_, migration) in all_migrations.items():
            graph.add_migration(version, migration)

        order, cycle = graph.validate_all()

        if cycle:
            print(json.dumps({"event": "circular_dependency_detected", "cycle": cycle, "status": "error"}))
            print(json.dumps({"event": "batch_complete", "migrations_applied": 0, "migrations_skipped": 0, "final_version": 0}))
            print(f"Error: circular dependency detected: cycle {cycle}", file=sys.stderr)
            return 1

        print(json.dumps({"event": "dependency_resolved", "order": order}))

        # Apply migrations in order
        conn = sqlite3.connect(database)
        conn.execute("PRAGMA foreign_keys = OFF")
        c = conn.cursor()
        create_migrations_table(c)
        conn.commit()
        conn.close()

        applied = 0
        skipped = 0
        final_version = 0

        for version in order:
            migration = all_migrations[version][1]
            filename = all_migrations[version][0]

            result = apply_migration(migration, database)
            if result == 0:
                applied += 1
                final_version = version
            else:
                skipped += 1

        print(json.dumps({"event": "batch_complete", "migrations_applied": applied, "migrations_skipped": skipped, "final_version": final_version}))
        return 0 if skipped == 0 else 1

    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')
    subparsers = parser.add_subparsers(dest='command', required=True)

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    rollback_parser = subparsers.add_parser('rollback', help='Rollback migrations')
    rollback_parser.add_argument('database', help='Path to SQLite database file')
    rollback_parser.add_argument('--to-version', type=int, help='Rollback to a specific version (exclusive)')
    rollback_parser.add_argument('--count', type=int, help='Rollback the last N migrations (default: 1)')

    validate_parser = subparsers.add_parser('validate', help='Validate a migration file')
    validate_parser.add_argument('migration_file', help='Path to migration JSON file')
    validate_parser.add_argument('--migrations-dir', dest='migrations_dir', help='Directory containing all migration files for dependency checking')

    migrate_all_parser = subparsers.add_parser('migrate-all', help='Apply all migrations from a directory')
    migrate_all_parser.add_argument('--migrations-dir', dest='migrations_dir', required=True, help='Directory containing migration files')
    migrate_all_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()

    if args.command == 'migrate':
        try:
            migration = load_migration_file(args.migration_file)
            sys.exit(apply_migration(migration, args.database))
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command == 'rollback':
        sys.exit(rollback_command(args.database, args.to_version, args.count))
    elif args.command == 'validate':
        sys.exit(validate_command(args.migration_file, args.migrations_dir))
    elif args.command == 'migrate-all':
        sys.exit(migrate_all_command(args.migrations_dir, args.database))


if __name__ == '__main__':
    main()
