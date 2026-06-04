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
    # Initialize _migrations table if it doesn't exist
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('SELECT version FROM _migrations WHERE version = ?', (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str) -> None:
    """Record a successfully applied migration."""
    cursor.execute(
        'INSERT INTO _migrations (version, description) VALUES (?, ?)',
        (version, description)
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
        else:
            raise ValidationError(f"Operation {i} has invalid type '{op_type}'. Must be: create_table, add_column, drop_column")

    return {
        'version': version,
        'description': description,
        'operations': validated_operations
    }


def build_column_definition(col: Dict[str, Any], use_double_quotes: bool = False) -> str:
    """Build a SQL column definition string."""
    parts = []

    # Column name
    if use_double_quotes:
        parts.append(f'"{col["name"]}"')
    else:
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

    # Start transaction
    cursor.execute('PRAGMA foreign_keys = OFF')

    output_lines = []

    try:
        for op in operations:
            op_type = op['type']

            if op_type == 'create_table':
                lines = execute_create_table(cursor, op)
            elif op_type == 'add_column':
                lines = execute_add_column(cursor, op)
            elif op_type == 'drop_column':
                lines = execute_drop_column(cursor, op, version)
            else:
                raise MigrationError(f"Unknown operation type: {op_type}")

            # Update version in output lines
            for i, line in enumerate(lines):
                if '"version":' in line:
                    lines[i] = line.replace(f'"version": 1}', f'"version": {version}}}')
                else:
                    # Add version if not present (for drop_column which adds it manually)
                    if line.strip().endswith('}'):
                        lines[i] = line.rstrip('}') + f', "version": {version}}}'
            output_lines.extend(lines)

        # Record successful migration
        record_migration(cursor, version, description)

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


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Database Migration Tool - Apply schema migrations to SQLite databases'
    )
    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration file to a database')
    migrate_parser.add_argument('migration_file', help='Path to the migration JSON file')
    migrate_parser.add_argument('database_file', help='Path to the SQLite database file')

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


if __name__ == '__main__':
    main()
