#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 1: Basic Schema Migrations

A CLI tool that applies database schema migrations to SQLite databases.
Supports create_table, add_column, and drop_column operations.
"""

import json
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any


class MigrationError(Exception):
    """Custom exception for migration-related errors."""
    pass


def validate_identifier(name: str) -> bool:
    """Check if a name is a valid SQLite identifier."""
    if not name:
        return False
    # SQLite identifiers: alphanumeric + underscore, starting with letter/underscore
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def validate_column_definition(col: dict, context: str) -> None:
    """Validate a column definition."""
    if not isinstance(col, dict):
        raise MigrationError(f"invalid column definition in {context}: must be an object")

    if 'name' not in col:
        raise MigrationError(f"invalid column definition in {context}: missing 'name'")

    column_name = col['name']
    if not validate_identifier(column_name):
        raise MigrationError(f"invalid column name '{column_name}' in {context}")

    if 'type' not in col:
        raise MigrationError(f"invalid column definition in {context}: missing 'type'")

    col_type = col['type'].upper()
    valid_types = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}
    if col_type not in valid_types:
        raise MigrationError(f"invalid column type '{col['type']}' for column '{column_name}'")

    # Validate auto_increment constraints
    if col.get('auto_increment', False):
        if col_type != 'INTEGER':
            raise MigrationError(f"column '{column_name}': auto_increment can only be used with INTEGER type")
        if not col.get('primary_key', False):
            raise MigrationError(f"column '{column_name}': auto_increment requires primary_key=true")

    # Validate default is a string or null
    if 'default' in col and col['default'] is not None and not isinstance(col['default'], str):
        raise MigrationError(f"column '{column_name}': default must be a string or null")


def load_migration_file(path: str) -> dict:
    """Load and parse a migration JSON file."""
    file_path = Path(path)
    if not file_path.exists():
        raise MigrationError(f"migration file not found: {path}")

    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")

    if not isinstance(data, dict):
        raise MigrationError("invalid migration schema: root must be an object")

    # Validate required fields
    if 'version' not in data:
        raise MigrationError("invalid migration schema: missing required field 'version'")

    version = data['version']
    if not isinstance(version, int) or version <= 0:
        raise MigrationError("invalid migration schema: version must be a positive integer")

    if 'description' not in data:
        raise MigrationError("invalid migration schema: missing required field 'description'")

    if not isinstance(data['description'], str):
        raise MigrationError("invalid migration schema: 'description' must be a string")

    if 'operations' not in data:
        raise MigrationError("invalid migration schema: missing required field 'operations'")

    if not isinstance(data['operations'], list):
        raise MigrationError("invalid migration schema: 'operations' must be an array")

    return data


def validate_migration(data: dict) -> None:
    """Validate the entire migration structure."""
    for i, op in enumerate(data['operations']):
        op_type = op.get('type')
        if not op_type:
            raise MigrationError(f"operation {i}: missing 'type'")

        if op_type == 'create_table':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: create_table missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'columns' not in op:
                raise MigrationError(f"operation {i}: create_table missing 'columns'")
            if not isinstance(op['columns'], list):
                raise MigrationError(f"operation {i}: 'columns' must be an array")
            if len(op['columns']) == 0:
                raise MigrationError(f"operation {i}: create_table must have at least one column")

            # Check for at most one primary key
            pk_count = sum(1 for col in op['columns'] if col.get('primary_key', False))
            if pk_count > 1:
                raise MigrationError(f"operation {i}: only one column can have primary_key=true")

            for j, col in enumerate(op['columns']):
                validate_column_definition(col, f"operation {i}, column {j}")

        elif op_type == 'add_column':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: add_column missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: add_column missing 'column'")
            validate_column_definition(op['column'], f"operation {i}, column")

        elif op_type == 'drop_column':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: drop_column missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: drop_column missing 'column'")
            column_name = op['column']
            if not validate_identifier(column_name):
                raise MigrationError(f"operation {i}: invalid column name '{column_name}'")

        elif op_type == 'transform_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: transform_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'transformations' not in op:
                raise MigrationError(f"operation {i}: transform_data missing 'transformations'")
            if not isinstance(op['transformations'], list):
                raise MigrationError(f"operation {i}: 'transformations' must be an array")
            if len(op['transformations']) == 0:
                raise MigrationError(f"operation {i}: transform_data must have at least one transformation")

            for j, trans in enumerate(op['transformations']):
                if not isinstance(trans, dict):
                    raise MigrationError(f"operation {i}: transformation {j} must be an object")
                if 'column' not in trans:
                    raise MigrationError(f"operation {i}: transformation {j} missing 'column'")
                column = trans['column']
                if not validate_identifier(column):
                    raise MigrationError(f"operation {i}: transformation {j} invalid column name '{column}'")
                if 'expression' not in trans:
                    raise MigrationError(f"operation {i}: transformation {j} missing 'expression'")
                expression = trans['expression']
                if not isinstance(expression, str):
                    raise MigrationError(f"operation {i}: transformation {j} 'expression' must be a string")

        elif op_type == 'migrate_column_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'from_column' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'from_column'")
            from_column = op['from_column']
            if not validate_identifier(from_column):
                raise MigrationError(f"operation {i}: migrate_column_data invalid 'from_column' name '{from_column}'")

            if 'to_column' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'to_column'")
            to_column = op['to_column']
            if not validate_identifier(to_column):
                raise MigrationError(f"operation {i}: migrate_column_data invalid 'to_column' name '{to_column}'")

        elif op_type == 'backfill_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'column'")
            column = op['column']
            if not validate_identifier(column):
                raise MigrationError(f"operation {i}: backfill_data invalid column name '{column}'")

            if 'value' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'value'")

            if op.get('where') is not None and not isinstance(op['where'], str):
                raise MigrationError(f"operation {i}: backfill_data 'where' must be a string")

        else:
            raise MigrationError(f"operation {i}: unknown operation type '{op_type}'")


def get_table_info(cursor: sqlite3.Cursor, table_name: str) -> list:
    """Get information about table columns."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return cursor.fetchall()


