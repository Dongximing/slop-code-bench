#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 1: Basic Schema Migrations

A CLI tool that applies database schema migrations to SQLite databases from JSON migration files.
"""

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


# Valid SQLite data types
VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}

# Regex for valid SQLite identifiers (alphanumeric + underscore, starting with letter/underscore)
VALID_IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


class ValidationError(Exception):
    """Raised when validation fails."""
    pass


class MigrationError(Exception):
    """Raised when migration operation fails."""
    pass


def validate_identifier(name: str, identifier_type: str = "identifier") -> None:
    """Validate that a name is a valid SQLite identifier."""
    if not VALID_IDENTIFIER_PATTERN.match(name):
        raise ValidationError(
            f"Invalid {identifier_type}: '{name}'. Must be alphanumeric with underscores, starting with letter or underscore."
        )


def escape_identifier(name: str) -> str:
    """Escape an identifier for safe SQL usage."""
    return f'"{name}"'


def check_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    # Initialize _migrations table if it doesn't exist (with operations column for rollback support)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operations TEXT NOT NULL DEFAULT '[]'
        )
    ''')

    cursor.execute('SELECT version FROM _migrations WHERE version = ?', (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str, operations: List[Dict[str, Any]] = None) -> None:
    """Record a successfully applied migration."""
    operations_json = json.dumps(operations) if operations is not None else '[]'
    cursor.execute(
        'INSERT INTO _migrations (version, description, operations) VALUES (?, ?, ?)',
        (version, description, operations_json)
    )


def get_table_columns(cursor: sqlite3.Cursor, table: str) -> Dict[str, Dict[str, Any]]:
    """Get column information for a table."""
    cursor.execute(f"PRAGMA table_info({escape_identifier(table)})")
    columns = {}
    for row in cursor.fetchall():
        # row: cid, name, type, notnull, default_value, pk
        columns[row[1]] = {
            'cid': row[0],
            'type': row[2],
            'not_null': bool(row[3]),
            'default': row[4],
            'primary_key': bool(row[5])
        }
    return columns


def get_migration_info(cursor: sqlite3.Cursor, version: int) -> Optional[Dict[str, Any]]:
    """Get migration information including stored operations."""
    cursor.execute('SELECT description, operations FROM _migrations WHERE version = ?', (version,))
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        'version': version,
        'description': row[0],
        'operations': json.loads(row[1])
    }


def get_applied_migrations(cursor: sqlite3.Cursor) -> List[Dict[str, Any]]:
    """Get all applied migrations in order."""
    cursor.execute('SELECT version, description, operations FROM _migrations ORDER BY version ASC')
    migrations = []
    for row in cursor.fetchall():
        migrations.append({
            'version': row[0],
            'description': row[1],
            'operations': json.loads(row[2])
        })
    return migrations


def get_primary_key_column(cursor: sqlite3.Cursor, table: str) -> Optional[str]:
    """Get the name of the primary key column for a table, or None if none."""
    columns = get_table_columns(cursor, table)
    for name, info in columns.items():
        if info['primary_key']:
            return name
    return None


def table_exists(cursor: sqlite3.Cursor, table: str) -> bool:
    """Check if a table exists."""
    cursor.execute('''
        SELECT name FROM sqlite_master
        WHERE type='table' AND name=?
    ''', (table,))
    return cursor.fetchone() is not None


def column_exists(cursor: sqlite3.Cursor, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    columns = get_table_columns(cursor, table)
    return column in columns


def index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    """Check if an index exists."""
    cursor.execute('''
        SELECT name FROM sqlite_master
        WHERE type='index' AND name=?
    ''', (index_name,))
    return cursor.fetchone() is not None


def get_foreign_keys(cursor: sqlite3.Cursor, table: str) -> List[Dict[str, Any]]:
    """Get foreign key information for a table."""
    cursor.execute(f"PRAGMA foreign_key_list({escape_identifier(table)})")
    fks = []
    for row in cursor.fetchall():
        # row: id, seq, table, from, to, on_update, on_delete, match
        fks.append({
            'id': row[0],
            'seq': row[1],
            'table': row[2],
            'from': row[3],
            'to': row[4],
            'on_update': row[5],
            'on_delete': row[6],
            'match': row[7]
        })
    return fks


def get_table_sql(cursor: sqlite3.Cursor, table: str) -> Optional[str]:
    """Get the CREATE TABLE SQL statement for a table."""
    cursor.execute('''
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name=?
    ''', (table,))
    result = cursor.fetchone()
    return result[0] if result else None


def get_check_constraints(cursor: sqlite3.Cursor, table: str) -> List[Dict[str, Any]]:
    """Get check constraints for a table by parsing the CREATE TABLE statement."""
    sql = get_table_sql(cursor, table)
    if not sql:
        return []

    constraints = []
    # Look for CHECK constraints in the SQL
    # Simple parsing: find CHECK (expression) patterns
    import re
    # Pattern to find CHECK constraints with optional name
    pattern = r'CONSTRAINT\s+(\w+)\s+CHECK\s*\(([^)]+)\)|CHECK\s*\(([^)]+)\)'
    for match in re.finditer(pattern, sql, re.IGNORECASE):
        if match.group(1):  # Named constraint
            constraints.append({
                'name': match.group(1),
                'expression': match.group(2).strip()
            })
        elif match.group(3):  # Unnamed constraint
            constraints.append({
                'name': None,
                'expression': match.group(3).strip()
            })
    return constraints


def validate_column_spec(column: Dict[str, Any], allow_primary_key: bool = True) -> None:
    """Validate a column specification."""
    name = column.get('name')
    if not name:
        raise ValidationError("Column specification missing required field 'name'")
    validate_identifier(name, "column name")

    column_type = column.get('type')
    if not column_type:
        raise ValidationError(f"Column '{name}' missing required field 'type'")
    if column_type.upper() not in VALID_TYPES:
        raise ValidationError(
            f"Column '{name}' has invalid type '{column_type}'. Must be one of: INTEGER, TEXT, REAL, BLOB, TIMESTAMP."
        )

    primary_key = column.get('primary_key', False)
    auto_increment = column.get('auto_increment', False)
    not_null = column.get('not_null', False)
    unique = column.get('unique', False)
    default = column.get('default')

    # Validate constraints
    if auto_increment and column_type.upper() != 'INTEGER':
        raise ValidationError(
            f"Column '{name}' has auto_increment=True but type is '{column_type}'. auto_increment only valid for INTEGER columns."
        )

    if auto_increment and not primary_key:
        raise ValidationError(
            f"Column '{name}' has auto_increment=True but primary_key=False. auto_increment requires primary_key=True."
        )

    # Note: unique constraint will be added as part of the column definition
    # or via separate ALTER TABLE statement

    return {
        'name': name,
        'type': column_type.upper(),
        'primary_key': primary_key,
        'auto_increment': auto_increment,
        'not_null': not_null,
        'unique': unique,
        'default': default
    }


def validate_create_table_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a create_table operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("create_table operation missing required field 'table'")
    validate_identifier(table, "table name")

    columns = op.get('columns')
    if not columns or not isinstance(columns, list):
        raise ValidationError("create_table operation missing required field 'columns' or it's not a list")

    validated_columns = []
    primary_key_count = 0

    for col_spec in columns:
        col = validate_column_spec(col_spec)
        if col['primary_key']:
            primary_key_count += 1
        validated_columns.append(col)

    if primary_key_count > 1:
        raise ValidationError("create_table operation: at most one column can have primary_key=True")

    return {
        'type': 'create_table',
        'table': table,
        'columns': validated_columns
    }


def validate_add_column_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an add_column operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("add_column operation missing required field 'table'")
    validate_identifier(table, "table name")

    column = op.get('column')
    if not column or not isinstance(column, dict):
        raise ValidationError("add_column operation missing required field 'column' or it's not an object")

    validated_column = validate_column_spec(column)

    return {
        'type': 'add_column',
        'table': table,
        'column': validated_column
    }


def validate_drop_column_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a drop_column operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("drop_column operation missing required field 'table'")
    validate_identifier(table, "table name")

    column = op.get('column')
    if not column:
        raise ValidationError("drop_column operation missing required field 'column'")
    validate_identifier(column, "column name")

    return {
        'type': 'drop_column',
        'table': table,
        'column': column
    }


def validate_transform_data_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a transform_data operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("transform_data operation missing required field 'table'")
    validate_identifier(table, "table name")

    transformations = op.get('transformations')
    if not transformations or not isinstance(transformations, list):
        raise ValidationError("transform_data operation missing required field 'transformations' or it's not a list")

    if len(transformations) == 0:
        raise ValidationError("transform_data operation 'transformations' array cannot be empty")

    validated_transformations = []
    for i, t in enumerate(transformations):
        if not isinstance(t, dict):
            raise ValidationError(f"transform_data transformation {i} is not a JSON object")

        column = t.get('column')
        if not column:
            raise ValidationError(f"transform_data transformation {i} missing required field 'column'")
        validate_identifier(column, "column name")

        expression = t.get('expression')
        if not expression or not isinstance(expression, str):
            raise ValidationError(f"transform_data transformation {i} missing required field 'expression' or it's not a string")

        validated_transformations.append({
            'column': column,
            'expression': expression
        })

    return {
        'type': 'transform_data',
        'table': table,
        'transformations': validated_transformations
    }


def validate_migrate_column_data_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a migrate_column_data operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("migrate_column_data operation missing required field 'table'")
    validate_identifier(table, "table name")

    from_column = op.get('from_column')
    if not from_column:
        raise ValidationError("migrate_column_data operation missing required field 'from_column'")
    validate_identifier(from_column, "from_column name")

    to_column = op.get('to_column')
    if not to_column:
        raise ValidationError("migrate_column_data operation missing required field 'to_column'")
    validate_identifier(to_column, "to_column name")

    default_value = op.get('default_value')

    return {
        'type': 'migrate_column_data',
        'table': table,
        'from_column': from_column,
        'to_column': to_column,
        'default_value': default_value
    }


def validate_backfill_data_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a backfill_data operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("backfill_data operation missing required field 'table'")
    validate_identifier(table, "table name")

    column = op.get('column')
    if not column:
        raise ValidationError("backfill_data operation missing required field 'column'")
    validate_identifier(column, "column name")

    value = op.get('value')
    if value is None:
        raise ValidationError("backfill_data operation missing required field 'value'")

    where = op.get('where')
    if where is not None and not isinstance(where, str):
        raise ValidationError("backfill_data operation 'where' must be a string")

    return {
        'type': 'backfill_data',
        'table': table,
        'column': column,
        'value': value,
        'where': where
    }


def validate_add_foreign_key_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an add_foreign_key operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("add_foreign_key operation missing required field 'table'")
    validate_identifier(table, "table name")

    name = op.get('name')
    if not name:
        raise ValidationError("add_foreign_key operation missing required field 'name'")
    validate_identifier(name, "foreign key name")

    columns = op.get('columns')
    if not columns or not isinstance(columns, list):
        raise ValidationError("add_foreign_key operation missing required field 'columns' or it's not a list")
    if len(columns) == 0:
        raise ValidationError("add_foreign_key operation 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            raise ValidationError("add_foreign_key operation 'columns' must be an array of strings")
        validate_identifier(col, "column name")

    references = op.get('references')
    if not references or not isinstance(references, dict):
        raise ValidationError("add_foreign_key operation missing required field 'references' or it's not an object")

    ref_table = references.get('table')
    if not ref_table:
        raise ValidationError("add_foreign_key operation 'references' missing required field 'table'")
    validate_identifier(ref_table, "referenced table name")

    ref_columns = references.get('columns')
    if not ref_columns or not isinstance(ref_columns, list):
        raise ValidationError("add_foreign_key operation 'references' missing required field 'columns' or it's not a list")
    if len(ref_columns) != len(columns):
        raise ValidationError("add_foreign_key operation: number of columns must match number of referenced columns")
    for col in ref_columns:
        if not isinstance(col, str):
            raise ValidationError("add_foreign_key operation 'references.columns' must be an array of strings")
        validate_identifier(col, "referenced column name")

    on_delete = op.get('on_delete')
    valid_on_actions = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}
    if on_delete is not None:
        if on_delete.upper() not in valid_on_actions:
            raise ValidationError(f"add_foreign_key operation: invalid on_delete value '{on_delete}'. Must be one of: CASCADE, RESTRICT, SET NULL, NO ACTION, SET DEFAULT")
        on_delete = on_delete.upper()

    on_update = op.get('on_update')
    if on_update is not None:
        if on_update.upper() not in valid_on_actions:
            raise ValidationError(f"add_foreign_key operation: invalid on_update value '{on_update}'. Must be one of: CASCADE, RESTRICT, SET NULL, NO ACTION, SET DEFAULT")
        on_update = on_update.upper()

    return {
        'type': 'add_foreign_key',
        'table': table,
        'name': name,
        'columns': columns,
        'references': {
            'table': ref_table,
            'columns': ref_columns
        },
        'on_delete': on_delete,
        'on_update': on_update
    }


def validate_drop_foreign_key_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a drop_foreign_key operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("drop_foreign_key operation missing required field 'table'")
    validate_identifier(table, "table name")

    name = op.get('name')
    if not name:
        raise ValidationError("drop_foreign_key operation missing required field 'name'")
    validate_identifier(name, "foreign key name")

    return {
        'type': 'drop_foreign_key',
        'table': table,
        'name': name
    }


def validate_create_index_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a create_index operation."""
    name = op.get('name')
    if not name:
        raise ValidationError("create_index operation missing required field 'name'")
    validate_identifier(name, "index name")

    table = op.get('table')
    if not table:
        raise ValidationError("create_index operation missing required field 'table'")
    validate_identifier(table, "table name")

    columns = op.get('columns')
    if not columns or not isinstance(columns, list):
        raise ValidationError("create_index operation missing required field 'columns' or it's not a list")
    if len(columns) == 0:
        raise ValidationError("create_index operation 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            raise ValidationError("create_index operation 'columns' must be an array of strings")
        validate_identifier(col, "column name")

    unique = op.get('unique', False)
    if not isinstance(unique, bool):
        raise ValidationError("create_index operation 'unique' must be a boolean")

    return {
        'type': 'create_index',
        'name': name,
        'table': table,
        'columns': columns,
        'unique': unique
    }


def validate_drop_index_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a drop_index operation."""
    name = op.get('name')
    if not name:
        raise ValidationError("drop_index operation missing required field 'name'")
    validate_identifier(name, "index name")

    return {
        'type': 'drop_index',
        'name': name
    }


def validate_add_check_constraint_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate an add_check_constraint operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("add_check_constraint operation missing required field 'table'")
    validate_identifier(table, "table name")

    name = op.get('name')
    if not name:
        raise ValidationError("add_check_constraint operation missing required field 'name'")
    validate_identifier(name, "constraint name")

    expression = op.get('expression')
    if not expression or not isinstance(expression, str):
        raise ValidationError("add_check_constraint operation missing required field 'expression' or it's not a string")

    return {
        'type': 'add_check_constraint',
        'table': table,
        'name': name,
        'expression': expression
    }


def validate_drop_check_constraint_operation(op: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a drop_check_constraint operation."""
    table = op.get('table')
    if not table:
        raise ValidationError("drop_check_constraint operation missing required field 'table'")
    validate_identifier(table, "table name")

    name = op.get('name')
    if not name:
        raise ValidationError("drop_check_constraint operation missing required field 'name'")
    validate_identifier(name, "constraint name")

    return {
        'type': 'drop_check_constraint',
        'table': table,
        'name': name
    }


def validate_migration_schema(migration: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the overall migration schema."""
    if not isinstance(migration, dict):
        raise ValidationError("Migration must be a JSON object")

    version = migration.get('version')
    if version is None:
        raise ValidationError("Migration missing required field 'version'")
    if not isinstance(version, int) or version < 1:
        raise ValidationError("Migration 'version' must be a positive integer")

    description = migration.get('description')
    if description is None:
        raise ValidationError("Migration missing required field 'description'")
    if not isinstance(description, str):
        raise ValidationError("Migration 'description' must be a string")

    operations = migration.get('operations')
    if not operations or not isinstance(operations, list):
        raise ValidationError("Migration missing required field 'operations' or it's not a list")
    if len(operations) == 0:
        raise ValidationError("Migration 'operations' array cannot be empty")

    validated_operations = []
    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            raise ValidationError(f"Operation {i} is not a JSON object")

        op_type = op.get('type')
        if not op_type:
            raise ValidationError(f"Operation {i} missing required field 'type'")

        if op_type == 'create_table':
            validated_operations.append(validate_create_table_operation(op))
        elif op_type == 'add_column':
            validated_operations.append(validate_add_column_operation(op))
        elif op_type == 'drop_column':
            validated_operations.append(validate_drop_column_operation(op))
        elif op_type == 'transform_data':
            validated_operations.append(validate_transform_data_operation(op))
        elif op_type == 'migrate_column_data':
            validated_operations.append(validate_migrate_column_data_operation(op))
        elif op_type == 'backfill_data':
            validated_operations.append(validate_backfill_data_operation(op))
        elif op_type == 'add_foreign_key':
            validated_operations.append(validate_add_foreign_key_operation(op))
        elif op_type == 'drop_foreign_key':
            validated_operations.append(validate_drop_foreign_key_operation(op))
        elif op_type == 'create_index':
            validated_operations.append(validate_create_index_operation(op))
        elif op_type == 'drop_index':
            validated_operations.append(validate_drop_index_operation(op))
        elif op_type == 'add_check_constraint':
            validated_operations.append(validate_add_check_constraint_operation(op))
        elif op_type == 'drop_check_constraint':
            validated_operations.append(validate_drop_check_constraint_operation(op))
        else:
            raise ValidationError(f"Operation {i} has invalid type '{op_type}'. Must be: create_table, add_column, drop_column, transform_data, migrate_column_data, backfill_data, add_foreign_key, drop_foreign_key, create_index, drop_index, add_check_constraint, drop_check_constraint")

    return {
        'version': version,
        'description': description,
        'operations': validated_operations
    }


def build_column_definition(col: Dict[str, Any], use_double_quotes: bool = False) -> str:
    """Build a SQL column definition string."""
    parts = []

    # Column name
    parts.append(f'"{col["name"]}"')

    # Column type
    parts.append(col['type'])

    # NOT NULL constraint
    if col['not_null']:
        parts.append('NOT NULL')

    # UNIQUE constraint
    if col['unique']:
        parts.append('UNIQUE')

    # DEFAULT constraint
    if col['default'] is not None:
        parts.append(f'DEFAULT {col["default"]}')

    # PRIMARY KEY constraint
    if col['primary_key']:
        parts.append('PRIMARY KEY')

    # AUTOINCREMENT for INTEGER primary key
    if col['auto_increment']:
        parts.append('AUTOINCREMENT')

    return ' '.join(parts)


def execute_create_table(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Execute a create_table operation and return output lines."""
    table = op['table']
    columns = op['columns']

    # Check if table already exists
    if table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' already exists")

    # Build CREATE TABLE statement
    column_defs = [build_column_definition(col, True) for col in columns]
    sql = f'CREATE TABLE "{table}" ({", ".join(column_defs)})'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "create_table", "table": "{table}", "version": {cursor.arraysize}}}']


