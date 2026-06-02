#!/usr/bin/env python3
"""
Database Migration Tool for SQLite.
Applies schema migrations from JSON files to SQLite databases.
"""

import argparse
import json
import os
import sys
import sqlite3
from typing import Any

# Supported SQLite data types
SUPPORTED_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}

# Error messages
ERROR_MESSAGES = {
    "file_not_found": "Error: migration file not found: {file}",
    "invalid_json": "Error: invalid JSON in migration file",
    "missing_version": "Error: invalid migration schema: missing required field 'version'",
    "missing_description": "Error: invalid migration schema: missing required field 'description'",
    "missing_operations": "Error: invalid migration schema: missing required field 'operations'",
    "invalid_version_type": "Error: invalid migration schema: 'version' must be an integer",
    "invalid_description_type": "Error: invalid migration schema: 'description' must be a string",
    "invalid_operations_type": "Error: invalid migration schema: 'operations' must be an array",
    "invalid_operation_type": "Error: invalid migration schema: each operation must have 'type' field",
    "table_already_exists": "Error: invalid operation: table '{table}' already exists",
    "table_not_found": "Error: invalid operation: table '{table}' does not exist",
    "column_already_exists": "Error: invalid operation: column '{column}' already exists in table '{table}'",
    "column_not_found": "Error: invalid operation: column '{column}' does not exist in table '{table}'",
    "migration_already_applied": "Warning: Migration version {version} already applied, skipping",
    "sql_error": "Error: SQL error: {error}",
    "invalid_column_schema": "Error: invalid column schema: {error}",
    "invalid_table_name": "Error: invalid table name: '{name}' (must be alphanumeric + underscore, starting with letter/underscore)",
    "invalid_column_name": "Error: invalid column name: '{name}' (must be alphanumeric + underscore, starting with letter/underscore)",
    "auto_increment_only_integer": "Error: invalid operation: auto_increment can only be used with INTEGER columns",
    "auto_increment_only_primary_key": "Error: invalid operation: auto_increment can only be used with primary_key columns",
    "multiple_primary_keys": "Error: invalid operation: only one column can be a primary key",
    "cannot_drop_only_column": "Error: invalid operation: cannot drop column '{column}' - it's the only column in table '{table}'",
    "cannot_drop_primary_key": "Error: invalid operation: cannot drop primary key column '{column}'",
    "missing_table_in_operation": "Error: invalid operation: missing required field 'table'",
    "missing_columns_in_create_table": "Error: invalid operation 'create_table': missing required field 'columns'",
    "missing_column_in_add_column": "Error: invalid operation 'add_column': missing required field 'column'",
    "missing_column_in_drop_column": "Error: invalid operation 'drop_column': missing required field 'column'",
    "invalid_columns_type": "Error: invalid operation 'create_table': 'columns' must be an array",
    "invalid_column_type": "Error: invalid column: 'type' is required",
    "invalid_column_name": "Error: invalid column: 'name' is required",
}


def is_valid_identifier(name: str) -> bool:
    """Check if a string is a valid SQLite identifier."""
    if not name:
        return False
    if not (name[0].isalpha() or name[0] == '_'):
        return False
    return all(c.isalnum() or c == '_' for c in name)


def escape_identifier(name: str) -> str:
    """Escape an identifier for safe SQL usage."""
    return f'`{name}`'


