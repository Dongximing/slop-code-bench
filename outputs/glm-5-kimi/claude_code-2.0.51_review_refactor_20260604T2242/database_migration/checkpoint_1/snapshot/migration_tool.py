#!/usr/bin/env python3
"""
Database Migration Tool for SQLite
Applies schema migrations from JSON specification files.
"""

import argparse
import json
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class MigrationTool:
    """Handles database schema migrations for SQLite."""

    # Valid SQLite data types
    VALID_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}

    def __init__(self, db_path: str):
        """Initialize the migration tool with database path."""
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        """Connect to the SQLite database."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = OFF")

    def close(self) -> None:
        """Close the database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    def _validate_identifier(self, identifier: str, identifier_type: str) -> None:
        """Validate that an identifier is a valid SQLite identifier."""
        if not identifier:
            raise MigrationError(f"invalid {identifier_type}: cannot be empty")

        # SQLite identifiers: alphanumeric + underscore, starting with letter/underscore
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier):
            raise MigrationError(
                f"invalid {identifier_type}: '{identifier}' is not a valid SQLite identifier"
            )

    def _quote_identifier(self, identifier: str) -> str:
        """Quote an identifier for safe SQL usage."""
        return f'"{identifier.replace(chr(34), chr(34)+chr(34))}"'

    def _build_column_def(self, column: Dict[str, Any]) -> str:
        """Build a SQL column definition from column specification."""
        name = column.get('name')
        col_type = column.get('type', 'TEXT').upper()

        if col_type not in self.VALID_TYPES:
            raise MigrationError(
                f"invalid column type: '{col_type}' (valid types: {', '.join(self.VALID_TYPES)})"
            )

        self._validate_identifier(name, 'column name')

        parts = [self._quote_identifier(name), col_type]

        is_primary_key = column.get('primary_key', False)
        auto_increment = column.get('auto_increment', False)
        not_null = column.get('not_null', False)
        unique = column.get('unique', False)
        default = column.get('default')

        # Validate auto_increment constraints
        if auto_increment:
            if not is_primary_key:
                raise MigrationError(
                    f"auto_increment can only be true for primary key columns"
                )
            if col_type != 'INTEGER':
                raise MigrationError(
                    f"auto_increment can only be used with INTEGER type, got '{col_type}'"
                )

        if is_primary_key:
            if auto_increment:
                parts.append('PRIMARY KEY AUTOINCREMENT')
            else:
                parts.append('PRIMARY KEY')

        if not_null and not is_primary_key:
            parts.append('NOT NULL')

        if unique and not is_primary_key:
            parts.append('UNIQUE')

        if default is not None:
            parts.append(f'DEFAULT {default}')

        return ' '.join(parts)

    def _ensure_migrations_table(self) -> None:
        """Create the _migrations table if it doesn't exist."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def _is_migration_applied(self, version: int) -> bool:
        """Check if a migration version has already been applied."""
        cursor = self.conn.execute(
            "SELECT 1 FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def _record_migration(self, version: int, description: str) -> None:
        """Record a successful migration in the _migrations table."""
        self.conn.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )
        self.conn.commit()

    def _table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database."""
        cursor = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None

    def _get_table_columns(self, table_name: str) -> List[str]:
        """Get list of column names for a table."""
        cursor = self.conn.execute(f'PRAGMA table_info({self._quote_identifier(table_name)})')
        return [row[1] for row in cursor.fetchall()]

    def _get_column_info(self, table_name: str) -> List[Dict[str, Any]]:
        """Get detailed column information for a table."""
        cursor = self.conn.execute(f'PRAGMA table_info({self._quote_identifier(table_name)})')
        columns = []
        for row in cursor.fetchall():
            columns.append({
                'cid': row[0],
                'name': row[1],
                'type': row[2],
                'notnull': row[3],
                'default': row[4],
                'pk': row[5]
            })
        return columns

    def _create_table(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Execute a create_table operation."""
        table_name = operation.get('table')
        columns = operation.get('columns', [])

        self._validate_identifier(table_name, 'table name')

        if not columns:
            raise MigrationError("create_table requires at least one column")

        # Check if table already exists
        if self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' already exists")

        # Validate columns
        primary_key_count = 0
        for col in columns:
            if col.get('primary_key', False):
                primary_key_count += 1

        if primary_key_count > 1:
            raise MigrationError("at most one column can have primary_key: true")

        # Build column definitions
        col_defs = [self._build_column_def(col) for col in columns]

        # Create the table
        sql = f"CREATE TABLE {self._quote_identifier(table_name)} ({', '.join(col_defs)})"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'create_table',
            'table': table_name,
            'version': version
        }

    def _add_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Execute an add_column operation."""
        table_name = operation.get('table')
        column = operation.get('column')

        self._validate_identifier(table_name, 'table name')

        if not column:
            raise MigrationError("add_column requires a 'column' specification")

        column_name = column.get('name')
        self._validate_identifier(column_name, 'column name')

        # Check if table exists
        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check if column already exists
        existing_columns = self._get_table_columns(table_name)
        if column_name in existing_columns:
            raise MigrationError(
                f"invalid operation: column '{column_name}' already exists in table '{table_name}'"
            )

        # Build column definition
        col_def = self._build_column_def(column)

        # Add the column
        sql = f"ALTER TABLE {self._quote_identifier(table_name)} ADD COLUMN {col_def}"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'add_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _drop_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Execute a drop_column operation."""
        table_name = operation.get('table')
        column_name = operation.get('column')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(column_name, 'column name')

        # Check if table exists
        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check if column exists
        columns_info = self._get_column_info(table_name)
        column_names = [col['name'] for col in columns_info]

        if column_name not in column_names:
            raise MigrationError(
                f"invalid operation: column '{column_name}' does not exist in table '{table_name}'"
            )

        # Check if it's the only column
        if len(columns_info) == 1:
            raise MigrationError(
                f"cannot drop column '{column_name}': it's the only column in table '{table_name}'"
            )

        # Check if it's a primary key column
        for col in columns_info:
            if col['name'] == column_name and col['pk'] > 0:
                raise MigrationError(
                    f"cannot drop column '{column_name}': it's a PRIMARY KEY column"
                )

        # Get columns to keep
        columns_to_keep = [col for col in columns_info if col['name'] != column_name]

        # Create new table name
        new_table_name = f"_{table_name}_new"

        # Build column definitions for new table
        col_defs = []
        for col in columns_to_keep:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            # Check if this column is a primary key
            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            # Handle NOT NULL (col['notnull'] is 1 if NOT NULL)
            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            # Handle DEFAULT
            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        # Create new table
        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(col_defs)})"
        self.conn.execute(create_sql)

        # Copy data
        keep_col_names = [self._quote_identifier(col['name']) for col in columns_to_keep]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(keep_col_names)}) SELECT {', '.join(keep_col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        # Drop old table
        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'drop_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _validate_migration_file(self, migration_data: Dict[str, Any]) -> None:
        """Validate the migration file structure."""
        required_fields = ['version', 'description', 'operations']

        for field in required_fields:
            if field not in migration_data:
                raise MigrationError(f"invalid migration schema: missing required field '{field}'")

        version = migration_data['version']
        if not isinstance(version, int) or version <= 0:
            raise MigrationError("migration version must be a positive integer")

        operations = migration_data['operations']
        if not isinstance(operations, list):
            raise MigrationError("invalid migration schema: 'operations' must be an array")

        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                raise MigrationError(f"invalid operation at index {i}: must be an object")

            op_type = op.get('type')
            if not op_type:
                raise MigrationError(f"invalid operation at index {i}: missing 'type' field")

            if op_type not in {'create_table', 'add_column', 'drop_column'}:
                raise MigrationError(f"invalid operation type: '{op_type}'")

            if 'table' not in op:
                raise MigrationError(f"invalid operation at index {i}: missing 'table' field")

            if op_type == 'create_table':
                if 'columns' not in op:
                    raise MigrationError(f"invalid create_table operation: missing 'columns' field")

            elif op_type == 'add_column':
                if 'column' not in op:
                    raise MigrationError(f"invalid add_column operation: missing 'column' field")

            elif op_type == 'drop_column':
                if 'column' not in op:
                    raise MigrationError(f"invalid drop_column operation: missing 'column' field")

    def apply_migration(self, migration_path: str) -> None:
        """Apply a migration from a JSON file."""
        # Check if migration file exists
        if not Path(migration_path).exists():
            raise MigrationError(f"migration file not found: {migration_path}")

        # Read and parse migration file
        try:
            with open(migration_path, 'r') as f:
                migration_data = json.load(f)
        except json.JSONDecodeError as e:
            raise MigrationError(f"invalid JSON in migration file: {e}")

        # Validate migration file
        self._validate_migration_file(migration_data)

        version = migration_data['version']
        description = migration_data['description']
        operations = migration_data['operations']

        # Ensure migrations table exists
        self._ensure_migrations_table()

        # Check if migration already applied
        if self._is_migration_applied(version):
            print(
                json.dumps({
                    'event': 'migration_skipped',
                    'version': version,
                    'reason': 'already_applied'
                }),
                file=sys.stdout
            )
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            return

        # Apply operations
        operations_count = 0
        for op in operations:
            op_type = op['type']

            if op_type == 'create_table':
                result = self._create_table(op, version)
            elif op_type == 'add_column':
                result = self._add_column(op, version)
            elif op_type == 'drop_column':
                result = self._drop_column(op, version)
            else:
                raise MigrationError(f"unknown operation type: {op_type}")

            print(json.dumps(result), file=sys.stdout)
            operations_count += 1

        # Record migration
        self._record_migration(version, description)

        # Print completion message
        print(
            json.dumps({
                'event': 'migration_complete',
                'version': version,
                'operations_count': operations_count
            }),
            file=sys.stdout
        )


def main():
    """Main entry point for the migration tool."""
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    # Migrate command
    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()

    if args.command != 'migrate':
        parser.print_help()
        sys.exit(1)

    tool = MigrationTool(args.database)

    try:
        tool.connect()
        tool.apply_migration(args.migration_file)
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Error: SQL error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        tool.close()


if __name__ == '__main__':
    main()