def execute_add_column(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Execute an add_column operation and return output lines."""
    table = op['table']
    column = op['column']
    column_name = column['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if column already exists
    if column_exists(cursor, table, column_name):
        raise MigrationError(f"Invalid operation: column '{column_name}' already exists in table '{table}'")

    # Build ALTER TABLE ADD COLUMN statement
    column_def = build_column_definition(column, True)
    sql = f'ALTER TABLE "{table}" ADD COLUMN {column_def}'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "add_column", "table": "{table}", "column": "{column_name}", "version": {cursor.arraysize}}}']


def execute_drop_column(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute a drop_column operation and return output lines."""
    table = op['table']
    column_name = op['column']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if column exists
    if not column_exists(cursor, table, column_name):
        raise MigrationError(f"Invalid operation: column '{column_name}' does not exist in table '{table}'")

    # Get table columns
    columns = get_table_columns(cursor, table)

    # Check for edge case: cannot drop the only column in the table
    if len(columns) <= 1:
        raise MigrationError(f"Invalid operation: cannot drop column '{column_name}' from table '{table}' as it's the only column")

    # Check if dropping the primary key column
    primary_key = get_primary_key_column(cursor, table)
    if primary_key == column_name:
        raise MigrationError(f"Invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    # Create new table name
    new_table_name = f"{table}_temp_{version}"

    # Build the list of columns to keep
    columns_to_keep = [col for col in columns if col != column_name]

    # Build CREATE TABLE statement for new table
    column_defs = []
    for col_name, col_info in [(name, columns[name]) for name in columns_to_keep]:
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,  # SQLite doesn't support UNIQUE in PRAGMA table_info the same way
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        col_defs.append(build_column_definition(col_def, True))

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table without the column
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        source_cols = [f'"{col}"' for col in columns_to_keep]
        target_cols = [f'"{col}"' for col in columns_to_keep]

        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(source_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "drop_column", "table": "{table}", "column": "{column_name}", "version": {version}}}']


def execute_transform_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Execute a transform_data operation and return output lines."""
    table = op['table']
    transformations = op['transformations']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Get columns to verify they exist (or will be created)
    columns = get_table_columns(cursor, table)

    output_lines = []

    for t in transformations:
        column = t['column']
        expression = t['expression']

        # Check if column exists, if not we'll need to add it first
        # For transform_data, the column must exist or we need to add it separately
        if column not in columns:
            raise MigrationError(f"Invalid operation: column '{column}' does not exist in table '{table}'")

        # Build UPDATE statement
        sql = f'UPDATE "{table}" SET "{column}" = ({expression})'

        try:
            cursor.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error: {e}")

        output_lines.append(f'{{"event": "operation_applied", "type": "transform_data", "table": "{table}", "column": "{column}", "rows_affected": {cursor.rowcount}}}')

    return output_lines


def execute_migrate_column_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Execute a migrate_column_data operation and return output lines."""
    table = op['table']
    from_column = op['from_column']
    to_column = op['to_column']
    default_value = op.get('default_value')

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if both columns exist
    columns = get_table_columns(cursor, table)

    if from_column not in columns:
        raise MigrationError(f"Invalid operation: column '{from_column}' does not exist in table '{table}'")

    if to_column not in columns:
        raise MigrationError(f"Invalid operation: column '{to_column}' does not exist in table '{table}'")

    # Build UPDATE statement
    if default_value is not None:
        # Use COALESCE to handle NULL values with default
        # Convert default_value to appropriate SQL representation
        if isinstance(default_value, str):
            default_sql = f"'{default_value}'"
        elif isinstance(default_value, bool):
            default_sql = '1' if default_value else '0'
        elif default_value is None:
            default_sql = 'NULL'
        else:
            default_sql = str(default_value)

        sql = f'UPDATE "{table}" SET "{to_column}" = COALESCE("{from_column}", {default_sql})'
    else:
        # Simply copy from from_column to to_column, NULL values will be NULL
        sql = f'UPDATE "{table}" SET "{to_column}" = "{from_column}"'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "migrate_column_data", "table": "{table}", "from_column": "{from_column}", "to_column": "{to_column}", "rows_affected": {cursor.rowcount}}}']


