#!/usr/bin/env python3
"""
Database Migration Tool - SQLite Schema Migrations
CLI tool that applies database schema migrations from JSON files to SQLite databases.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

VALID_COLUMN_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}


class MigrationError(Exception):
    """Base exception for migration errors."""
    pass


class ValidationError(MigrationError):
    """Raised when migration schema validation fails."""
    pass


class SQLExecutionError(MigrationError):
    """Raised when SQL execution fails."""
    pass


def validate_identifier(identifier: str) -> bool:
    """Validate that identifier matches SQLite identifier rules."""
    if not identifier:
        return False
    # SQLite identifiers: alphanumeric + underscore, starting with letter or underscore
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier))


def quote_identifier(identifier: str) -> str:
    """Quote an identifier to prevent SQL injection."""
    if not validate_identifier(identifier):
        raise ValidationError(f"Invalid identifier: '{identifier}'")
    return f'"{identifier}"'


def validate_migration_file(data: dict) -> None:
    """Validate the migration file structure."""
    if not isinstance(data, dict):
        raise ValidationError("Migration file must be a JSON object")

    required_fields = ['version', 'description', 'operations']
    for field in required_fields:
        if field not in data:
            raise ValidationError(f"Missing required field '{field}'")

    if not isinstance(data['version'], int) or data['version'] < 1:
        raise ValidationError("Migration version must be a positive integer")

    if not isinstance(data['description'], str):
        raise ValidationError("Description must be a string")

    if not isinstance(data['operations'], list):
        raise ValidationError("Operations must be an array")

    if len(data['operations']) == 0:
        raise ValidationError("Operations array cannot be empty")

    valid_types = {'create_table', 'add_column', 'drop_column'}
    for i, op in enumerate(data['operations']):
        if not isinstance(op, dict):
            raise ValidationError(f"Operation at index {i} must be an object")

        if 'type' not in op:
            raise ValidationError(f"Operation at index {i} missing required field 'type'")

        if op['type'] not in valid_types:
            raise ValidationError(f"Invalid operation type: '{op['type']}'")

        # Validate required fields per operation type
        if op['type'] == 'create_table':
            validate_create_table_operation(op, i)
        elif op['type'] == 'add_column':
            validate_add_column_operation(op, i)
        elif op['type'] == 'drop_column':
            validate_drop_column_operation(op, i)


def validate_create_table_operation(op: dict, index: int) -> None:
    """Validate create_table operation."""
    if 'table' not in op:
        raise ValidationError(f"create_table operation at index {index} missing 'table'")
    if 'columns' not in op:
        raise ValidationError(f"create_table operation at index {index} missing 'columns'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise ValidationError(f"create_table operation at index {index} must have at least one column")

    primary_key_count = 0

    for j, col in enumerate(op['columns']):
        if not isinstance(col, dict):
            raise ValidationError(f"Column at index {j} in create_table must be an object")
        if 'name' not in col:
            raise ValidationError(f"Column at index {j} missing 'name'")
        if 'type' not in col:
            raise ValidationError(f"Column at index {j} missing 'type'")

        col_name = col['name']
        col_type = col['type'].upper()

        if not validate_identifier(col_name):
            raise ValidationError(f"Invalid column name: '{col_name}'")

        if col_type not in VALID_COLUMN_TYPES:
            raise ValidationError(f"Invalid column type: '{col['type']}' for column '{col_name}'")

        # Check primary_key constraint
        if col.get('primary_key', False):
            primary_key_count += 1
            if primary_key_count > 1:
                raise ValidationError(f"Only one primary key allowed in create_table")

        # Check auto_increment constraint
        if col.get('auto_increment', False):
            if col_type != 'INTEGER':
                raise ValidationError(f"auto_increment can only be used with INTEGER column '{col_name}'")
            if not col.get('primary_key', False):
                raise ValidationError(f"auto_increment requires primary_key=True for column '{col_name}'")


def validate_add_column_operation(op: dict, index: int) -> None:
    """Validate add_column operation."""
    if 'table' not in op:
        raise ValidationError(f"add_column operation at index {index} missing 'table'")
    if 'column' not in op:
        raise ValidationError(f"add_column operation at index {index} missing 'column'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    col = op['column']
    if not isinstance(col, dict):
        raise ValidationError(f"Column must be an object for add_column operation at index {index}")

    if 'name' not in col:
        raise ValidationError(f"Column missing 'name' in add_column operation at index {index}")
    if 'type' not in col:
        raise ValidationError(f"Column missing 'type' in add_column operation at index {index}")

    col_name = col['name']
    col_type = col['type'].upper()

    if not validate_identifier(col_name):
        raise ValidationError(f"Invalid column name: '{col_name}'")

    if col_type not in VALID_COLUMN_TYPES:
        raise ValidationError(f"Invalid column type: '{col['type']}' for column '{col_name}'")

    # auto_increment not valid for add_column
    if col.get('auto_increment', False):
        raise ValidationError(f"auto_increment not allowed for add_column operation for column '{col_name}'")


def validate_drop_column_operation(op: dict, index: int) -> None:
    """Validate drop_column operation."""
    if 'table' not in op:
        raise ValidationError(f"drop_column operation at index {index} missing 'table'")
    if 'column' not in op:
        raise ValidationError(f"drop_column operation at index {index} missing 'column'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    column_name = op['column']
    if not validate_identifier(column_name):
        raise ValidationError(f"Invalid column name: '{column_name}'")


def get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> dict:
    """Get information about columns in a table."""
    pragma_query = f"PRAGMA table_info({quote_identifier(table_name)})"
    cursor.execute(pragma_query)
    columns = {}
    for row in cursor.fetchall():
        col_name = row[1]  # name
        col_type = row[2].upper() if row[2] else 'TEXT'
        not_null = bool(row[3])
        default_val = row[4]
        primary_key = bool(row[5])
        columns[col_name] = {
            'name': col_name,
            'type': col_type,
            'not_null': not_null,
            'default': default_val,
            'primary_key': primary_key
        }
    return columns


def check_table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check if a table exists."""
    query = f"""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name={quote_identifier(table_name)}
    """
    cursor.execute(query)
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


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def apply_create_table(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply create_table operation."""
    table_name = op['table']
    columns = op['columns']

    if check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' already exists")

    column_defs = []
    for col in columns:
        col_name = col['name']
        col_type = col['type'].upper()

        # Build column definition
        def_parts = [quote_identifier(col_name), col_type]

        if col.get('not_null', False):
            def_parts.append('NOT NULL')

        if col.get('primary_key', False):
            def_parts.append('PRIMARY KEY')
            if col.get('auto_increment', False):
                def_parts.append('AUTOINCREMENT')

        if col.get('unique', False):
            def_parts.append('UNIQUE')

        if 'default' in col and col['default'] is not None:
            def_parts.append(f"DEFAULT {col['default']}")

        column_defs.append(' '.join(def_parts))

    create_sql = f"CREATE TABLE {quote_identifier(table_name)} (" + ', '.join(column_defs) + ")"
    cursor.execute(create_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'create_table',
        'table': table_name,
        'version': version
    }))


def apply_add_column(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply add_column operation."""
    table_name = op['table']
    col = op['column']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    # Check if column already exists
    existing_columns = get_table_columns(cursor, table_name)
    if col['name'] in existing_columns:
        raise SQLExecutionError(f"column '{col['name']}' already exists in table '{table_name}'")

    # Build ALTER TABLE statement
    col_name = col['name']
    col_type = col['type'].upper()

    def_parts = [quote_identifier(col_name), col_type]

    if col.get('not_null', False):
        def_parts.append('NOT NULL')

    if 'default' in col and col['default'] is not None:
        def_parts.append(f"DEFAULT {col['default']}")

    alter_sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN " + ' '.join(def_parts)
    cursor.execute(alter_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'add_column',
        'table': table_name,
        'column': col_name,
        'version': version
    }))


