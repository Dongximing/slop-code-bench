#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 3: Foreign Keys, Indexes, and Constraints

A CLI tool that applies database schema migrations to SQLite databases.
Supports foreign key relationships, custom indexes, and advanced constraints.
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


class MigrationError(Exception):
    """Base exception for migration errors."""
    pass


class MigrationFileError(MigrationError):
    """Error related to migration file (not found, invalid JSON, invalid schema)."""
    pass


class OperationError(MigrationError):
    """Error related to a specific migration operation."""
    pass


class SQLError(MigrationError):
    """Error related to SQL execution."""
    pass


def validate_identifier(name: str, identifier_type: str = "identifier") -> None:
    """
    Validate that a name is a valid SQLite identifier.
    Must be alphanumeric + underscore, starting with letter or underscore.
    """
    if not isinstance(name, str):
        raise MigrationFileError(f"invalid migration schema: {identifier_type} must be a string")
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise MigrationFileError(f"invalid migration schema: invalid {identifier_type} '{name}'")


def quote_identifier(identifier: str) -> str:
    """Properly quote/escape a SQLite identifier to prevent SQL injection."""
    # Double any double quotes and wrap in double quotes
    return '"' + identifier.replace('"', '""') + '"'


def load_migration_file(migration_path: str) -> Dict[str, Any]:
    """Load and parse the migration JSON file."""
    path = Path(migration_path)

    if not path.exists():
        raise MigrationFileError(f"migration file not found: {migration_path}")

    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationFileError(f"invalid JSON in migration file: {e}")

    return data


def validate_migration_schema(data: Dict[str, Any]) -> None:
    """Validate the migration schema structure."""
    if not isinstance(data, dict):
        raise MigrationFileError("invalid migration schema: migration must be an object")

    # Check required fields
    if 'version' not in data:
        raise MigrationFileError("invalid migration schema: missing required field 'version'")
    if 'description' not in data:
        raise MigrationFileError("invalid migration schema: missing required field 'description'")
    if 'operations' not in data:
        raise MigrationFileError("invalid migration schema: missing required field 'operations'")

    # Validate version
    if not isinstance(data['version'], int) or data['version'] <= 0:
        raise MigrationFileError("invalid migration schema: version must be a positive integer")

    # Validate description
    if not isinstance(data['description'], str):
        raise MigrationFileError("invalid migration schema: description must be a string")

    # Validate operations
    if not isinstance(data['operations'], list):
        raise MigrationFileError("invalid migration schema: operations must be an array")

    if len(data['operations']) == 0:
        raise MigrationFileError("invalid migration schema: operations array cannot be empty")

    for i, op in enumerate(data['operations']):
        validate_operation(op, i)


def validate_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a single operation."""
    if not isinstance(op, dict):
        raise MigrationFileError(f"invalid migration schema: operation {index} must be an object")

    if 'type' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'type'")

    op_type = op['type']

    if op_type == 'create_table':
        validate_create_table_operation(op, index)
    elif op_type == 'add_column':
        validate_add_column_operation(op, index)
    elif op_type == 'drop_column':
        validate_drop_column_operation(op, index)
    elif op_type == 'transform_data':
        validate_transform_data_operation(op, index)
    elif op_type == 'migrate_column_data':
        validate_migrate_column_data_operation(op, index)
    elif op_type == 'backfill_data':
        validate_backfill_data_operation(op, index)
    elif op_type == 'add_foreign_key':
        validate_add_foreign_key_operation(op, index)
    elif op_type == 'drop_foreign_key':
        validate_drop_foreign_key_operation(op, index)
    elif op_type == 'create_index':
        validate_create_index_operation(op, index)
    elif op_type == 'drop_index':
        validate_drop_index_operation(op, index)
    elif op_type == 'add_check_constraint':
        validate_add_check_constraint_operation(op, index)
    elif op_type == 'drop_check_constraint':
        validate_drop_check_constraint_operation(op, index)
    else:
        raise MigrationFileError(f"invalid migration schema: unknown operation type '{op_type}'")


def validate_create_table_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a create_table operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'columns' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'columns'")

    validate_identifier(op['table'], "table name")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise MigrationFileError(f"invalid migration schema: operation {index} columns must be a non-empty array")

    # Check for at most one primary key
    pk_count = 0
    for col in op['columns']:
        if col.get('primary_key', False):
            pk_count += 1

    if pk_count > 1:
        raise MigrationFileError(f"invalid migration schema: operation {index} cannot have more than one primary key column")

    # Validate each column
    for col in op['columns']:
        validate_column_definition(col, index, pk_count == 1)


def validate_column_definition(col: Dict[str, Any], op_index: int, has_pk: bool) -> None:
    """Validate a column definition."""
    if not isinstance(col, dict):
        raise MigrationFileError(f"invalid migration schema: operation {op_index} column must be an object")

    if 'name' not in col:
        raise MigrationFileError(f"invalid migration schema: operation {op_index} column missing required field 'name'")
    if 'type' not in col:
        raise MigrationFileError(f"invalid migration schema: operation {op_index} column missing required field 'type'")

    validate_identifier(col['name'], "column name")

    # Validate type
    valid_types = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}
    if col['type'].upper() not in valid_types:
        raise MigrationFileError(f"invalid migration schema: operation {op_index} column has invalid type '{col['type']}'")

    # Validate auto_increment
    if col.get('auto_increment', False):
        if col['type'].upper() != 'INTEGER':
            raise MigrationFileError(f"invalid migration schema: operation {op_index} auto_increment can only be used with INTEGER type")
        if not col.get('primary_key', False):
            raise MigrationFileError(f"invalid migration schema: operation {op_index} auto_increment can only be used with primary_key")


def validate_add_column_operation(op: Dict[str, Any], index: int) -> None:
    """Validate an add_column operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'column' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'column'")

    validate_identifier(op['table'], "table name")

    col = op['column']
    if not isinstance(col, dict):
        raise MigrationFileError(f"invalid migration schema: operation {index} column must be an object")

    # For add_column, auto_increment cannot be used (SQLite limitation)
    if 'name' not in col:
        raise MigrationFileError(f"invalid migration schema: operation {index} column missing required field 'name'")
    if 'type' not in col:
        raise MigrationFileError(f"invalid migration schema: operation {index} column missing required field 'type'")

    validate_identifier(col['name'], "column name")

    valid_types = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}
    if col['type'].upper() not in valid_types:
        raise MigrationFileError(f"invalid migration schema: operation {index} column has invalid type '{col['type']}'")

    # auto_increment not allowed in add_column
    if col.get('auto_increment', False):
        raise MigrationFileError(f"invalid migration schema: operation {index} auto_increment cannot be used in add_column")

    # primary_key not allowed in add_column for existing tables
    if col.get('primary_key', False):
        raise MigrationFileError(f"invalid migration schema: operation {index} primary_key cannot be added to existing table")


def validate_drop_column_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a drop_column operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'column' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'column'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['column'], "column name")