def execute_backfill_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Execute a backfill_data operation and return output lines."""
    table = op['table']
    column = op['column']
    value = op['value']
    where = op.get('where')

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if column exists
    columns = get_table_columns(cursor, table)
    if column not in columns:
        raise MigrationError(f"Invalid operation: column '{column}' does not exist in table '{table}'")

    # Build UPDATE statement
    # Convert value to appropriate SQL representation
    if isinstance(value, str):
        value_sql = f"'{value}'"
    elif isinstance(value, bool):
        value_sql = '1' if value else '0'
    elif value is None:
        value_sql = 'NULL'
    else:
        value_sql = str(value)

    sql = f'UPDATE "{table}" SET "{column}" = {value_sql}'

    if where:
        sql += f' WHERE {where}'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "backfill_data", "table": "{table}", "column": "{column}", "rows_affected": {cursor.rowcount}}}']


def execute_add_foreign_key(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute an add_foreign_key operation and return output lines."""
    table = op['table']
    name = op['name']
    columns = op['columns']
    references = op['references']
    on_delete = op.get('on_delete')
    on_update = op.get('on_update')

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if referenced table exists
    if not table_exists(cursor, references['table']):
        raise MigrationError(f"Invalid operation: referenced table '{references['table']}' does not exist")

    # Check if columns exist in the table
    for col in columns:
        if not column_exists(cursor, table, col):
            raise MigrationError(f"Invalid operation: column '{col}' does not exist in table '{table}'")

    # Check if referenced columns exist
    for col in references['columns']:
        if not column_exists(cursor, references['table'], col):
            raise MigrationError(f"Invalid operation: referenced column '{col}' does not exist in table '{references['table']}'")

    # Check if foreign key already exists
    existing_fks = get_foreign_keys(cursor, table)
    for fk_info in existing_fks:
        # Get all columns for this FK by sequence
        fk_columns = []
        for seq_fk in existing_fks:
            if seq_fk['id'] == fk_info['id']:
                fk_columns.append(seq_fk['from'])
        if set(fk_columns) == set(columns):
            raise MigrationError(f"Invalid operation: foreign key on columns '{columns}' already exists in table '{table}'")

    # For SQLite, need to recreate table to add foreign key
    new_table_name = f"{table}_temp_{version}"

    # Get current table structure
    table_cols = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in table_cols.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    # Add the FOREIGN KEY constraint
    cols = [f'"{col}"' for col in columns]
    ref_cols = [f'"{col}"' for col in references['columns']]
    fk_clause = f'FOREIGN KEY ({", ".join(cols)}) REFERENCES "{references["table"]}" ({", ".join(ref_cols)})'

    if on_delete:
        fk_clause += f' ON DELETE {on_delete}'
    if on_update:
        fk_clause += f' ON UPDATE {on_update}'

    column_defs.append(fk_clause)

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table with the foreign key
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in table_cols.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "add_foreign_key", "table": "{table}", "name": "{name}", "version": {version}}}']