def get_primary_key_column(cursor: sqlite3.Cursor, table_name: str) -> str | None:
    """Get the name of the primary key column, if any."""
    info = get_table_info(cursor, table_name)
    for col in info:
        # col[5] is the pk flag (1 for primary key)
        if col[5] == 1:
            return col[1]  # col[1] is the column name
    return None


def column_definition_to_sql(col: dict) -> str:
    """Convert a column definition dict to SQL column definition."""
    parts = []
    parts.append(f"{col['name']} {col['type'].upper()}")

    if col.get('not_null', False):
        parts.append("NOT NULL")

    if col.get('primary_key', False):
        parts.append("PRIMARY KEY")
        if col.get('auto_increment', False):
            parts.append("AUTOINCREMENT")

    if col.get('unique', False):
        parts.append("UNIQUE")

    if 'default' in col and col['default'] is not None:
        parts.append(f"DEFAULT {col['default']}")

    return " ".join(parts)


def apply_create_table(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a create_table operation."""
    table_name = op['table']

    # Check if table already exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if cursor.fetchone():
        raise MigrationError(f"table '{table_name}' already exists")

    # Build column definitions
    columns_sql = [column_definition_to_sql(col) for col in op['columns']]
    sql = f"CREATE TABLE {table_name} ({', '.join(columns_sql)})"

    cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "create_table",
        "table": table_name,
        "version": version
    }


def apply_add_column(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply an add_column operation."""
    table_name = op['table']
    column = op['column']
    column_name = column['name']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if column already exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column_name in existing_columns:
        raise MigrationError(f"column '{column_name}' already exists in table '{table_name}'")

    # Build ALTER TABLE statement
    column_sql = column_definition_to_sql(column)
    sql = f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"

    cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "add_column",
        "table": table_name,
        "column": column_name,
        "version": version
    }


