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


def _table_info(cursor: sqlite3.Cursor, table: str) -> list[tuple]:
    cursor.execute(f'PRAGMA table_info("{table}")')
    return cursor.fetchall()


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
            rollback_operations TEXT
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

    if not validate_identifier(table) or not validate_identifier(name) or not validate_identifier(ref_table):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")
    if not table_exists(cursor, ref_table):
        raise MigrationError(f"referenced table '{ref_table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]
    for col in columns:
        if col not in existing:
            raise MigrationError(f"column '{col}' does not exist in table '{table}'")

    ref_existing = [row[1] for row in _table_info(cursor, ref_table)]
    for col in ref_columns:
        if col not in ref_existing:
            raise MigrationError(f"referenced column '{col}' does not exist in table '{ref_table}'")

    cols_str = ', '.join(f'"{c}"' for c in columns)
    ref_cols_str = ', '.join(f'"{c}"' for c in ref_columns)

    cursor.execute(f'ALTER TABLE "{table}" ADD FOREIGN KEY ({cols_str}) REFERENCES "{ref_table}"({ref_cols_str}) ON DELETE {on_delete} ON UPDATE {on_update}')
    print(json.dumps({"event": "operation_applied", "type": "add_foreign_key", "table": table, "name": name, "version": version}))


def apply_drop_foreign_key(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_foreign_key operation missing required 'table' or 'name'")

    if not validate_identifier(table) or not validate_identifier(name):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    # Get foreign key list for the table
    cursor.execute(f'PRAGMA foreign_key_list("{table}")')
    fks = cursor.fetchall()

    fk_info = None
    for fk in fks:
        if fk[1] == name:  # fk[1] is the foreign key name
            fk_info = fk
            break

    if fk_info is None:
        raise MigrationError(f"foreign key '{name}' not found on table '{table}'")

    # For SQLite, we need to rebuild the table to drop a foreign key
    # Get table schema
    cursor.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name="{table}"')
    create_sql = cursor.fetchone()[0]

    temp = f'"__temp_{table}"'
    cursor.execute(f'CREATE TABLE {temp}({create_sql})')

    # Get all columns except the foreign key constraint
    cols = [row[1] for row in _table_info(cursor, table)]
    cols_str = ', '.join(f'"{c}"' for c in cols)

    cursor.execute(f'INSERT INTO {temp} SELECT {cols_str} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')

    # Store original definition for rollback
    orig_def = {
        "table": table,
        "name": name,
        "columns": fk_info[4],  # fk_info[4] contains column names
        "ref_table": fk_info[2],  # fk_info[2] is referenced table
        "ref_columns": [fk_info[3]]  # fk_info[3] is referenced column
    }

    print(json.dumps({"event": "operation_applied", "type": "drop_foreign_key", "table": table, "name": name, "version": version, "original_definition": orig_def}))


def apply_add_foreign_key_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the foreign key"""
    table = op.get('table')
    name = op.get('name')
    columns = op.get('columns', [])
    ref_table = op.get('ref_table')
    ref_columns = op.get('ref_columns', [])
    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    if not table or not name or not columns or not ref_table or not ref_columns:
        raise MigrationError("add_foreign_key_rollback operation missing required fields")

    # Rebuild table to add the foreign key back
    cursor.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name="{table}"')
    create_sql = cursor.fetchone()[0]

    temp = f'"__temp_{table}"'
    cursor.execute(f'CREATE TABLE {temp}({create_sql})')

    cols = [row[1] for row in _table_info(cursor, table)]
    cols_str = ', '.join(f'"{c}"' for c in cols)

    cursor.execute(f'INSERT INTO {temp} SELECT {cols_str} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')

    print(json.dumps({"event": "operation_rolled_back", "type": "add_foreign_key", "table": table, "name": name, "version": version}))


def apply_drop_foreign_key_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the foreign key with original definition"""
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

    if not validate_identifier(table) or not validate_identifier(name):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]
    for col in columns:
        if col not in existing:
            raise MigrationError(f"column '{col}' does not exist in table '{table}'")

    cols_str = ', '.join(f'"{c}"' for c in columns)
    unique_str = 'UNIQUE' if unique else ''

    cursor.execute(f'CREATE {unique_str} INDEX "{name}" ON "{table}"({cols_str})')
    print(json.dumps({"event": "operation_applied", "type": "create_index", "table": table, "name": name, "version": version}))


def apply_drop_index(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_index operation missing required 'table' or 'name'")

    if not validate_identifier(table) or not validate_identifier(name):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    # Check if index exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,))
    if cursor.fetchone() is None:
        raise MigrationError(f"index '{name}' does not exist")

    cursor.execute(f'DROP INDEX "{name}"')

    print(json.dumps({"event": "operation_applied", "type": "drop_index", "table": table, "name": name, "version": version}))


def apply_create_index_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-create the index"""
    table = op.get('table')
    name = op.get('name')
    columns = op.get('columns', [])
    unique = op.get('unique', False)

    if not table or not name or not columns:
        raise MigrationError("create_index_rollback missing required fields")

    cols_str = ', '.join(f'"{c}"' for c in columns)
    unique_str = 'UNIQUE' if unique else ''

    cursor.execute(f'CREATE {unique_str} INDEX "{name}" ON "{table}"({cols_str})')
    print(json.dumps({"event": "operation_rolled_back", "type": "create_index", "table": table, "name": name, "version": version}))


def apply_drop_index_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-create the index with original definition"""
    orig_def = op.get('original_definition', {})

    table = orig_def.get('table') or op.get('table')
    name = orig_def.get('name') or op.get('name')
    columns = orig_def.get('columns', []) or op.get('columns', [])
    unique = orig_def.get('unique', False) or op.get('unique', False)

    if not all([table, name, columns]):
        raise MigrationError("drop_index_rollback missing required fields or original_definition")

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

    if not validate_identifier(table) or not validate_identifier(name):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})')
    print(json.dumps({"event": "operation_applied", "type": "add_check_constraint", "table": table, "name": name, "version": version}))