def execute_add_check_constraint(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute an add_check_constraint operation and return output lines."""
    table = op['table']
    name = op['name']
    expression = op['expression']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if constraint already exists
    existing_constraints = get_check_constraints(cursor, table)
    for constraint in existing_constraints:
        if constraint['name'] == name:
            raise MigrationError(f"Invalid operation: check constraint '{name}' already exists in table '{table}'")

    # For SQLite, need to recreate table to add check constraint
    new_table_name = f"{table}_temp_{version}"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    # Add the CHECK constraint
    column_defs.append(f'CONSTRAINT "{name}" CHECK ({expression})')

    create_sql = f'CREATE TABLE "{new_table_name}" (", ".join(column_defs))'

    try:
        # 1. Create new table with the check constraint
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "add_check_constraint", "table": "{table}", "name": "{name}", "version": {version}}}']


def execute_drop_foreign_key(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute a drop_foreign_key operation and return output lines."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Get table SQL to find the foreign key constraint
    table_sql = get_table_sql(cursor, table)
    if not table_sql:
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Find foreign key constraint in the SQL
    import re
    # Look for CONSTRAINT name FOREIGN KEY ... pattern
    pattern = rf'CONSTRAINT\s+{re.escape(name)}\s+FOREIGN\s+KEY\s*\([^)]+\)\s+REFERENCES\s+[^ ]+\s*\([^)]+\)(?:\s+ON\s+DELETE\s+\w+)?(?:\s+ON\s+UPDATE\s+\w+)?'
    match = re.search(pattern, table_sql, re.IGNORECASE)

    if not match:
        # Try to find it in PRAGMA foreign_key_list
        existing_fks = get_foreign_keys(cursor, table)
        found = False
        for fk in existing_fks:
            # Check if constraint name matches
            # SQLite doesn't store constraint names in foreign_key_list, so we'll rely on the pattern match
            # For simplicity, if we can't find by name, check if any FK exists with these columns
            found = True
            break
        if not found:
            raise MigrationError(f"Invalid operation: foreign key constraint '{name}' does not exist in table '{table}'")

    # For SQLite, need to recreate table to drop foreign key
    new_table_name = f"{table}_temp_{version}"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table without the foreign key
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "drop_foreign_key", "table": "{table}", "name": "{name}", "version": {version}}}']


def execute_create_index(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute a create_index operation and return output lines."""
    name = op['name']
    table = op['table']
    columns = op['columns']
    unique = op.get('unique', False)

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if index already exists
    if index_exists(cursor, name):
        raise MigrationError(f"Invalid operation: index '{name}' already exists")

    # Check if columns exist
    for col in columns:
        if not column_exists(cursor, table, col):
            raise MigrationError(f"Invalid operation: column '{col}' does not exist in table '{table}'")

    # Build CREATE INDEX statement
    cols = [f'"{col}"' for col in columns]
    sql = f'CREATE {'UNIQUE ' if unique else ''}INDEX "{name}" ON "{table}"({", ".join(cols)})'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "create_index", "name": "{name}", "table": "{table}", "version": {version}}}']


