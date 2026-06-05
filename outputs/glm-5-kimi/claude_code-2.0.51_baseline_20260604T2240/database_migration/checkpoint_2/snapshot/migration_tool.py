#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 2: Data Migrations

A CLI tool that applies database schema migrations to SQLite databases.
Supports both schema changes and data transformations during migrations.
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


def output_event(event: Dict[str, Any]) -> None:
    """Output an event as a JSON line to stdout."""
    print(json.dumps(event), flush=True)


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
        # Disable foreign keys during migration
        conn.execute("PRAGMA foreign_keys = OFF")

        # Create _migrations table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                ops_count += 1

            # Record migration
            conn.execute(
                "INSERT INTO _migrations (version, description) VALUES (?, ?)",
                (version, description)
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

    args = parser.parse_args()

    if args.command == 'migrate':
        apply_migration(args.migration_file, args.database)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