def validate_transform_data_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a transform_data operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'transformations' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'transformations'")

    validate_identifier(op['table'], "table name")

    if not isinstance(op['transformations'], list) or len(op['transformations']) == 0:
        raise MigrationFileError(f"invalid migration schema: operation {index} transformations must be a non-empty array")

    for i, transform in enumerate(op['transformations']):
        if not isinstance(transform, dict):
            raise MigrationFileError(f"invalid migration schema: operation {index} transformation {i} must be an object")
        if 'column' not in transform:
            raise MigrationFileError(f"invalid migration schema: operation {index} transformation {i} missing required field 'column'")
        if 'expression' not in transform:
            raise MigrationFileError(f"invalid migration schema: operation {index} transformation {i} missing required field 'expression'")

        validate_identifier(transform['column'], f"column name in transformation {i}")
        if not isinstance(transform['expression'], str):
            raise MigrationFileError(f"invalid migration schema: operation {index} transformation {i} expression must be a string")


def validate_migrate_column_data_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a migrate_column_data operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'from_column' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'from_column'")
    if 'to_column' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'to_column'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['from_column'], "from_column name")
    validate_identifier(op['to_column'], "to_column name")


def validate_backfill_data_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a backfill_data operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'column' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'column'")
    if 'value' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'value'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['column'], "column name")

    # value can be a string (SQL expression) or other JSON value
    # We just validate it exists

    # where clause is optional but must be a string if provided
    if 'where' in op and not isinstance(op['where'], str):
        raise MigrationFileError(f"invalid migration schema: operation {index} 'where' must be a string")


def validate_add_foreign_key_operation(op: Dict[str, Any], index: int) -> None:
    """Validate an add_foreign_key operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")
    if 'columns' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'columns'")
    if 'references' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'references'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['name'], "foreign key name")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise MigrationFileError(f"invalid migration schema: operation {index} columns must be a non-empty array")

    for col in op['columns']:
        validate_identifier(col, "column name")

    refs = op['references']
    if not isinstance(refs, dict):
        raise MigrationFileError(f"invalid migration schema: operation {index} references must be an object")
    if 'table' not in refs:
        raise MigrationFileError(f"invalid migration schema: operation {index} references missing required field 'table'")
    if 'columns' not in refs:
        raise MigrationFileError(f"invalid migration schema: operation {index} references missing required field 'columns'")

    validate_identifier(refs['table'], "referenced table name")

    if not isinstance(refs['columns'], list) or len(refs['columns']) == 0:
        raise MigrationFileError(f"invalid migration schema: operation {index} references.columns must be a non-empty array")

    for col in refs['columns']:
        validate_identifier(col, "referenced column name")

    if len(op['columns']) != len(refs['columns']):
        raise MigrationFileError(f"invalid migration schema: operation {index} columns count must match references.columns count")

    # Validate on_delete and on_update
    valid_actions = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}
    if 'on_delete' in op:
        if not isinstance(op['on_delete'], str) or op['on_delete'].upper() not in valid_actions:
            raise MigrationFileError(f"invalid migration schema: operation {index} invalid on_delete value")
    if 'on_update' in op:
        if not isinstance(op['on_update'], str) or op['on_update'].upper() not in valid_actions:
            raise MigrationFileError(f"invalid migration schema: operation {index} invalid on_update value")


def validate_drop_foreign_key_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a drop_foreign_key operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['name'], "foreign key name")


def validate_create_index_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a create_index operation."""
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'columns' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'columns'")

    validate_identifier(op['name'], "index name")
    validate_identifier(op['table'], "table name")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise MigrationFileError(f"invalid migration schema: operation {index} columns must be a non-empty array")

    for col in op['columns']:
        validate_identifier(col, "column name")

    # unique is optional, default false
    if 'unique' in op and not isinstance(op['unique'], bool):
        raise MigrationFileError(f"invalid migration schema: operation {index} 'unique' must be a boolean")


def validate_drop_index_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a drop_index operation."""
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")

    validate_identifier(op['name'], "index name")


def validate_add_check_constraint_operation(op: Dict[str, Any], index: int) -> None:
    """Validate an add_check_constraint operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")
    if 'expression' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'expression'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['name'], "constraint name")

    if not isinstance(op['expression'], str):
        raise MigrationFileError(f"invalid migration schema: operation {index} expression must be a string")


def validate_drop_check_constraint_operation(op: Dict[str, Any], index: int) -> None:
    """Validate a drop_check_constraint operation."""
    if 'table' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'table'")
    if 'name' not in op:
        raise MigrationFileError(f"invalid migration schema: operation {index} missing required field 'name'")

    validate_identifier(op['table'], "table name")
    validate_identifier(op['name'], "constraint name")


def build_column_sql(col: Dict[str, Any]) -> str:
    """Build the SQL for a column definition."""
    parts = [quote_identifier(col['name']), col['type'].upper()]

    if col.get('primary_key', False):
        parts.append('PRIMARY KEY')

    if col.get('auto_increment', False):
        parts.append('AUTOINCREMENT')

    if col.get('not_null', False):
        parts.append('NOT NULL')

    if col.get('unique', False):
        parts.append('UNIQUE')

    if 'default' in col and col['default'] is not None:
        parts.append(f"DEFAULT {col['default']}")

    return ' '.join(parts)