def execute_drop_index(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute a drop_index operation and return output lines."""
    name = op['name']

    # Check if index exists
    if not index_exists(cursor, name):
        raise MigrationError(f"Invalid operation: index '{name}' does not exist")

    # Build DROP INDEX statement
    sql = f'DROP INDEX "{name}"'

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "drop_index", "name": "{name}", "version": {version}}}']


def execute_add_check_constraint(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute an add_check_constraint operation and return output lines."""
    table = op['table']
    name = op['name']
    expression = op['expression']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if constraint already exists
    existing_constraints = get_check_constraints(cursor, table)
    for constraint in existing_constraints:
        if constraint['name'] == name:
            raise MigrationError(f"Invalid operation: check constraint '{name}' already exists in table '{table}'")

    # For SQLite, need to recreate table to add check constraint
    new_table_name = f"{table}_temp_{version}"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    # Add the CHECK constraint
    column_defs.append(f'CONSTRAINT "{name}" CHECK ({expression})')

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table with the check constraint
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "add_check_constraint", "table": "{table}", "name": "{name}", "version": {version}}}']


def execute_drop_check_constraint(cursor: sqlite3.Cursor, op: Dict[str, Any], version: int) -> List[str]:
    """Execute a drop_check_constraint operation and return output lines."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if constraint exists
    existing_constraints = get_check_constraints(cursor, table)
    found = False
    for constraint in existing_constraints:
        if constraint['name'] == name:
            found = True
            break

    if not found:
        raise MigrationError(f"Invalid operation: check constraint '{name}' does not exist in table '{table}'")

    # For SQLite, need to recreate table to drop check constraint
    new_table_name = f"{table}_temp_{version}"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table without the check constraint
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_applied", "type": "drop_check_constraint", "table": "{table}", "name": "{name}", "version": {version}}}']


def apply_migration(conn: sqlite3.Connection, migration: Dict[str, Any]) -> None:
    """Apply a migration to the database."""
    version = migration['version']
    description = migration['description']
    operations = migration['operations']

    cursor = conn.cursor()

    # Check if migration already applied
    if check_migration_applied(cursor, version):
        print(f'{{"event": "migration_skipped", "version": {version}, "reason": "already_applied"}}')
        print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
        conn.commit()
        return

    # Enable foreign keys at the start
    # Some operations need them OFF temporarily (table recreation)
    cursor.execute('PRAGMA foreign_keys = ON')

    output_lines = []

    try:
        for op in operations:
            op_type = op['type']

            # Operations that need table recreation need foreign_keys OFF
            if op_type in ('drop_column', 'drop_foreign_key', 'add_check_constraint', 'drop_check_constraint'):
                cursor.execute('PRAGMA foreign_keys = OFF')

            if op_type == 'create_table':
                lines = execute_create_table(cursor, op)
            elif op_type == 'add_column':
                lines = execute_add_column(cursor, op)
            elif op_type == 'drop_column':
                lines = execute_drop_column(cursor, op, version)
            elif op_type == 'transform_data':
                lines = execute_transform_data(cursor, op)
            elif op_type == 'migrate_column_data':
                lines = execute_migrate_column_data(cursor, op)
            elif op_type == 'backfill_data':
                lines = execute_backfill_data(cursor, op)
            elif op_type == 'add_foreign_key':
                lines = execute_add_foreign_key(cursor, op, version)
            elif op_type == 'drop_foreign_key':
                lines = execute_drop_foreign_key(cursor, op, version)
            elif op_type == 'create_index':
                lines = execute_create_index(cursor, op, version)
            elif op_type == 'drop_index':
                lines = execute_drop_index(cursor, op, version)
            elif op_type == 'add_check_constraint':
                lines = execute_add_check_constraint(cursor, op, version)
            elif op_type == 'drop_check_constraint':
                lines = execute_drop_check_constraint(cursor, op, version)
            else:
                raise MigrationError(f"Unknown operation type: {op_type}")

            # Re-enable foreign_keys after table recreation operations
            if op_type in ('drop_column', 'drop_foreign_key', 'add_check_constraint', 'drop_check_constraint'):
                cursor.execute('PRAGMA foreign_keys = ON')

            # Update version in output lines
            for i, line in enumerate(lines):
                if '"version":' in line:
                    lines[i] = line.replace('"version": 1}', '"version": ' + str(version) + '}')
                else:
                    # Add version if not present (for drop_column which adds it manually)
                    if line.strip().endswith('}'):
                        lines[i] = line.rstrip('}') + ', "version": ' + str(version) + '}'
            output_lines.extend(lines)

        # Record successful migration with operations for rollback
        record_migration(cursor, version, description, operations)

        # Output migration completion
        operations_count = len(operations)
        output_lines.append(f'{{"event": "migration_complete", "version": {version}, "operations_count": {operations_count}}}')

        # Commit transaction
        conn.commit()

        # Print all output lines
        for line in output_lines:
            print(line)

    except Exception as e:
        conn.rollback()
        raise


def load_migration_file(filepath: str) -> Dict[str, Any]:
    """Load and parse a migration file."""
    path = Path(filepath)

    if not path.exists():
        raise FileNotFoundError(f"migration file not found: {filepath}")

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValidationError(f"Invalid JSON in migration file: {e}")

    return validate_migration_schema(data)


# ============================================================================
# ROLLBACK FUNCTIONS
# ============================================================================


def rollback_create_table(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a create_table operation by dropping the table."""
    table = op['table']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: table '{table}' does not exist")

    try:
        cursor.execute(f'DROP TABLE "{table}"')
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "create_table", "table": "{table}"}}']