def apply_drop_column(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a drop_column operation."""
    table_name = op['table']
    column_name = op['column']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if column exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column_name not in existing_columns:
        raise MigrationError(f"column '{column_name}' does not exist in table '{table_name}'")

    # Cannot drop if it's the only column
    if len(existing_columns) == 1:
        raise MigrationError(f"cannot drop column '{column_name}': it is the only column in table '{table_name}'")

    # Cannot drop PRIMARY KEY column
    pk_column = get_primary_key_column(cursor, table_name)
    if pk_column == column_name:
        raise MigrationError(f"cannot drop column '{column_name}': it is a PRIMARY KEY column")

    # Get columns to keep (excluding the one to drop)
    columns_to_keep = [col for col in existing_columns if col != column_name]

    # Create temp table name
    temp_table_name = f"{table_name}_temp"

    # Build the new column definitions from the old table info, excluding the dropped column
    # We need to preserve all column properties from the original table
    new_columns_sql = []
    for col in table_info:
        col_name = col[1]
        if col_name == column_name:
            continue

        col_type = col[2]  # type
        not_null = col[3] == 1  # notnull
        default_value = col[4]  # default_value
        pk = col[5]  # pk flag

        col_def = {"name": col_name, "type": col_type}
        if not_null:
            col_def["not_null"] = True
        if pk:
            col_def["primary_key"] = True
        if default_value is not None:
            # SQLite stores default values as strings, need to handle quotes
            col_def["default"] = repr(default_value)

        new_columns_sql.append(column_definition_to_sql(col_def))

    # Create new table without the dropped column
    create_new_table_sql = f"CREATE TABLE {temp_table_name} ({', '.join(new_columns_sql)})"
    cursor.execute(create_new_table_sql)

    # Copy data from old table to new table
    select_cols = ", ".join(columns_to_keep)
    insert_sql = f"INSERT INTO {temp_table_name} SELECT {select_cols} FROM {table_name}"
    cursor.execute(insert_sql)

    # Drop old table
    cursor.execute(f"DROP TABLE {table_name}")

    # Rename new table to original name
    cursor.execute(f"ALTER TABLE {temp_table_name} RENAME TO {table_name}")

    return {
        "event": "operation_applied",
        "type": "drop_column",
        "table": table_name,
        "column": column_name,
        "version": version
    }


def apply_transform_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a transform_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Get existing columns
    table_info = get_table_info(cursor, table_name)
    existing_columns = {col[1]: col for col in table_info}

    # Get primary key for the table
    pk_column = get_primary_key_column(cursor, table_name)
    if pk_column is None:
        raise MigrationError(f"table '{table_name}' has no primary key, required for transform_data")

    transformations = op.get('transformations', [])
    if not isinstance(transformations, list):
        raise MigrationError(f"operation: transform_data 'transformations' must be an array")

    for i, trans in enumerate(transformations):
        if not isinstance(trans, dict):
            raise MigrationError(f"operation: transformation {i} must be an object")

        column = trans.get('column')
        if not column:
            raise MigrationError(f"operation: transformation {i} missing 'column'")
        if not validate_identifier(column):
            raise MigrationError(f"operation: transformation {i} invalid column name '{column}'")

        expression = trans.get('expression')
        if not expression:
            raise MigrationError(f"operation: transformation {i} missing 'expression'")
        if not isinstance(expression, str):
            raise MigrationError(f"operation: transformation {i} 'expression' must be a string")

        # Check if column exists
        if column not in existing_columns:
            raise MigrationError(f"column '{column}' does not exist in table '{table_name}'")

        # Build and execute UPDATE statement
        sql = f"UPDATE {table_name} SET {column} = ({expression})"
        cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "transform_data",
        "table": table_name,
        "transformations": len(transformations),
        "version": version
    }


def apply_migrate_column_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a migrate_column_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    from_column = op.get('from_column')
    to_column = op.get('to_column')

    if not from_column:
        raise MigrationError("operation: migrate_column_data missing 'from_column'")
    if not to_column:
        raise MigrationError("operation: migrate_column_data missing 'to_column'")

    if not validate_identifier(from_column):
        raise MigrationError(f"operation: migrate_column_data invalid 'from_column' name '{from_column}'")
    if not validate_identifier(to_column):
        raise MigrationError(f"operation: migrate_column_data invalid 'to_column' name '{to_column}'")

    # Get existing columns
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]

    if from_column not in existing_columns:
        raise MigrationError(f"column '{from_column}' does not exist in table '{table_name}'")
    if to_column not in existing_columns:
        raise MigrationError(f"column '{to_column}' does not exist in table '{table_name}'")

    default_value = op.get('default_value')

    # Build SET expression
    if default_value is not None:
        # Use COALESCE to set to default when from_column is NULL
        sql = f"UPDATE {table_name} SET {to_column} = COALESCE({from_column}, ?)"
        cursor.execute(sql, (default_value,))
    else:
        sql = f"UPDATE {table_name} SET {to_column} = {from_column}"
        cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "migrate_column_data",
        "table": table_name,
        "from_column": from_column,
        "to_column": to_column,
        "version": version
    }


def apply_backfill_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a backfill_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    column = op.get('column')
    if not column:
        raise MigrationError("operation: backfill_data missing 'column'")
    if not validate_identifier(column):
        raise MigrationError(f"operation: backfill_data invalid column name '{column}'")

    # Check if column exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column not in existing_columns:
        raise MigrationError(f"column '{column}' does not exist in table '{table_name}'")

    value = op.get('value')
    if value is None:
        raise MigrationError("operation: backfill_data missing 'value'")

    where = op.get('where')

    # Build SET clause
    if isinstance(value, str):
        # Treat as SQL expression
        set_clause = f"{column} = {value}"
    else:
        # Treat as constant - use parameter
        set_clause = f"{column} = ?"

    # Build SQL
    if where:
        if not isinstance(where, str):
            raise MigrationError("operation: backfill_data 'where' must be a string")
        sql = f"UPDATE {table_name} SET {set_clause} WHERE {where}"
        if not isinstance(value, str):
            cursor.execute(sql, (value,))
        else:
            cursor.execute(sql)
    else:
        sql = f"UPDATE {table_name} SET {set_clause}"
        if not isinstance(value, str):
            cursor.execute(sql, (value,))
        else:
            cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "backfill_data",
        "table": table_name,
        "column": column,
        "version": version
    }


def initialize_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Create the _migrations table if it doesn't exist."""
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
    """Record that a migration has been applied."""
    cursor.execute(
        "INSERT INTO _migrations (version, description) VALUES (?, ?)",
        (version, description)
    )


def apply_migration(db_path: str, migration_data: dict) -> int:
    """
    Apply a migration to the database.
    Returns 0 for success, 1 for skip, 2 for error.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Initialize migrations table
        initialize_migrations_table(cursor)

        version = migration_data['version']
        description = migration_data['description']

        # Check if already applied
        if is_migration_applied(cursor, version):
            print(json.dumps({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            }))
            sys.stderr.write(f"Warning: Migration version {version} already applied, skipping\n")
            conn.close()
            return 0

        # Validate the migration
        validate_migration(migration_data)

        # Apply operations
        applied_count = 0
        for op in migration_data['operations']:
            op_type = op['type']

            if op_type == 'create_table':
                result = apply_create_table(cursor, op, version)
            elif op_type == 'add_column':
                result = apply_add_column(cursor, op, version)
            elif op_type == 'drop_column':
                result = apply_drop_column(cursor, op, version)
            elif op_type == 'transform_data':
                result = apply_transform_data(cursor, op, version)
            elif op_type == 'migrate_column_data':
                result = apply_migrate_column_data(cursor, op, version)
            elif op_type == 'backfill_data':
                result = apply_backfill_data(cursor, op, version)
            else:
                # Should not happen due to validation
                raise MigrationError(f"unknown operation type: {op_type}")

            print(json.dumps(result))
            applied_count += 1

        # Record the migration
        record_migration(cursor, version, description)

        # Output completion event
        print(json.dumps({
            "event": "migration_complete",
            "version": version,
            "operations_count": applied_count
        }))

        conn.commit()
        conn.close()
        return 0

    except MigrationError as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: {e}\n")
        return 2
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: SQL error: {e}\n")
        return 2
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: {e}\n")
        return 2


def main():
    """Main entry point."""
    if len(sys.argv) < 4:
        sys.stderr.write("Usage: python migration_tool.py migrate <migration.json> <database.db>\n")
        sys.exit(1)

    command = sys.argv[1]

    if command != 'migrate':
        sys.stderr.write(f"Error: unknown command '{command}'\n")
        sys.stderr.write("Usage: python migration_tool.py migrate <migration.json> <database.db>\n")
        sys.exit(1)

    migration_file = sys.argv[2]
    database_file = sys.argv[3]

    # Load migration
    try:
        migration_data = load_migration_file(migration_file)
    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    # Apply migration
    result = apply_migration(database_file, migration_data)
    sys.exit(result)


if __name__ == '__main__':
    main()