def get_table_info(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """Get information about a table's columns."""
    cursor = conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    columns = []
    for row in cursor.fetchall():
        columns.append({
            'cid': row[0],
            'name': row[1],
            'type': row[2],
            'not_null': bool(row[3]),
            'default': row[4],
            'primary_key': bool(row[5])
        })
    return columns


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table."""
    columns = get_table_info(conn, table_name)
    return any(col['name'] == column_name for col in columns)


def get_foreign_keys(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """Get information about foreign keys in a table."""
    cursor = conn.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})")
    fks = []
    for row in cursor.fetchall():
        fks.append({
            'id': row[0],  # Foreign key ID (for composite FK identification)
            'seq': row[1],  # Sequence number within the foreign key
            'table': row[2],  # Referenced table
            'from': row[3],  # Column in this table
            'to': row[4],  # Referenced column
            'on_update': row[5],
            'on_delete': row[6],
            'match': row[7]
        })
    return fks


def get_indexes(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """Get information about indexes on a table."""
    cursor = conn.execute(f"PRAGMA index_list({quote_identifier(table_name)})")
    indexes = []
    for row in cursor.fetchall():
        index_name = row[1]
        is_unique = bool(row[2])

        # Get columns in the index
        col_cursor = conn.execute(f"PRAGMA index_info({quote_identifier(index_name)})")
        columns = []
        for col_row in col_cursor.fetchall():
            columns.append(col_row[2])  # Column name

        indexes.append({
            'name': index_name,
            'unique': is_unique,
            'columns': columns,
            'origin': row[3] if len(row) > 3 else 'c'  # 'c' for CREATE INDEX, 'pk' for PRIMARY KEY, 'u' for UNIQUE
        })
    return indexes


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    """Check if an index exists in the database."""
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    return cursor.fetchone() is not None


def get_table_sql(conn: sqlite3.Connection, table_name: str) -> Optional[str]:
    """Get the original CREATE TABLE SQL statement."""
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def get_check_constraints(conn: sqlite3.Connection, table_name: str) -> List[Dict[str, Any]]:
    """
    Get check constraints from a table.
    Note: SQLite doesn't have a direct PRAGMA for check constraints.
    We need to parse the CREATE TABLE SQL to find them.
    """
    table_sql = get_table_sql(conn, table_name)
    if not table_sql:
        return []

    constraints = []
    # Parse CHECK constraints from SQL
    # Simple approach: find CHECK keyword and extract expression
    sql_upper = table_sql.upper()

    # Find CHECK constraints - look for patterns like CONSTRAINT name CHECK(...) or CHECK(...)
    i = 0
    while i < len(table_sql):
        check_pos = table_sql.upper().find('CHECK', i)
        if check_pos == -1:
            break

        # Check if this is part of CONSTRAINT name CHECK
        constraint_name = None
        pre_check = table_sql[:check_pos].strip()

        # Look for CONSTRAINT keyword before CHECK - handle both quoted and unquoted names
        constraint_match = re.search(r'CONSTRAINT\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?\s*$', pre_check, re.IGNORECASE)
        if constraint_match:
            constraint_name = constraint_match.group(1)

        # Find the expression inside CHECK(...)
        paren_start = table_sql.find('(', check_pos)
        if paren_start == -1:
            i = check_pos + 5
            continue

        # Find matching closing parenthesis
        depth = 1
        j = paren_start + 1
        while j < len(table_sql) and depth > 0:
            if table_sql[j] == '(':
                depth += 1
            elif table_sql[j] == ')':
                depth -= 1
            j += 1

        expression = table_sql[paren_start+1:j-1].strip()

        # Generate a name if not provided
        if not constraint_name:
            constraint_name = f"chk_{table_name}_{len(constraints)}"

        constraints.append({
            'name': constraint_name,
            'expression': expression
        })

        i = j

    return constraints


def execute_create_table(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a create_table operation."""
    table_name = op['table']

    if table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' already exists")

    column_defs = [build_column_sql(col) for col in op['columns']]
    sql = f"CREATE TABLE {quote_identifier(table_name)} ({', '.join(column_defs)})"

    try:
        conn.execute(sql)
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_add_column(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute an add_column operation."""
    table_name = op['table']
    col = op['column']
    column_name = col['name']

    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    if column_exists(conn, table_name, column_name):
        raise OperationError(f"invalid operation: column '{column_name}' already exists in table '{table_name}'")

    column_sql = build_column_sql(col)
    sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {column_sql}"

    try:
        conn.execute(sql)
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_drop_column(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """
    Execute a drop_column operation.
    SQLite doesn't support DROP COLUMN directly, so we:
    1. Create a new table without the column
    2. Copy data from old table to new table
    3. Drop old table
    4. Rename new table to original name
    """
    table_name = op['table']
    column_name = op['column']

    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    columns = get_table_info(conn, table_name)

    # Check column exists
    column_to_drop = None
    for col in columns:
        if col['name'] == column_name:
            column_to_drop = col
            break

    if column_to_drop is None:
        raise OperationError(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

    # Cannot drop only column
    if len(columns) == 1:
        raise OperationError(f"invalid operation: cannot drop the only column in table '{table_name}'")

    # Cannot drop primary key column
    if column_to_drop['primary_key']:
        raise OperationError(f"invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    # Get columns to keep
    columns_to_keep = [col for col in columns if col['name'] != column_name]

    # Create new table name (temporary)
    temp_table_name = f"_temp_{table_name}_{version}"

    # Build column definitions for new table
    column_defs = []
    for col in columns_to_keep:
        parts = [quote_identifier(col['name']), col['type']]
        if col['primary_key']:
            parts.append('PRIMARY KEY')
            # Check if this was autoincrement - we need to preserve it
            # We'll check the original SQL
        if col['not_null']:
            parts.append('NOT NULL')
        if col['default'] is not None:
            parts.append(f"DEFAULT {col['default']}")
        column_defs.append(' '.join(parts))

    # Check for AUTOINCREMENT on the primary key
    cursor = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    original_sql = cursor.fetchone()
    if original_sql and 'AUTOINCREMENT' in original_sql[0].upper():
        # Add AUTOINCREMENT to the primary key column
        for i, col in enumerate(columns_to_keep):
            if col['primary_key']:
                column_defs[i] = column_defs[i] + ' AUTOINCREMENT'
                break

    try:
        # Create new table
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(column_defs)})"
        conn.execute(create_sql)

        # Copy data
        cols_to_copy = ', '.join(quote_identifier(col['name']) for col in columns_to_keep)
        copy_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_copy} FROM {quote_identifier(table_name)}"
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_transform_data(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a transform_data operation."""
    table_name = op['table']
    transformations = op['transformations']

    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    for transform in transformations:
        column_name = transform['column']
        expression = transform['expression']

        # Check if column exists - it must exist for updates
        if not column_exists(conn, table_name, column_name):
            raise OperationError(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

        # Build the UPDATE SQL
        sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {expression}"

        try:
            conn.execute(sql)
        except sqlite3.Error as e:
            raise SQLError(f"SQL error: {e}")


def execute_migrate_column_data(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a migrate_column_data operation."""
    table_name = op['table']
    from_column = op['from_column']
    to_column = op['to_column']
    default_value = op.get('default_value')

    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    if not column_exists(conn, table_name, from_column):
        raise OperationError(f"invalid operation: column '{from_column}' does not exist in table '{table_name}'")

    if not column_exists(conn, table_name, to_column):
        raise OperationError(f"invalid operation: column '{to_column}' does not exist in table '{table_name}'")

    # Build the UPDATE SQL
    # If default_value is provided, use COALESCE to handle NULL values
    if default_value is not None:
        # Need to properly format the default value for SQL
        if isinstance(default_value, str):
            default_sql = f"'{default_value.replace("'", "''")}'"
        elif default_value is None:
            default_sql = "NULL"
        else:
            default_sql = str(default_value)

        sql = f"""UPDATE {quote_identifier(table_name)}
                  SET {quote_identifier(to_column)} = COALESCE({quote_identifier(from_column)}, {default_sql})"""
    else:
        sql = f"""UPDATE {quote_identifier(table_name)}
                  SET {quote_identifier(to_column)} = {quote_identifier(from_column)}"""

    try:
        conn.execute(sql)
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_backfill_data(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a backfill_data operation."""
    table_name = op['table']
    column_name = op['column']
    value = op['value']
    where_clause = op.get('where')

    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    if not column_exists(conn, table_name, column_name):
        raise OperationError(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

    # Build the UPDATE SQL
    # value is treated as a SQL expression (can be a literal like 'active' or a function)
    sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {value}"

    if where_clause:
        sql += f" WHERE {where_clause}"

    try:
        conn.execute(sql)
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_add_foreign_key(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """
    Execute an add_foreign_key operation.
    SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we must recreate the table.
    """
    table_name = op['table']
    fk_name = op['name']
    columns = op['columns']
    refs = op['references']
    ref_table = refs['table']
    ref_columns = refs['columns']
    on_delete = op.get('on_delete', 'NO ACTION').upper()
    on_update = op.get('on_update', 'NO ACTION').upper()

    # Validate table exists
    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    # Validate referenced table exists
    if not table_exists(conn, ref_table):
        raise OperationError(f"cannot add foreign key: referenced table '{ref_table}' does not exist")

    # Validate all columns exist in referencing table
    for col in columns:
        if not column_exists(conn, table_name, col):
            raise OperationError(f"cannot add foreign key: column '{table_name}.{col}' does not exist")

    # Validate all referenced columns exist
    for col in ref_columns:
        if not column_exists(conn, ref_table, col):
            raise OperationError(f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist")

    # Check if foreign key with same name already exists
    existing_fks = get_foreign_keys(conn, table_name)
    # Group foreign keys by id (for composite FK)
    fk_groups = {}
    for fk in existing_fks:
        if fk['id'] not in fk_groups:
            fk_groups[fk['id']] = []
        fk_groups[fk['id']].append(fk)

    # Check for duplicate name by looking at constraint naming
    # SQLite doesn't store FK names directly, so we check if adding would create a duplicate
    # by checking if the same FK definition already exists
    for fk_id, fk_list in fk_groups.items():
        if len(fk_list) == len(columns):
            existing_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
            existing_ref_table = fk_list[0]['table']
            existing_ref_cols = [fk['to'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
            if existing_cols == columns and existing_ref_table == ref_table and existing_ref_cols == ref_columns:
                raise OperationError(f"foreign key already exists on table '{table_name}'")

    # Get table info
    table_info = get_table_info(conn, table_name)
    existing_fks = get_foreign_keys(conn, table_name)
    existing_indexes = get_indexes(conn, table_name)
    existing_checks = get_check_constraints(conn, table_name)

    # Create temporary table name
    temp_table_name = f"_temp_{table_name}_{version}"

    # Build column definitions
    column_defs = []
    pk_columns = [col for col in table_info if col['primary_key']]

    for col in table_info:
        parts = [quote_identifier(col['name']), col['type']]
        if col['primary_key']:
            parts.append('PRIMARY KEY')
        if col['not_null']:
            parts.append('NOT NULL')
        if col['default'] is not None:
            parts.append(f"DEFAULT {col['default']}")
        column_defs.append(' '.join(parts))

    # Build foreign key constraints
    fk_constraints = []

    # Add existing foreign keys
    for fk_id, fk_list in fk_groups.items():
        fk_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_ref_table = fk_list[0]['table']
        fk_ref_cols = [fk['to'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_on_delete = fk_list[0]['on_delete']
        fk_on_update = fk_list[0]['on_update']

        fk_sql = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_cols)}) REFERENCES {quote_identifier(fk_ref_table)} ({', '.join(quote_identifier(c) for c in fk_ref_cols)})"
        if fk_on_delete != 'NO ACTION':
            fk_sql += f" ON DELETE {fk_on_delete}"
        if fk_on_update != 'NO ACTION':
            fk_sql += f" ON UPDATE {fk_on_update}"
        fk_constraints.append(fk_sql)

    # Add new foreign key
    fk_sql = f"CONSTRAINT {quote_identifier(fk_name)} FOREIGN KEY ({', '.join(quote_identifier(c) for c in columns)}) REFERENCES {quote_identifier(ref_table)} ({', '.join(quote_identifier(c) for c in ref_columns)})"
    if on_delete != 'NO ACTION':
        fk_sql += f" ON DELETE {on_delete}"
    if on_update != 'NO ACTION':
        fk_sql += f" ON UPDATE {on_update}"
    fk_constraints.append(fk_sql)

    # Build check constraints
    check_constraints = []
    for check in existing_checks:
        check_constraints.append(f"CONSTRAINT {quote_identifier(check['name'])} CHECK ({check['expression']})")

    # Combine all constraints
    all_constraints = column_defs + fk_constraints + check_constraints

    try:
        # Create new table with foreign key
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(all_constraints)})"
        conn.execute(create_sql)

        # Copy data
        cols_str = ', '.join(quote_identifier(col['name']) for col in table_info)
        copy_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_str} FROM {quote_identifier(table_name)}"
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

        # Recreate indexes (except auto-generated ones like PRIMARY KEY)
        for idx in existing_indexes:
            if idx['origin'] == 'c':  # Created by CREATE INDEX
                unique_str = 'UNIQUE ' if idx['unique'] else ''
                idx_sql = f"CREATE {unique_str}INDEX {quote_identifier(idx['name'])} ON {quote_identifier(table_name)} ({', '.join(quote_identifier(c) for c in idx['columns'])})"
                conn.execute(idx_sql)

    except sqlite3.Error as e:
        # Check for foreign key violation
        if 'FOREIGN KEY' in str(e).upper() or 'foreign key' in str(e).lower():
            raise SQLError(f"foreign key violation: {e}")
        raise SQLError(f"SQL error: {e}")


def execute_drop_foreign_key(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """
    Execute a drop_foreign_key operation.
    SQLite doesn't support ALTER TABLE DROP CONSTRAINT, so we must recreate the table.
    """
    table_name = op['table']
    fk_name = op['name']

    # Validate table exists
    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    # Get existing foreign keys
    existing_fks = get_foreign_keys(conn, table_name)

    # Group foreign keys by id
    fk_groups = {}
    for fk in existing_fks:
        if fk['id'] not in fk_groups:
            fk_groups[fk['id']] = []
        fk_groups[fk['id']].append(fk)

    # Find the foreign key to drop
    # Note: SQLite doesn't store FK names in PRAGMA, so we parse from SQL
    table_sql = get_table_sql(conn, table_name)
    fk_to_drop_id = None

    # Look for CONSTRAINT fk_name in the CREATE TABLE SQL
    if table_sql:
        # Find the constraint with matching name
        pattern = rf'CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY'
        if re.search(pattern, table_sql, re.IGNORECASE):
            # Find which FK this corresponds to by analyzing columns
            # This is complex - for now, we'll try to match by checking each FK
            for fk_id, fk_list in fk_groups.items():
                fk_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
                # Build pattern to match this FK with the constraint name
                fk_pattern = rf'CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY\s*\(\s*{r"\s*,\s*".join(map(re.escape, fk_cols))}\s*\)'
                if re.search(fk_pattern, table_sql, re.IGNORECASE):
                    fk_to_drop_id = fk_id
                    break

    # If not found by name, check if there's only one FK
    if fk_to_drop_id is None:
        if len(fk_groups) == 1:
            fk_to_drop_id = list(fk_groups.keys())[0]
        else:
            raise OperationError(f"foreign key '{fk_name}' does not exist in table '{table_name}'")

    # Get table info
    table_info = get_table_info(conn, table_name)
    existing_indexes = get_indexes(conn, table_name)
    existing_checks = get_check_constraints(conn, table_name)

    # Create temporary table name
    temp_table_name = f"_temp_{table_name}_{version}"

    # Build column definitions
    column_defs = []
    for col in table_info:
        parts = [quote_identifier(col['name']), col['type']]
        if col['primary_key']:
            parts.append('PRIMARY KEY')
        if col['not_null']:
            parts.append('NOT NULL')
        if col['default'] is not None:
            parts.append(f"DEFAULT {col['default']}")
        column_defs.append(' '.join(parts))

    # Build foreign key constraints (excluding the one to drop)
    fk_constraints = []
    for fk_id, fk_list in fk_groups.items():
        if fk_id == fk_to_drop_id:
            continue
        fk_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_ref_table = fk_list[0]['table']
        fk_ref_cols = [fk['to'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_on_delete = fk_list[0]['on_delete']
        fk_on_update = fk_list[0]['on_update']

        fk_sql = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_cols)}) REFERENCES {quote_identifier(fk_ref_table)} ({', '.join(quote_identifier(c) for c in fk_ref_cols)})"
        if fk_on_delete != 'NO ACTION':
            fk_sql += f" ON DELETE {fk_on_delete}"
        if fk_on_update != 'NO ACTION':
            fk_sql += f" ON UPDATE {fk_on_update}"
        fk_constraints.append(fk_sql)

    # Build check constraints
    check_constraints = []
    for check in existing_checks:
        check_constraints.append(f"CONSTRAINT {quote_identifier(check['name'])} CHECK ({check['expression']})")

    # Combine all constraints
    all_constraints = column_defs + fk_constraints + check_constraints

    try:
        # Create new table without the foreign key
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(all_constraints)})"
        conn.execute(create_sql)

        # Copy data
        cols_str = ', '.join(quote_identifier(col['name']) for col in table_info)
        copy_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_str} FROM {quote_identifier(table_name)}"
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

        # Recreate indexes
        for idx in existing_indexes:
            if idx['origin'] == 'c':
                unique_str = 'UNIQUE ' if idx['unique'] else ''
                idx_sql = f"CREATE {unique_str}INDEX {quote_identifier(idx['name'])} ON {quote_identifier(table_name)} ({', '.join(quote_identifier(c) for c in idx['columns'])})"
                conn.execute(idx_sql)

    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_create_index(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a create_index operation."""
    index_name = op['name']
    table_name = op['table']
    columns = op['columns']
    is_unique = op.get('unique', False)

    # Validate table exists
    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    # Validate all columns exist
    for col in columns:
        if not column_exists(conn, table_name, col):
            raise OperationError(f"invalid operation: column '{col}' does not exist in table '{table_name}'")

    # Check if index already exists
    if index_exists(conn, index_name):
        raise OperationError(f"index '{index_name}' already exists")

    # Build CREATE INDEX SQL
    unique_str = 'UNIQUE ' if is_unique else ''
    cols_str = ', '.join(quote_identifier(c) for c in columns)
    sql = f"CREATE {unique_str}INDEX {quote_identifier(index_name)} ON {quote_identifier(table_name)} ({cols_str})"

    try:
        conn.execute(sql)
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_drop_index(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a drop_index operation."""
    index_name = op['name']

    # Check if index exists
    if not index_exists(conn, index_name):
        raise OperationError(f"index '{index_name}' does not exist")

    try:
        conn.execute(f"DROP INDEX {quote_identifier(index_name)}")
    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def execute_add_check_constraint(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """
    Execute an add_check_constraint operation.
    SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we must recreate the table.
    """
    table_name = op['table']
    constraint_name = op['name']
    expression = op['expression']

    # Validate table exists
    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    # Check if constraint with same name already exists
    existing_checks = get_check_constraints(conn, table_name)
    if any(c['name'] == constraint_name for c in existing_checks):
        raise OperationError(f"check constraint '{constraint_name}' already exists in table '{table_name}'")

    # Get table info
    table_info = get_table_info(conn, table_name)
    existing_fks = get_foreign_keys(conn, table_name)
    existing_indexes = get_indexes(conn, table_name)

    # Group foreign keys
    fk_groups = {}
    for fk in existing_fks:
        if fk['id'] not in fk_groups:
            fk_groups[fk['id']] = []
        fk_groups[fk['id']].append(fk)

    # Create temporary table name
    temp_table_name = f"_temp_{table_name}_{version}"

    # Build column definitions
    column_defs = []
    for col in table_info:
        parts = [quote_identifier(col['name']), col['type']]
        if col['primary_key']:
            parts.append('PRIMARY KEY')
        if col['not_null']:
            parts.append('NOT NULL')
        if col['default'] is not None:
            parts.append(f"DEFAULT {col['default']}")
        column_defs.append(' '.join(parts))

    # Build foreign key constraints
    fk_constraints = []
    for fk_id, fk_list in fk_groups.items():
        fk_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_ref_table = fk_list[0]['table']
        fk_ref_cols = [fk['to'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_on_delete = fk_list[0]['on_delete']
        fk_on_update = fk_list[0]['on_update']

        fk_sql = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_cols)}) REFERENCES {quote_identifier(fk_ref_table)} ({', '.join(quote_identifier(c) for c in fk_ref_cols)})"
        if fk_on_delete != 'NO ACTION':
            fk_sql += f" ON DELETE {fk_on_delete}"
        if fk_on_update != 'NO ACTION':
            fk_sql += f" ON UPDATE {fk_on_update}"
        fk_constraints.append(fk_sql)

    # Build check constraints (existing + new)
    check_constraints = []
    for check in existing_checks:
        check_constraints.append(f"CONSTRAINT {quote_identifier(check['name'])} CHECK ({check['expression']})")

    # Add new check constraint
    check_constraints.append(f"CONSTRAINT {quote_identifier(constraint_name)} CHECK ({expression})")

    # Combine all constraints
    all_constraints = column_defs + fk_constraints + check_constraints

    try:
        # Create new table with check constraint
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(all_constraints)})"
        conn.execute(create_sql)

        # Copy data (this will validate the check constraint)
        cols_str = ', '.join(quote_identifier(col['name']) for col in table_info)
        copy_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_str} FROM {quote_identifier(table_name)}"
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

        # Recreate indexes
        for idx in existing_indexes:
            if idx['origin'] == 'c':
                unique_str = 'UNIQUE ' if idx['unique'] else ''
                idx_sql = f"CREATE {unique_str}INDEX {quote_identifier(idx['name'])} ON {quote_identifier(table_name)} ({', '.join(quote_identifier(c) for c in idx['columns'])})"
                conn.execute(idx_sql)

    except sqlite3.Error as e:
        if 'CHECK constraint' in str(e) or 'constraint failed' in str(e).lower():
            raise SQLError(f"check constraint violation: {e}")
        raise SQLError(f"SQL error: {e}")