def rollback_add_column(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback an add_column operation by dropping the column."""
    table = op['table']
    column_name = op['column']['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if column exists
    if not column_exists(cursor, table, column_name):
        raise MigrationError(f"Invalid operation: column '{column_name}' does not exist in table '{table}'")

    try:
        cursor.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column_name}"')
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "add_column", "table": "{table}", "column": "{column_name}"}}']


def rollback_drop_column(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a drop_column operation by re-adding the column."""
    table = op['table']
    column_name = op['column']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if column already exists
    if column_exists(cursor, table, column_name):
        raise MigrationError(f"Invalid operation: column '{column_name}' already exists in table '{table}'")

    # Get original column definition from stored metadata
    original_definition = op.get('original_definition')
    if not original_definition:
        raise MigrationError(
            f"Cannot rollback drop_column: missing original_definition for column '{column_name}'. "
            f"Include explicit rollback_operations in migration file."
        )

    try:
        column_def = build_column_definition(original_definition, True)
        cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN {column_def}')
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "drop_column", "table": "{table}", "column": "{column_name}"}}']


def rollback_transform_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a transform_data operation - NOT SUPPORTED."""
    table = op['table']
    raise MigrationError(
        f"Cannot rollback transform_data operation on table '{table}'. "
        f"Include explicit rollback_operations in migration file."
    )


def rollback_migrate_column_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a migrate_column_data operation by reversing the migration."""
    table = op['table']
    from_column = op['from_column']
    to_column = op['to_column']
    default_value = op.get('default_value')

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if columns exist
    columns = get_table_columns(cursor, table)
    if from_column not in columns:
        raise MigrationError(f"Invalid operation: column '{from_column}' does not exist in table '{table}'")
    if to_column not in columns:
        raise MigrationError(f"Invalid operation: column '{to_column}' does not exist in table '{table}'")

    # For rollback: copy from to_column back to from_column
    try:
        if default_value is not None:
            if isinstance(default_value, str):
                default_sql = f"'{default_value}'"
            elif isinstance(default_value, bool):
                default_sql = '1' if default_value else '0'
            elif default_value is None:
                default_sql = 'NULL'
            else:
                default_sql = str(default_value)
            sql = f'UPDATE "{table}" SET "{from_column}" = COALESCE("{to_column}", {default_sql})'
        else:
            sql = f'UPDATE "{table}" SET "{from_column}" = "{to_column}"'
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "migrate_column_data", "table": "{table}", "from_column": "{from_column}", "to_column": "{to_column}"}}']