class MigrationError(Exception):
    """Custom exception for migration errors."""
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ColumnSchema:
    """Represents a column schema definition."""

    def __init__(self, definition: dict):
        self.definition = definition
        self.name = definition.get('name')
        self.type = definition.get('type')
        self.primary_key = definition.get('primary_key', False)
        self.auto_increment = definition.get('auto_increment', False)
        self.not_null = definition.get('not_null', False)
        self.unique = definition.get('unique', False)
        self.default = definition.get('default')

        self._validate()

    def _validate(self):
        """Validate the column schema."""
        if not self.name:
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"])

        if not is_valid_identifier(self.name):
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=self.name))

        if not self.type:
            raise MigrationError(ERROR_MESSAGES["invalid_column_type"])

        if self.type.upper() not in SUPPORTED_TYPES:
            raise MigrationError(f"Error: unsupported column type: '{self.type}'")

        if self.auto_increment:
            if self.type.upper() != "INTEGER":
                raise MigrationError(ERROR_MESSAGES["auto_increment_only_integer"])
            if not self.primary_key:
                raise MigrationError(ERROR_MESSAGES["auto_increment_only_primary_key"])

    def to_sql(self) -> str:
        """Convert column schema to SQL definition."""
        parts = [escape_identifier(self.name), self.type.upper()]

        if self.primary_key:
            parts.append("PRIMARY KEY")
            if self.auto_increment:
                parts.append("AUTOINCREMENT")

        if self.not_null:
            parts.append("NOT NULL")

        if self.unique:
            parts.append("UNIQUE")

        if self.default is not None:
            parts.append(f"DEFAULT {self.default}")

        return " ".join(parts)


class Operation:
    """Base class for migration operations."""

    def __init__(self, definition: dict):
        self.definition = definition
        self.type = definition.get('type')
        self.table = definition.get('table')

        if not self.type:
            raise MigrationError(ERROR_MESSAGES["invalid_operation_type"])

        if not self.table:
            raise MigrationError(ERROR_MESSAGES["missing_table_in_operation"])

        if not is_valid_identifier(self.table):
            raise MigrationError(ERROR_MESSAGES["invalid_table_name"].format(name=self.table))

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the operation and return event data."""
        raise NotImplementedError


class CreateTableOperation(Operation):
    """Operation to create a new table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.columns = definition.get('columns', [])

        if self.columns is None:
            raise MigrationError(ERROR_MESSAGES["missing_columns_in_create_table"])

        if not isinstance(self.columns, list):
            raise MigrationError(ERROR_MESSAGES["invalid_columns_type"])

        if not self.columns:
            raise MigrationError("Error: invalid operation 'create_table': columns array cannot be empty")

        self.column_schemas = [ColumnSchema(col) for col in self.columns]

        # Validate only one primary key
        primary_keys = sum(1 for col in self.column_schemas if col.primary_key)
        if primary_keys > 1:
            raise MigrationError(ERROR_MESSAGES["multiple_primary_keys"])

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the create table operation."""
        # Check if table exists
        if db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_already_exists"].format(table=self.table))

        # Build SQL
        column_defs = [col.to_sql() for col in self.column_schemas]
        sql = f"CREATE TABLE {escape_identifier(self.table)} ({', '.join(column_defs)})"

        db.execute(sql)

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "version": db.current_version
        }


class AddColumnOperation(Operation):
    """Operation to add a column to an existing table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        column_def = definition.get('column')

        if not column_def:
            raise MigrationError(ERROR_MESSAGES["missing_column_in_add_column"])

        self.column = ColumnSchema(column_def)

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the add column operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if column already exists
        if db.column_exists(self.table, self.column.name):
            raise MigrationError(ERROR_MESSAGES["column_already_exists"].format(
                column=self.column.name, table=self.table
            ))

        # Build SQL
        column_sql = self.column.to_sql()
        sql = f"ALTER TABLE {escape_identifier(self.table)} ADD COLUMN {column_sql}"

        db.execute(sql)

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "column": self.column.name,
            "version": db.current_version
        }