def execute_drop_check_constraint(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """
    Execute a drop_check_constraint operation.
    SQLite doesn't support ALTER TABLE DROP CONSTRAINT, so we must recreate the table.
    """
    table_name = op['table']
    constraint_name = op['name']

    # Validate table exists
    if not table_exists(conn, table_name):
        raise OperationError(f"invalid operation: table '{table_name}' does not exist")

    # Get existing check constraints
    existing_checks = get_check_constraints(conn, table_name)

    # Find the constraint to drop
    constraint_to_drop = None
    for check in existing_checks:
        if check['name'] == constraint_name:
            constraint_to_drop = check
            break

    if constraint_to_drop is None:
        raise OperationError(f"check constraint '{constraint_name}' does not exist in table '{table_name}'")

    # Get table info
    table_info = get_table_info(conn, table_name)
    existing_fks = get_foreign_keys(conn, table_name)
    existing_indexes = get_indexes(conn, table_name)

    # Group foreign keys
    fk_groups = {}
    for fk in existing_fks:
        if fk['id'] not in fk_groups:
            fk_groups[fk['id']] = []
        fk_groups[fk['id']].append(fk)

    # Create temporary table name
    temp_table_name = f"_temp_{table_name}_{version}"

    # Build column definitions
    column_defs = []
    for col in table_info:
        parts = [quote_identifier(col['name']), col['type']]
        if col['primary_key']:
            parts.append('PRIMARY KEY')
        if col['not_null']:
            parts.append('NOT NULL')
        if col['default'] is not None:
            parts.append(f"DEFAULT {col['default']}")
        column_defs.append(' '.join(parts))

    # Build foreign key constraints
    fk_constraints = []
    for fk_id, fk_list in fk_groups.items():
        fk_cols = [fk['from'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_ref_table = fk_list[0]['table']
        fk_ref_cols = [fk['to'] for fk in sorted(fk_list, key=lambda x: x['seq'])]
        fk_on_delete = fk_list[0]['on_delete']
        fk_on_update = fk_list[0]['on_update']

        fk_sql = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_cols)}) REFERENCES {quote_identifier(fk_ref_table)} ({', '.join(quote_identifier(c) for c in fk_ref_cols)})"
        if fk_on_delete != 'NO ACTION':
            fk_sql += f" ON DELETE {fk_on_delete}"
        if fk_on_update != 'NO ACTION':
            fk_sql += f" ON UPDATE {fk_on_update}"
        fk_constraints.append(fk_sql)

    # Build check constraints (excluding the one to drop)
    check_constraints = []
    for check in existing_checks:
        if check['name'] != constraint_name:
            check_constraints.append(f"CONSTRAINT {quote_identifier(check['name'])} CHECK ({check['expression']})")

    # Combine all constraints
    all_constraints = column_defs + fk_constraints + check_constraints

    try:
        # Create new table without the check constraint
        create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} ({', '.join(all_constraints)})"
        conn.execute(create_sql)

        # Copy data
        cols_str = ', '.join(quote_identifier(col['name']) for col in table_info)
        copy_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_str} FROM {quote_identifier(table_name)}"
        conn.execute(copy_sql)

        # Drop old table
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        conn.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

        # Recreate indexes
        for idx in existing_indexes:
            if idx['origin'] == 'c':
                unique_str = 'UNIQUE ' if idx['unique'] else ''
                idx_sql = f"CREATE {unique_str}INDEX {quote_identifier(idx['name'])} ON {quote_identifier(table_name)} ({', '.join(quote_identifier(c) for c in idx['columns'])})"
                conn.execute(idx_sql)

    except sqlite3.Error as e:
        raise SQLError(f"SQL error: {e}")