def rollback_backfill_data(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a backfill_data operation - NOT SUPPORTED."""
    table = op['table']
    column = op['column']
    raise MigrationError(
        f"Cannot rollback backfill_data operation on table '{table}' column '{column}'. "
        f"Include explicit rollback_operations in migration file."
    )


def rollback_add_foreign_key(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback an add_foreign_key operation by dropping the foreign key."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Check if foreign key exists and get its definition for potential use
    existing_fks = get_foreign_keys(cursor, table)
    if not existing_fks:
        raise MigrationError(f"Invalid operation: foreign key constraint '{name}' does not exist in table '{table}'")

    # For SQLite, need to recreate table to drop foreign key
    # We need the original table structure to recreate it
    raise MigrationError(
        f"Rollback for add_foreign_key requires restoring table structure. "
        f"Include explicit rollback_operations in migration file."
    )


def rollback_drop_foreign_key(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a drop_foreign_key operation by re-adding the foreign key."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Get original foreign key definition from stored metadata
    original_fk = op.get('original_definition')
    if not original_fk:
        raise MigrationError(
            f"Cannot rollback drop_foreign_key: missing original_definition for constraint '{name}'. "
            f"Include explicit rollback_operations in migration file."
        )

    # Need to recreate table with the foreign key
    new_table_name = f"{table}_temp_rollback"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    # Add the FOREIGN KEY constraint
    cols = [f'"{col}"' for col in original_fk['columns']]
    ref_cols = [f'"{col}"' for col in original_fk['references']['columns']]
    fk_clause = f'FOREIGN KEY ({", ".join(cols)}) REFERENCES "{original_fk["references"]["table"]}" ({", ".join(ref_cols)})'

    if original_fk.get('on_delete'):
        fk_clause += f' ON DELETE {original_fk["on_delete"]}'
    if original_fk.get('on_update'):
        fk_clause += f' ON UPDATE {original_fk["on_update"]}'

    column_defs.append(fk_clause)

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table with the foreign key
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "drop_foreign_key", "table": "{table}", "name": "{name}"}}']


def rollback_create_index(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a create_index operation by dropping the index."""
    name = op['name']

    # Check if index exists
    if not index_exists(cursor, name):
        raise MigrationError(f"Invalid operation: index '{name}' does not exist")

    try:
        cursor.execute(f'DROP INDEX "{name}"')
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "create_index", "name": "{name}"}}']