def apply_drop_check_constraint(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    table = op.get('table')
    name = op.get('name')

    if not table or not name:
        raise MigrationError("drop_check_constraint operation missing required 'table' or 'name'")

    if not validate_identifier(table) or not validate_identifier(name):
        raise MigrationError(f"Invalid identifier")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    # Rebuild table to drop check constraint
    cursor.execute(f'SELECT sql FROM sqlite_master WHERE type="table" AND name="{table}"')
    create_sql = cursor.fetchone()[0]

    temp = f'"__temp_{table}"'
    cursor.execute(f'CREATE TABLE {temp}({create_sql})')

    cols = [row[1] for row in _table_info(cursor, table)]
    cols_str = ', '.join(f'"{c}"' for c in cols)

    cursor.execute(f'INSERT INTO {temp} SELECT {cols_str} FROM "{table}"')
    cursor.execute(f'DROP TABLE "{table}"')
    cursor.execute(f'ALTER TABLE {temp} RENAME TO "{table}"')

    # Store original definition for rollback
    orig_def = {
        "table": table,
        "name": name,
        "expression": op.get('expression', '')
    }

    print(json.dumps({"event": "operation_applied", "type": "drop_check_constraint", "table": table, "name": name, "version": version, "original_definition": orig_def}))


def apply_add_check_constraint_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the check constraint"""
    table = op.get('table')
    name = op.get('name')
    expression = op.get('expression')

    if not table or not name or not expression:
        raise MigrationError("add_check_constraint_rollback missing required fields")

    cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})')
    print(json.dumps({"event": "operation_rolled_back", "type": "add_check_constraint", "table": table, "name": name, "version": version}))


def apply_drop_check_constraint_rollback(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the check constraint with original definition"""
    orig_def = op.get('original_definition', {})

    table = orig_def.get('table') or op.get('table')
    name = orig_def.get('name') or op.get('name')
    expression = orig_def.get('expression', '') or op.get('expression', '')

    if not all([table, name, expression]):
        raise MigrationError("drop_check_constraint_rollback missing required fields or original_definition")

    cursor.execute(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" CHECK ({expression})')

    print(json.dumps({"event": "operation_rolled_back", "type": "drop_check_constraint", "table": table, "name": name, "version": version}))


# ============== ROLLBACK HANDLERS ==============

def rollback_create_table(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Drop the table"""
    table = op.get('table')
    if not table:
        raise MigrationError("create_table rollback missing required field 'table'")

    # Check for foreign keys referencing this table
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    if cursor.fetchone() is None:
        raise MigrationError(f"table '{table}' does not exist")

    # Check for foreign keys from other tables to this table
    cursor.execute(f'PRAGMA foreign_key_list("{table}")')
    incoming_fks = cursor.fetchall()
    if incoming_fks:
        raise MigrationError("cannot rollback: foreign key constraint violation")

    cursor.execute(f'DROP TABLE "{table}"')
    print(json.dumps({"event": "operation_rolled_back", "type": "create_table", "table": table, "version": version}))


def rollback_add_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Drop the column (data is lost)"""
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("add_column rollback missing required 'table' or 'column'")

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

    # Rebuild table without the column
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

    print(json.dumps({"event": "operation_rolled_back", "type": "add_column", "table": table, "column": column, "version": version}))


def rollback_drop_column(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Re-add the column with original definition"""
    orig_def = op.get('original_definition')
    table = op.get('table')
    column = op.get('column')

    if not table or not column:
        raise MigrationError("drop_column rollback missing required 'table' or 'column'")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    info = _table_info(cursor, table)
    existing = [row[1] for row in info]

    if column in existing:
        raise MigrationError(f"column '{column}' already exists in table '{table}'")

    if orig_def:
        # Use original definition
        col_def = {
            "name": column,
            "type": orig_def.get('type', 'TEXT'),
            "not_null": orig_def.get('not_null', False),
            "unique": orig_def.get('unique', False),
            "default": orig_def.get('default'),
            "primary_key": orig_def.get('primary_key', False),
            "auto_increment": orig_def.get('auto_increment', False)
        }
    else:
        # Default to TEXT if no original definition
        col_def = {"name": column, "type": "TEXT", "not_null": False}

    cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN {build_column(col_def)}')
    print(json.dumps({"event": "operation_rolled_back", "type": "drop_column", "table": table, "column": column, "version": version}))


def rollback_migrate_column_data(cursor: sqlite3.Cursor, op: dict[str, Any], version: int) -> None:
    """Rollback: Reverse the migration (copy from to_column back to from_column)"""
    table = op.get('table')
    from_col = op.get('from_column')
    to_col = op.get('to_column')
    default = op.get('default_value')

    if not table or not from_col or not to_col:
        raise MigrationError("migrate_column_data rollback missing required fields")

    if not table_exists(cursor, table):
        raise MigrationError(f"table '{table}' does not exist")

    existing = [row[1] for row in _table_info(cursor, table)]

    if from_col not in existing:
        raise MigrationError(f"column '{from_col}' does not exist in table '{table}'")
    if to_col not in existing:
        raise MigrationError(f"column '{to_col}' does not exist in table '{table}'")

    # Reverse: copy from to_col back to from_col
    if default is not None:
        sql = f'UPDATE "{table}" SET "{from_col}" = COALESCE("{to_col}", {default})'
    else:
        sql = f'UPDATE "{table}" SET "{from_col}" = "{to_col}"'

    cursor.execute(sql)
    print(json.dumps({"event": "operation_rolled_back", "type": "migrate_column_data", "table": table, "from_column": from_col, "to_column": to_col, "version": version}))


def get_rollback_handler(op_type: str) -> Any:
    """Get the rollback handler for an operation type."""
    handlers = {
        'create_table': rollback_create_table,
        'add_column': rollback_add_column,
        'drop_column': rollback_drop_column,
        'add_foreign_key': apply_add_foreign_key_rollback,
        'drop_foreign_key': apply_drop_foreign_key_rollback,
        'create_index': apply_create_index_rollback,
        'drop_index': apply_drop_index_rollback,
        'add_check_constraint': apply_add_check_constraint_rollback,
        'drop_check_constraint': apply_drop_check_constraint_rollback,
        'migrate_column_data': rollback_migrate_column_data,
    }
    return handlers.get(op_type)


def rollback_migration(cursor: sqlite3.Cursor, version: int, description: str, operations: list, rollback_ops: list = None) -> None:
    """Rollback a single migration by executing operations in reverse order."""
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
    """Get list of applied migrations ordered by version."""
    cursor.execute("SELECT version, description, operations FROM _migrations ORDER BY version ASC")
    migrations = []
    for row in cursor.fetchall():
        migrations.append({
            "version": row[0],
            "description": row[1],
            "operations": json.loads(row[2]) if row[2] else []
        })
    return migrations


def rollback_command(db_path: str, to_version: int = None, count: int = None) -> int:
    """Execute the rollback command."""
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()

        create_migrations_table(c)

        migrations = get_applied_migrations(c)

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

        # Sort in reverse order (highest version first)
        migrations_to_rollback = sorted(migrations_to_rollback, key=lambda m: m['version'], reverse=True)

        versions_rolled_back = []
        try:
            c.execute("BEGIN IMMEDIATE")

            for migration in migrations_to_rollback:
                rollback_migration(c, migration['version'], migration['description'], migration['operations'], migration.get('rollback_operations'))
                versions_rolled_back.append(migration['version'])

            final_version = migrations[-1]['version'] if migrations else 0

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
    cursor.execute(f'PRAGMA table_info("{table}")')
    for row in cursor.fetchall():
        if row[1] == column:
            col_def = {
                "name": row[1],
                "type": row[2],
                "not_null": bool(row[3]),
                "default": row[4],
                "primary_key": bool(row[5])
            }
            # Check for auto_increment
            if col_def.get('primary_key') and col_def.get('type', '').upper() == 'INTEGER':
                cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{table}'")
                create_sql = cursor.fetchone()[0]
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

        # Store operation metadata for rollback
        ops_metadata = []

        for op in operations:
            handler = OPERATION_HANDLERS.get(op['type'])
            if handler is None:
                raise MigrationError(f"Unknown operation type: '{op['type']}'")

            # For drop_column, store original definition for automatic rollback
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

        # Store operations metadata as JSON in _migrations table
        rollback_ops = migration.get('rollback_operations')
        c.execute("INSERT INTO _migrations (version, description, operations, rollback_operations) VALUES (?, ?, ?, ?)",
                  (version, description, json.dumps(ops_metadata), json.dumps(rollback_ops) if rollback_ops else None))
        conn.commit()
        conn.close()
        return 0

    except (MigrationError, sqlite3.Error) as e:
        if 'conn' in locals():
            conn.rollback()
            conn.close()
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


if __name__ == '__main__':
    main()