def output_event(event: Dict[str, Any]) -> None:
    """Output an event as a JSON line to stdout."""
    print(json.dumps(event), flush=True)


def get_applied_migrations(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Get all applied migrations from the database."""
    cursor = conn.execute(
        "SELECT version, description, operations FROM _migrations ORDER BY version"
    )
    migrations = []
    for row in cursor.fetchall():
        ops_data = json.loads(row[2]) if row[2] else {}
        # Handle both old format (just array) and new format (object with operations key)
        if isinstance(ops_data, list):
            operations = ops_data
            rollback_ops = None
        else:
            operations = ops_data.get('operations', [])
            rollback_ops = ops_data.get('rollback_operations')

        migrations.append({
            'version': row[0],
            'description': row[1],
            'operations': operations,
            'rollback_operations': rollback_ops
        })
    return migrations


def get_current_version(conn: sqlite3.Connection) -> int:
    """Get the current (highest) migration version."""
    cursor = conn.execute("SELECT MAX(version) FROM _migrations")
    row = cursor.fetchone()
    return row[0] if row[0] is not None else 0


def generate_rollback_operations(operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Generate rollback operations from forward operations (reverse order)."""
    rollback_ops = []

    for op in reversed(operations):
        op_type = op['type']

        if op_type == 'create_table':
            # Rollback: Drop the table
            rollback_ops.append({
                'type': 'drop_table',
                'table': op['table']
            })
        elif op_type == 'add_column':
            # Rollback: Drop the column
            rollback_ops.append({
                'type': 'drop_column',
                'table': op['table'],
                'column': op['column']['name']
            })
        elif op_type == 'drop_column':
            # Rollback: Re-add the column (if original definition stored)
            if 'original_definition' in op:
                rollback_ops.append({
                    'type': 'add_column',
                    'table': op['table'],
                    'column': op['original_definition']
                })
            else:
                # Without original definition, create column as TEXT with nullable
                rollback_ops.append({
                    'type': 'add_column',
                    'table': op['table'],
                    'column': {
                        'name': op['column'],
                        'type': 'TEXT',
                        'not_null': False
                    }
                })
        elif op_type == 'add_foreign_key':
            # Rollback: Drop the foreign key
            rollback_ops.append({
                'type': 'drop_foreign_key',
                'table': op['table'],
                'name': op['name']
            })
        elif op_type == 'drop_foreign_key':
            # Rollback: Re-add the foreign key (if original definition stored)
            if 'original_definition' in op:
                rollback_ops.append(op['original_definition'])
            else:
                # Cannot automatically restore without original definition
                raise MigrationError(f"cannot rollback: missing original_definition for drop_foreign_key")
        elif op_type == 'create_index':
            # Rollback: Drop the index
            rollback_ops.append({
                'type': 'drop_index',
                'name': op['name']
            })
        elif op_type == 'drop_index':
            # Rollback: Re-create the index (if original definition stored)
            if 'original_definition' in op:
                rollback_ops.append(op['original_definition'])
            else:
                raise MigrationError(f"cannot rollback: missing original_definition for drop_index")
        elif op_type == 'add_check_constraint':
            # Rollback: Drop the check constraint
            rollback_ops.append({
                'type': 'drop_check_constraint',
                'table': op['table'],
                'name': op['name']
            })
        elif op_type == 'drop_check_constraint':
            # Rollback: Re-add the check constraint (if original definition stored)
            if 'original_definition' in op:
                rollback_ops.append(op['original_definition'])
            else:
                raise MigrationError(f"cannot rollback: missing original_definition for drop_check_constraint")
        elif op_type == 'transform_data':
            # Cannot be automatically rolled back
            raise MigrationError(f"cannot rollback: transform_data requires explicit rollback_operations")
        elif op_type == 'backfill_data':
            # Cannot be automatically rolled back
            raise MigrationError(f"cannot rollback: backfill_data requires explicit rollback_operations")
        elif op_type == 'migrate_column_data':
            # Rollback: Reverse the migration (copy from to_column back to from_column)
            rollback_ops.append({
                'type': 'migrate_column_data',
                'table': op['table'],
                'from_column': op['to_column'],
                'to_column': op['from_column'],
                'default_value': op.get('default_value')
            })

    return rollback_ops


def execute_rollback_drop_table(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a drop_table operation (rollback of create_table)."""
    table_name = op['table']

    if not table_exists(conn, table_name):
        raise OperationError(f"rollback failed: table '{table_name}' does not exist")

    try:
        conn.execute(f"DROP TABLE {quote_identifier(table_name)}")
    except sqlite3.Error as e:
        if 'FOREIGN KEY' in str(e).upper() or 'foreign key' in str(e).lower():
            raise SQLError(f"cannot rollback: foreign key constraint violation")
        raise SQLError(f"SQL error: {e}")


def execute_rollback_drop_column(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute drop_column operation (rollback of add_column). Uses existing drop_column logic."""
    execute_drop_column(conn, op, version)


def execute_rollback_add_column(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute add_column operation (rollback of drop_column). Uses existing add_column logic."""
    execute_add_column(conn, op, version)


def execute_rollback_drop_foreign_key(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute drop_foreign_key operation (rollback of add_foreign_key)."""
    execute_drop_foreign_key(conn, op, version)


def execute_rollback_add_foreign_key(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute add_foreign_key operation (rollback of drop_foreign_key)."""
    execute_add_foreign_key(conn, op, version)


def execute_rollback_drop_index(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute drop_index operation (rollback of create_index)."""
    execute_drop_index(conn, op, version)


def execute_rollback_create_index(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute create_index operation (rollback of drop_index)."""
    execute_create_index(conn, op, version)


def execute_rollback_drop_check_constraint(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute drop_check_constraint operation (rollback of add_check_constraint)."""
    execute_drop_check_constraint(conn, op, version)


def execute_rollback_add_check_constraint(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute add_check_constraint operation (rollback of drop_check_constraint)."""
    execute_add_check_constraint(conn, op, version)


def execute_rollback_migrate_column_data(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute migrate_column_data operation (reversed for rollback)."""
    execute_migrate_column_data(conn, op, version)


def execute_rollback_operation(conn: sqlite3.Connection, op: Dict[str, Any], version: int) -> None:
    """Execute a rollback operation based on its type."""
    op_type = op['type']

    if op_type == 'drop_table':
        execute_rollback_drop_table(conn, op, version)
    elif op_type == 'drop_column':
        execute_rollback_drop_column(conn, op, version)
    elif op_type == 'add_column':
        execute_rollback_add_column(conn, op, version)
    elif op_type == 'drop_foreign_key':
        execute_rollback_drop_foreign_key(conn, op, version)
    elif op_type == 'add_foreign_key':
        execute_rollback_add_foreign_key(conn, op, version)
    elif op_type == 'drop_index':
        execute_rollback_drop_index(conn, op, version)
    elif op_type == 'create_index':
        execute_rollback_create_index(conn, op, version)
    elif op_type == 'drop_check_constraint':
        execute_rollback_drop_check_constraint(conn, op, version)
    elif op_type == 'add_check_constraint':
        execute_rollback_add_check_constraint(conn, op, version)
    elif op_type == 'migrate_column_data':
        execute_rollback_migrate_column_data(conn, op, version)
    else:
        raise OperationError(f"unknown rollback operation type '{op_type}'")


def output_rollback_operation_event(op: Dict[str, Any], version: int) -> None:
    """Output an event for a rolled back operation."""
    op_type = op['type']
    event = {
        'event': 'operation_rolled_back',
        'type': op_type,
        'version': version
    }

    if 'table' in op:
        event['table'] = op['table']
    if 'column' in op:
        event['column'] = op['column']
    if 'name' in op:
        event['name'] = op['name']
    if 'from_column' in op:
        event['from_column'] = op['from_column']
    if 'to_column' in op:
        event['to_column'] = op['to_column']

    output_event(event)


def rollback_single_migration(conn: sqlite3.Connection, migration: Dict[str, Any],
                               rollback_operations: Optional[List[Dict[str, Any]]] = None) -> None:
    """Rollback a single migration."""
    version = migration['version']
    description = migration['description']
    operations = migration['operations']

    output_event({
        'event': 'rollback_started',
        'version': version,
        'description': description
    })

    # Use provided rollback operations, or stored rollback_operations, or generate automatic ones
    if rollback_operations is not None:
        ops_to_execute = rollback_operations
    elif migration.get('rollback_operations') is not None:
        ops_to_execute = migration['rollback_operations']
    else:
        ops_to_execute = generate_rollback_operations(operations)

    # Execute rollback operations in order
    for op in ops_to_execute:
        execute_rollback_operation(conn, op, version)
        output_rollback_operation_event(op, version)

    output_event({
        'event': 'rollback_complete',
        'version': version
    })


def rollback_migrations(db_path: str, to_version: Optional[int], count: Optional[int]) -> None:
    """Rollback migrations from the database."""
    # Connect to database
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        print(f"Error: cannot connect to database: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Enable foreign keys
        conn.execute("PRAGMA foreign_keys = ON")

        # Check if _migrations table exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_migrations'"
        )
        if cursor.fetchone() is None:
            print("Error: no migrations to rollback", file=sys.stderr)
            sys.exit(1)

        # Get applied migrations
        applied = get_applied_migrations(conn)

        if len(applied) == 0:
            print("Error: no migrations to rollback", file=sys.stderr)
            sys.exit(1)

        current_version = get_current_version(conn)

        # Determine which versions to rollback
        versions_to_rollback = []

        if to_version is not None:
            # Rollback to specific version (exclusive)
            if to_version < 0:
                print(f"Error: version {to_version} not found", file=sys.stderr)
                sys.exit(1)

            # Check if target version exists
            applied_versions = [m['version'] for m in applied]
            if to_version > 0 and to_version not in applied_versions:
                print(f"Error: version {to_version} not found", file=sys.stderr)
                sys.exit(1)

            # Get all versions above to_version
            versions_to_rollback = [m['version'] for m in applied if m['version'] > to_version]
        else:
            # Rollback by count (default 1)
            rollback_count = count if count is not None else 1

            if rollback_count <= 0:
                print("Error: count must be a positive integer", file=sys.stderr)
                sys.exit(1)

            if rollback_count > len(applied):
                print(f"Error: cannot rollback {rollback_count} migrations, only {len(applied)} applied", file=sys.stderr)
                sys.exit(1)

            # Get the last N migrations
            versions_to_rollback = [m['version'] for m in applied[-rollback_count:]]

        if len(versions_to_rollback) == 0:
            print("Error: no migrations to rollback", file=sys.stderr)
            sys.exit(1)

        # Sort versions in reverse order (rollback highest first)
        versions_to_rollback.sort(reverse=True)

        # Get migration data for versions to rollback
        migrations_to_rollback = []
        for v in versions_to_rollback:
            for m in applied:
                if m['version'] == v:
                    migrations_to_rollback.append(m)
                    break

        # Start transaction
        try:
            conn.execute("BEGIN TRANSACTION")

            rolled_back_versions = []

            for migration in migrations_to_rollback:
                version = migration['version']

                # Check if migration file has explicit rollback_operations
                # We need to load the original migration file to check
                # For now, we'll use automatic rollback
                rollback_single_migration(conn, migration)
                rolled_back_versions.append(version)

                # Remove migration from _migrations table
                conn.execute("DELETE FROM _migrations WHERE version = ?", (version,))

            # Commit the transaction
            conn.commit()

            # Calculate final version
            final_version = get_current_version(conn)

            output_event({
                'event': 'rollback_finished',
                'versions_rolled_back': rolled_back_versions,
                'final_version': final_version
            })

        except MigrationError as e:
            conn.rollback()
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except (OperationError, SQLError) as e:
            conn.rollback()
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.Error as e:
            conn.rollback()
            if 'FOREIGN KEY' in str(e).upper() or 'foreign key' in str(e).lower():
                print(f"Error: cannot rollback: foreign key constraint violation", file=sys.stderr)
            else:
                print(f"Error: SQL error: {e}", file=sys.stderr)
            sys.exit(1)

    finally:
        conn.close()


def apply_migration(migration_path: str, db_path: str) -> None:
    """Apply a migration to a database."""
    # Load and validate migration file
    try:
        migration = load_migration_file(migration_path)
    except MigrationFileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate schema
    try:
        validate_migration_schema(migration)
    except MigrationFileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    version = migration['version']
    description = migration['description']
    operations = migration['operations']

    # Connect to database (creates if doesn't exist)
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        print(f"Error: cannot connect to database: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        # Enable foreign keys for migration
        conn.execute("PRAGMA foreign_keys = ON")

        # Create _migrations table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operations TEXT NOT NULL DEFAULT '[]'
            )
        """)
        conn.commit()

        # Check if migration already applied
        cursor = conn.execute(
            "SELECT version FROM _migrations WHERE version = ?",
            (version,)
        )
        if cursor.fetchone() is not None:
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            output_event({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            })
            return

        # Execute operations within a transaction
        ops_count = 0
        try:
            # Start transaction
            conn.execute("BEGIN TRANSACTION")

            for op in operations:
                op_type = op['type']

                if op_type == 'create_table':
                    execute_create_table(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "create_table",
                        "table": op['table'],
                        "version": version
                    })
                elif op_type == 'add_column':
                    execute_add_column(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "add_column",
                        "table": op['table'],
                        "column": op['column']['name'],
                        "version": version
                    })
                elif op_type == 'drop_column':
                    execute_drop_column(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "drop_column",
                        "table": op['table'],
                        "column": op['column'],
                        "version": version
                    })
                elif op_type == 'transform_data':
                    execute_transform_data(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "transform_data",
                        "table": op['table'],
                        "transformations_count": len(op['transformations']),
                        "version": version
                    })
                elif op_type == 'migrate_column_data':
                    execute_migrate_column_data(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "migrate_column_data",
                        "table": op['table'],
                        "from_column": op['from_column'],
                        "to_column": op['to_column'],
                        "version": version
                    })
                elif op_type == 'backfill_data':
                    execute_backfill_data(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "backfill_data",
                        "table": op['table'],
                        "column": op['column'],
                        "version": version
                    })
                elif op_type == 'add_foreign_key':
                    execute_add_foreign_key(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "add_foreign_key",
                        "table": op['table'],
                        "name": op['name'],
                        "version": version
                    })
                elif op_type == 'drop_foreign_key':
                    execute_drop_foreign_key(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "drop_foreign_key",
                        "table": op['table'],
                        "name": op['name'],
                        "version": version
                    })
                elif op_type == 'create_index':
                    execute_create_index(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "create_index",
                        "name": op['name'],
                        "table": op['table'],
                        "version": version
                    })
                elif op_type == 'drop_index':
                    execute_drop_index(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "drop_index",
                        "name": op['name'],
                        "version": version
                    })
                elif op_type == 'add_check_constraint':
                    execute_add_check_constraint(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "add_check_constraint",
                        "table": op['table'],
                        "name": op['name'],
                        "version": version
                    })
                elif op_type == 'drop_check_constraint':
                    execute_drop_check_constraint(conn, op, version)
                    output_event({
                        "event": "operation_applied",
                        "type": "drop_check_constraint",
                        "table": op['table'],
                        "name": op['name'],
                        "version": version
                    })
                ops_count += 1

            # Record migration with operations JSON (include rollback_operations if present)
            migration_data = {
                'operations': operations
            }
            if 'rollback_operations' in migration:
                migration_data['rollback_operations'] = migration['rollback_operations']
            operations_json = json.dumps(migration_data)
            conn.execute(
                "INSERT INTO _migrations (version, description, operations) VALUES (?, ?, ?)",
                (version, description, operations_json)
            )

            # Commit the transaction
            conn.commit()

            output_event({
                "event": "migration_complete",
                "version": version,
                "operations_count": ops_count
            })

        except (OperationError, SQLError) as e:
            # Rollback on error
            conn.rollback()
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.Error as e:
            # Rollback on SQL error
            conn.rollback()
            print(f"Error: SQL error: {e}", file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Database Migration Tool'
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    # rollback command
    rollback_parser = subparsers.add_parser('rollback', help='Rollback migrations')
    rollback_parser.add_argument('database', help='Path to SQLite database file')
    rollback_parser.add_argument('--to-version', type=int, help='Rollback to a specific version (exclusive)')
    rollback_parser.add_argument('--count', type=int, help='Number of migrations to rollback (default: 1)')

    args = parser.parse_args()

    if args.command == 'migrate':
        apply_migration(args.migration_file, args.database)
    elif args.command == 'rollback':
        rollback_migrations(args.database, args.to_version, args.count)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