def apply_drop_column(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply drop_column operation."""
    table_name = op['table']
    column_name = op['column']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)

    if column_name not in existing_columns:
        raise SQLExecutionError(f"column '{column_name}' does not exist in table '{table_name}'")

    # Check if column is the only column
    if len(existing_columns) <= 1:
        raise SQLExecutionError(f"Cannot drop column '{column_name}' as it is the only column in table '{table_name}'")

    # Check if column is primary key
    if existing_columns[column_name]['primary_key']:
        raise SQLExecutionError(f"Cannot drop PRIMARY KEY column '{column_name}' from table '{table_name}'")

    # SQLite doesn't support DROP COLUMN, so we need to recreate the table
    temp_table_name = f"_{table_name}_temp"

    # Get columns to keep (excluding the one to drop)
    columns_to_keep = [col for col in existing_columns.keys() if col != column_name]

    # Build CREATE TABLE statement for new table
    new_table_defs = []
    for col_name in columns_to_keep:
        col_info = existing_columns[col_name]
        def_parts = [quote_identifier(col_name), col_info['type']]

        if col_info['not_null']:
            def_parts.append('NOT NULL')

        if col_info['primary_key']:
            def_parts.append('PRIMARY KEY')

        # Handle default value - SQLite stores default as string representation
        if col_info['default'] is not None:
            default_val = col_info['default']
            if default_val:
                # Check if it's a quoted string or expression
                if isinstance(default_val, str) and default_val.startswith("'") and default_val.endswith("'"):
                    def_parts.append(f"DEFAULT {default_val}")
                else:
                    def_parts.append(f"DEFAULT {default_val}")

        new_table_defs.append(' '.join(def_parts))

    # Create new table
    create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} (" + ', '.join(new_table_defs) + ")"
    cursor.execute(create_sql)

    # Copy data from old table to new table
    cols_to_select = ', '.join([quote_identifier(col) for col in columns_to_keep])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    cursor.execute(insert_sql)

    # Drop old table
    cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")

    # Rename new table to original name
    cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'drop_column',
        'table': table_name,
        'column': column_name,
        'version': version
    }))


def apply_migration(db_path: str, migration_data: dict) -> None:
    """Apply a migration to the database."""
    version = migration_data['version']
    description = migration_data['description']
    operations = migration_data['operations']

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Create _migrations table
        create_migrations_table(cursor)

        # Check if migration already applied
        if is_migration_applied(cursor, version):
            print(json.dumps({
                'event': 'migration_skipped',
                'version': version,
                'reason': 'already_applied'
            }))
            sys.stderr.write(f"Warning: Migration version {version} already applied, skipping\n")
            conn.commit()
            conn.close()
            return

        # Apply operations in order
        for op in operations:
            op_type = op['type']

            if op_type == 'create_table':
                apply_create_table(cursor, op, version)
            elif op_type == 'add_column':
                apply_add_column(cursor, op, version)
            elif op_type == 'drop_column':
                apply_drop_column(cursor, op, version)

        # Record migration
        cursor.execute("""
            INSERT INTO _migrations (version, description)
            VALUES (?, ?)
        """, (version, description))

        print(json.dumps({
            'event': 'migration_complete',
            'version': version,
            'operations_count': len(operations)
        }))

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise MigrationError(str(e))

    finally:
        conn.close()


def load_migration_file(file_path: str) -> dict:
    """Load and parse a migration file."""
    path = Path(file_path)

    if not path.exists():
        raise MigrationError(f"migration file not found: {file_path}")

    try:
        with open(path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")


def main():
    if len(sys.argv) < 4:
        sys.stderr.write("Usage: python migration_tool.py migrate migration.json database.db\n")
        sys.exit(1)

    command = sys.argv[1]

    if command != 'migrate':
        sys.stderr.write(f"Unknown command: {command}\n")
        sys.exit(1)

    migration_path = sys.argv[2]
    db_path = sys.argv[3]

    try:
        migration_data = load_migration_file(migration_path)
        validate_migration_file(migration_data)
        apply_migration(db_path, migration_data)

    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == '__main__':
    main()