def rollback_drop_index(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a drop_index operation by re-creating the index."""
    name = op['name']

    # Check if index already exists
    if index_exists(cursor, name):
        raise MigrationError(f"Invalid operation: index '{name}' already exists")

    # Get original index definition from stored metadata
    original_definition = op.get('original_definition')
    if not original_definition:
        raise MigrationError(
            f"Cannot rollback drop_index: missing original_definition for index '{name}'. "
            f"Include explicit rollback_operations in migration file."
        )

    table = original_definition['table']
    columns = original_definition['columns']
    unique = original_definition.get('unique', False)

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"SQL error: no such table: {table}")

    # Check if columns exist
    for col in columns:
        if not column_exists(cursor, table, col):
            raise MigrationError(f"Invalid operation: column '{col}' does not exist in table '{table}'")

    try:
        cols = [f'"{col}"' for col in columns]
        sql = f'CREATE {'UNIQUE ' if unique else ''}INDEX "{name}" ON "{table}"({", ".join(cols)})'
        cursor.execute(sql)
    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "drop_index", "name": "{name}"}}']


def rollback_add_check_constraint(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback an add_check_constraint operation by dropping the constraint."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # For SQLite, need to recreate table to drop check constraint
    raise MigrationError(
        f"Rollback for add_check_constraint requires restoring table structure. "
        f"Include explicit rollback_operations in migration file."
    )


def rollback_drop_check_constraint(cursor: sqlite3.Cursor, op: Dict[str, Any]) -> List[str]:
    """Rollback a drop_check_constraint operation by re-adding the constraint."""
    table = op['table']
    name = op['name']

    # Check if table exists
    if not table_exists(cursor, table):
        raise MigrationError(f"Invalid operation: table '{table}' does not exist")

    # Get original constraint definition from stored metadata
    original_definition = op.get('original_definition')
    if not original_definition:
        raise MigrationError(
            f"Cannot rollback drop_check_constraint: missing original_definition for constraint '{name}'. "
            f"Include explicit rollback_operations in migration file."
        )

    expression = original_definition['expression']

    # For SQLite, need to recreate table to add check constraint
    new_table_name = f"{table}_temp_rollback"

    # Get current table structure
    columns = get_table_columns(cursor, table)
    column_defs = []
    for col_name, col_info in columns.items():
        col_def = {
            'name': col_name,
            'type': col_info['type'],
            'not_null': col_info['not_null'],
            'unique': False,
            'default': col_info['default'],
            'primary_key': col_info['primary_key'],
            'auto_increment': col_info.get('auto_increment', False)
        }
        column_defs.append(build_column_definition(col_def, True))

    # Add the CHECK constraint
    column_defs.append(f'CONSTRAINT "{name}" CHECK ({expression})')

    create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(column_defs)})'

    try:
        # 1. Create new table with the check constraint
        cursor.execute(create_sql)

        # 2. Copy data from old table to new table
        all_cols = [f'"{col}"' for col in columns.keys()]
        copy_sql = f'INSERT INTO "{new_table_name}" SELECT {", ".join(all_cols)} FROM "{table}"'
        cursor.execute(copy_sql)

        # 3. Drop old table
        cursor.execute(f'DROP TABLE "{table}"')

        # 4. Rename new table to original name
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table}"')

    except sqlite3.Error as e:
        raise MigrationError(f"SQL error: {e}")

    return [f'{{"event": "operation_rolled_back", "type": "drop_check_constraint", "table": "{table}", "name": "{name}"}}']



def rollback_migration(conn: sqlite3.Connection, version: int,
                       explicit_rollback_ops: Dict[int, List[Dict[str, Any]]] = None) -> None:
    """
    Rollback a single migration by its version.

    Args:
        conn: Database connection
        version: Migration version to rollback
        explicit_rollback_ops: Optional mapping of version -> explicit rollback operations
    """
    cursor = conn.cursor()

    # Check if migration exists
    migration_info = get_migration_info(cursor, version)
    if migration_info is None:
        raise MigrationError(f"version {version} not found")

    description = migration_info['description']
    operations = migration_info['operations']

    # Get explicit rollback operations if provided, otherwise generate automatic
    override_ops = explicit_rollback_ops.get(version) if explicit_rollback_ops else None
    rollback_ops = get_rollback_operations(operations, override_ops)

    output_lines = []

    try:
        # Start transaction
        cursor.execute('PRAGMA foreign_keys = OFF')

        # Output rollback started event
        output_lines.append(
            f'{{"event": "rollback_started", "version": {version}, "description": "{description}"}}'
        )

        # Execute rollback operations in reverse order
        for op in rollback_ops:
            op_type = op['type']

            # Each rollback operation directly performs the reverse
            if op_type == 'drop_table':
                lines = rollback_create_table(cursor, op)
            elif op_type == 'drop_column':
                lines = rollback_add_column(cursor, op)
            elif op_type == 'add_column':
                lines = rollback_drop_column(cursor, op)
            elif op_type == 'migrate_column_data':
                lines = rollback_migrate_column_data(cursor, op)
            elif op_type == 'drop_foreign_key':
                lines = rollback_add_foreign_key(cursor, op)
            elif op_type == 'add_foreign_key':
                lines = rollback_drop_foreign_key(cursor, op)
            elif op_type == 'drop_index':
                lines = rollback_create_index(cursor, op)
            elif op_type == 'add_index':
                lines = rollback_drop_index(cursor, op)
            elif op_type == 'drop_check_constraint':
                lines = rollback_add_check_constraint(cursor, op)
            elif op_type == 'add_check_constraint':
                lines = rollback_drop_check_constraint(cursor, op)
            else:
                raise MigrationError(f"Unknown rollback operation type: {op_type}")

            output_lines.extend(lines)

        # Output rollback complete event
        output_lines.append(f'{{"event": "rollback_complete", "version": {version}}}')

        # Remove from _migrations table
        cursor.execute('DELETE FROM _migrations WHERE version = ?', (version,))

        # Commit transaction
        conn.commit()

        # Output all lines
        for line in output_lines:
            print(line)

    except Exception as e:
        conn.rollback()
        raise
    finally:
        cursor.execute('PRAGMA foreign_keys = ON')


def execute_rollback(conn: sqlite3.Connection,
                     to_version: int = None,
                     count: int = 1,
                     explicit_rollback_ops: Dict[int, List[Dict[str, Any]]] = None) -> None:
    """
    Execute rollback of one or more migrations.

    Args:
        conn: Database connection
        to_version: Rollback to this version (exclusive). If None, use count.
        count: Number of migrations to rollback (default: 1)
        explicit_rollback_ops: Optional mapping of version -> explicit rollback operations
    """
    cursor = conn.cursor()

    # Check if any migrations have been applied
    applied_migrations = get_applied_migrations(cursor)
    if not applied_migrations:
        raise MigrationError("no migrations to rollback")

    # Determine which versions to rollback
    if to_version is not None:
        # Find all versions greater than to_version
        versions_to_rollback = [m['version'] for m in applied_migrations if m['version'] > to_version]
        if not versions_to_rollback:
            raise MigrationError(f"version {to_version} not found or all migrations are at or below this version")
    else:
        # Rollback the last 'count' migrations
        if count < 1:
            raise MigrationError("count must be at least 1")
        if count > len(applied_migrations):
            raise MigrationError(f"cannot rollback {count} migrations: only {len(applied_migrations)} applied")
        versions_to_rollback = [m['version'] for m in applied_migrations[-count:]]

    # Output final_version before any rollback
    final_version = applied_migrations[0]['version']

    # Rollback in reverse version order
    rolled_back = []
    for version in sorted(versions_to_rollback, reverse=True):
        rollback_migration(conn, version, explicit_rollback_ops)
        rolled_back.append(version)

    # Output rollback_finished event
    new_final = get_applied_migrations(cursor)
    final_ver = new_final[-1]['version'] if new_final else 0
    print(f'{{"event": "rollback_finished", "versions_rolled_back": {rolled_back}, "final_version": {final_ver}}}')


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Database Migration Tool - Apply schema migrations to SQLite databases'
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration file to a database')
    migrate_parser.add_argument('migration_file', help='Path to the migration JSON file')
    migrate_parser.add_argument('database_file', help='Path to the SQLite database file')

    rollback_parser = subparsers.add_parser('rollback', help='Rollback migrations from a database')
    rollback_parser.add_argument('database_file', help='Path to the SQLite database file')
    rollback_parser.add_argument('--to-version', type=int, help='Rollback to a specific version (exclusive)')
    rollback_parser.add_argument('--count', type=int, default=1, help='Rollback the last N migrations (default: 1)')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == 'migrate':
        try:
            # Load and validate migration
            migration = load_migration_file(args.migration_file)

            # Connect to database
            conn = sqlite3.connect(args.database_file)

            # Apply migration
            apply_migration(conn, migration)

            conn.close()

        except FileNotFoundError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except ValidationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.Error as e:
            print(f"Error: SQL error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == 'rollback':
        try:
            # Connect to database
            conn = sqlite3.connect(args.database_file)

            # Validate arguments
            if args.to_version is not None and args.count != 1:
                print("Error: Cannot specify both --to-version and --count", file=sys.stderr)
                sys.exit(1)

            # Execute rollback
            execute_rollback(conn, to_version=args.to_version, count=args.count)

            conn.close()

        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        except sqlite3.Error as e:
            print(f"Error: SQL error: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == '__main__':
    main()