class DropColumnOperation(Operation):
    """Operation to drop a column from a table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.column_name = definition.get('column')

        if not self.column_name:
            raise MigrationError(ERROR_MESSAGES["missing_column_in_drop_column"])

        if not is_valid_identifier(self.column_name):
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=self.column_name))

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the drop column operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if column exists
        if not db.column_exists(self.table, self.column_name):
            raise MigrationError(ERROR_MESSAGES["column_not_found"].format(
                column=self.column_name, table=self.table
            ))

        # Get all columns in the table
        columns = db.get_table_columns(self.table)

        # Check if this is the only column
        if len(columns) == 1:
            raise MigrationError(ERROR_MESSAGES["cannot_drop_only_column"].format(
                column=self.column_name, table=self.table
            ))

        # Check if it's a primary key column
        if self.column_name in db.get_primary_key_columns(self.table):
            raise MigrationError(ERROR_MESSAGES["cannot_drop_primary_key"].format(
                column=self.column_name
            ))

        # Perform the drop by recreating the table
        self._drop_column_by_recreation(db)

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "column": self.column_name,
            "version": db.current_version
        }

    def _drop_column_by_recreation(self, db: 'MigrationDatabase'):
        """Drop a column by recreating the table without it."""
        table = self.table
        column_to_drop = self.column_name

        # Get all columns except the one to drop
        columns = db.get_table_columns(table)
        columns_to_keep = [col for col in columns if col != column_to_drop]

        # Get primary key info
        pk_columns = db.get_primary_key_columns(table)

        # Get table info for recreate
        table_info = db.get_table_info(table)

        # Create new table name
        temp_table = f"{table}_temp"

        # Build CREATE TABLE statement for new table
        column_defs = []
        for col_name in columns_to_keep:
            col_info = table_info[col_name]
            def_parts = [escape_identifier(col_name), col_info['type']]

            if col_name in pk_columns:
                def_parts.append("PRIMARY KEY")
                if col_info.get('auto_increment'):
                    def_parts.append("AUTOINCREMENT")

            if col_info.get('not_null'):
                def_parts.append("NOT NULL")

            if col_info.get('unique'):
                def_parts.append("UNIQUE")

            if col_info.get('default'):
                def_parts.append(f"DEFAULT {col_info['default']}")

            column_defs.append(" ".join(def_parts))

        create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(column_defs)})"
        db.execute(create_sql)

        # Copy data to new table
        cols_str = ", ".join(escape_identifier(col) for col in columns_to_keep)
        insert_sql = f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(table)}"
        db.execute(insert_sql)

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(table)}")


def create_operation(definition: dict) -> Operation:
    """Factory function to create operation instances."""
    op_type = definition.get('type')

    if op_type == "create_table":
        return CreateTableOperation(definition)
    elif op_type == "add_column":
        return AddColumnOperation(definition)
    elif op_type == "drop_column":
        return DropColumnOperation(definition)
    else:
        raise MigrationError(f"Error: unknown operation type: '{op_type}'")


class MigrationDatabase:
    """Handles database operations and migration tracking."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None
        self.current_version = None

    def connect(self):
        """Connect to the database and ensure _migrations table exists."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self._create_migrations_table()

    def close(self):
        """Close the database connection."""
        if self.conn:
            self.conn.close()

    def _create_migrations_table(self):
        """Create the _migrations table if it doesn't exist."""
        sql = """
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        self.execute(sql)

    def execute(self, sql: str):
        """Execute a SQL statement."""
        try:
            self.conn.execute(sql)
            self.conn.commit()
        except sqlite3.Error as e:
            self.conn.rollback()
            raise MigrationError(ERROR_MESSAGES["sql_error"].format(error=str(e)))

    def get_applied_migrations(self) -> set:
        """Get set of already applied migration versions."""
        cursor = self.conn.execute("SELECT version FROM _migrations ORDER BY version")
        return {row[0] for row in cursor.fetchall()}

    def record_migration(self, version: int, description: str):
        """Record a completed migration."""
        sql = "INSERT INTO _migrations (version, description) VALUES (?, ?)"
        self.conn.execute(sql, (version, description))
        self.conn.commit()

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists."""
        sql = "SELECT name FROM sqlite_master WHERE type='table' AND name=?"
        cursor = self.conn.execute(sql, (table_name,))
        return cursor.fetchone() is not None

    def column_exists(self, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table."""
        columns = self.get_table_columns(table_name)
        return column_name in columns

    def get_table_columns(self, table_name: str) -> list:
        """Get list of column names for a table."""
        cursor = self.conn.execute(f"PRAGMA table_info({escape_identifier(table_name)})")
        return [row[1] for row in cursor.fetchall()]

    def get_table_info(self, table_name: str) -> dict:
        """Get detailed info about table columns."""
        # Get table SQL to detect AUTOINCREMENT
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        table_sql = row[0] if row else ''

        cursor = self.conn.execute(f"PRAGMA table_info({escape_identifier(table_name)})")
        info = {}
        for row in cursor.fetchall():
            cid, name, type_str, not_null, default_val, pk = row
            # Check if column has AUTOINCREMENT in the original CREATE TABLE statement
            auto_inc = False
            if name:
                # Look for AUTOINCREMENT in the CREATE TABLE statement
                import re
                # Pattern to find column definition with AUTOINCREMENT
                pattern = rf'{escape_identifier(name)}\s+{type_str}\s+PRIMARY\s+KEY\s+AUTOINCREMENT'
                if re.search(pattern, table_sql, re.IGNORECASE):
                    auto_inc = True
                # Also check simpler pattern without escaping
                pattern2 = rf'{name}\s+{type_str}\s+PRIMARY\s+KEY.*AUTOINCREMENT'
                if not auto_inc and re.search(pattern2, table_sql, re.IGNORECASE):
                    auto_inc = True

            info[name] = {
                'type': type_str,
                'not_null': bool(not_null),
                'default': default_val,
                'pk': bool(pk),
                'auto_increment': auto_inc
            }
        return info

    def get_primary_key_columns(self, table_name: str) -> list:
        """Get list of primary key column names for a table."""
        info = self.get_table_info(table_name)
        return [name for name, col_info in info.items() if col_info['pk']]


def parse_migration_file(file_path: str) -> dict:
    """Parse and validate a migration file."""
    if not os.path.exists(file_path):
        raise MigrationError(ERROR_MESSAGES["file_not_found"].format(file=file_path))

    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        raise MigrationError(ERROR_MESSAGES["invalid_json"])

    # Validate required fields
    if 'version' not in data:
        raise MigrationError(ERROR_MESSAGES["missing_version"])
    if 'description' not in data:
        raise MigrationError(ERROR_MESSAGES["missing_description"])
    if 'operations' not in data:
        raise MigrationError(ERROR_MESSAGES["missing_operations"])

    # Validate field types
    if not isinstance(data['version'], int):
        raise MigrationError(ERROR_MESSAGES["invalid_version_type"])
    if not isinstance(data['description'], str):
        raise MigrationError(ERROR_MESSAGES["invalid_description_type"])
    if not isinstance(data['operations'], list):
        raise MigrationError(ERROR_MESSAGES["invalid_operations_type"])

    # Validate version is positive
    if data['version'] <= 0:
        raise MigrationError("Error: migration version must be a positive integer")

    return data


def apply_migration(migration_data: dict, db: MigrationDatabase):
    """Apply a migration and output events as JSON lines."""
    version = migration_data['version']
    description = migration_data['description']
    operations_data = migration_data['operations']

    # Check if already applied
    applied_versions = db.get_applied_migrations()
    if version in applied_versions:
        print(ERROR_MESSAGES["migration_already_applied"].format(version=version), file=sys.stderr)
        print(json.dumps({
            "event": "migration_skipped",
            "version": version,
            "reason": "already_applied"
        }))
        return

    # Set current version for operations
    db.current_version = version

    # Create operation instances
    operations = []
    for op_data in operations_data:
        try:
            op = create_operation(op_data)
            operations.append(op)
        except MigrationError as e:
            raise e

    # Execute operations
    for op in operations:
        event = op.execute(db)
        print(json.dumps(event))

    # Record migration completion
    db.record_migration(version, description)

    print(json.dumps({
        "event": "migration_complete",
        "version": version,
        "operations_count": len(operations)
    }))


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Database Migration Tool for SQLite"
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database_file', help='Path to SQLite database file')

    args = parser.parse_args()

    try:
        # Parse migration file
        migration_data = parse_migration_file(args.migration_file)

        # Connect to database
        db = MigrationDatabase(args.database_file)
        db.connect()

        try:
            # Apply migration
            apply_migration(migration_data, db)
        finally:
            db.close()

    except MigrationError as e:
        print(e.message, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
