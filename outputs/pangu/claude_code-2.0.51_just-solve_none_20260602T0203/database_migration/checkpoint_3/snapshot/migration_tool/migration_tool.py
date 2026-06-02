#!/usr/bin/env python3
"""
Database Migration Tool for SQLite.
Applies schema migrations from JSON files to SQLite databases.
"""

import argparse
import json
import os
import re
import sys
import sqlite3

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
    "missing_transformations": "Error: invalid operation 'transform_data': missing required field 'transformations'",
    "invalid_transformations_type": "Error: invalid operation 'transform_data': 'transformations' must be an array",
    "missing_column_in_transformation": "Error: invalid operation 'transform_data': transformation missing required field 'column'",
    "missing_expression_in_transformation": "Error: invalid operation 'transform_data': transformation missing required field 'expression'",
    "missing_from_column": "Error: invalid operation 'migrate_column_data': missing required field 'from_column'",
    "missing_to_column": "Error: invalid operation 'migrate_column_data': missing required field 'to_column'",
    "missing_column_in_backfill": "Error: invalid operation 'backfill_data': missing required field 'column'",
    "missing_value_in_backfill": "Error: invalid operation 'backfill_data': missing required field 'value'",
    "column_not_found_for_migration": "Error: invalid operation: column '{column}' does not exist in table '{table}'",
    "sql_execution_error": "Error: SQL execution error: {error}",
    "foreign_key_violation": "Error: foreign key violation: referenced row in '{table}' table does not exist",
    "index_already_exists": "Error: index '{name}' already exists",
    "index_not_found": "Error: index '{name}' does not exist",
    "foreign_key_not_found": "Error: foreign key '{name}' does not exist in table '{table}'",
    "foreign_key_already_exists": "Error: foreign key '{name}' already exists in table '{table}'",
    "check_constraint_already_exists": "Error: check constraint '{name}' already exists in table '{table}'",
    "check_constraint_not_found": "Error: check constraint '{name}' does not exist in table '{table}'",
    "invalid_on_delete_action": "Error: invalid on_delete action '{action}'. Valid values: CASCADE, RESTRICT, SET NULL, NO ACTION, SET DEFAULT",
    "invalid_on_update_action": "Error: invalid on_update action '{action}'. Valid values: CASCADE, RESTRICT, SET NULL, NO ACTION, SET DEFAULT",
    "referenced_table_not_found": "Error: cannot add foreign key: referenced table '{table}' does not exist",
    "referenced_column_not_found": "Error: cannot add foreign key: referenced column '{column}' does not exist",
    "column_count_mismatch": "Error: cannot add foreign key: column count mismatch (referencing: {local_count}, referenced: {referenced_count})",
    "circular_dependency": "Error: cannot add foreign key: circular dependency detected with table '{table}'",
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


class TransformDataOperation(Operation):
    """Operation to transform data in a table using SQL expressions."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.transformations = definition.get('transformations', [])

        if self.transformations is None:
            raise MigrationError(ERROR_MESSAGES["missing_transformations"])

        if not isinstance(self.transformations, list):
            raise MigrationError(ERROR_MESSAGES["invalid_transformations_type"])

        if not self.transformations:
            raise MigrationError("Error: invalid operation 'transform_data': transformations array cannot be empty")

        # Validate each transformation
        self.validated_transformations = []
        for t in self.transformations:
            if not t.get('column'):
                raise MigrationError(ERROR_MESSAGES["missing_column_in_transformation"])
            if not t.get('expression'):
                raise MigrationError(ERROR_MESSAGES["missing_expression_in_transformation"])

            column_name = t['column']
            expression = t['expression']

            if not is_valid_identifier(column_name):
                raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=column_name))

            self.validated_transformations.append({
                'column': column_name,
                'expression': expression
            })

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the transform data operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Apply each transformation in order
        for transformation in self.validated_transformations:
            column = transformation['column']
            expression = transformation['expression']

            # Check if column exists
            if not db.column_exists(self.table, column):
                raise MigrationError(ERROR_MESSAGES["column_not_found_for_migration"].format(
                    column=column, table=self.table
                ))

            # Update the column with the expression
            sql = f"UPDATE {escape_identifier(self.table)} SET {escape_identifier(column)} = ({expression})"
            try:
                db.conn.execute(sql)
            except sqlite3.Error as e:
                raise MigrationError(ERROR_MESSAGES["sql_execution_error"].format(error=str(e)))

        db.conn.commit()

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "version": db.current_version
        }


class MigrateColumnDataOperation(Operation):
    """Operation to migrate data from one column to another."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.from_column = definition.get('from_column')
        self.to_column = definition.get('to_column')
        self.default_value = definition.get('default_value')

        if not self.from_column:
            raise MigrationError(ERROR_MESSAGES["missing_from_column"])
        if not self.to_column:
            raise MigrationError(ERROR_MESSAGES["missing_to_column"])

        if not is_valid_identifier(self.from_column):
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=self.from_column))
        if not is_valid_identifier(self.to_column):
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=self.to_column))

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the migrate column data operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if both columns exist
        if not db.column_exists(self.table, self.from_column):
            raise MigrationError(ERROR_MESSAGES["column_not_found_for_migration"].format(
                column=self.from_column, table=self.table
            ))
        if not db.column_exists(self.table, self.to_column):
            raise MigrationError(ERROR_MESSAGES["column_not_found_for_migration"].format(
                column=self.to_column, table=self.table
            ))

        # Build the UPDATE statement
        # Use COALESCE to handle NULL values with default_value if provided
        if self.default_value is not None:
            # If default_value is provided, use it for rows where from_column is NULL
            set_clause = f"{escape_identifier(self.to_column)} = COALESCE({escape_identifier(self.from_column)}, {self.default_value})"
        else:
            # Just copy from from_column to to_column
            set_clause = f"{escape_identifier(self.to_column)} = {escape_identifier(self.from_column)}"

        sql = f"UPDATE {escape_identifier(self.table)} SET {set_clause}"
        try:
            db.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(ERROR_MESSAGES["sql_execution_error"].format(error=str(e)))

        db.conn.commit()

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "from_column": self.from_column,
            "to_column": self.to_column,
            "version": db.current_version
        }


class BackfillDataOperation(Operation):
    """Operation to backfill a column with computed or constant values."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.column = definition.get('column')
        self.value = definition.get('value')
        self.where = definition.get('where')

        if not self.column:
            raise MigrationError(ERROR_MESSAGES["missing_column_in_backfill"])
        if self.value is None:
            raise MigrationError(ERROR_MESSAGES["missing_value_in_backfill"])

        if not is_valid_identifier(self.column):
            raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=self.column))

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the backfill data operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if column exists
        if not db.column_exists(self.table, self.column):
            raise MigrationError(ERROR_MESSAGES["column_not_found_for_migration"].format(
                column=self.column, table=self.table
            ))

        # Build the UPDATE statement
        set_clause = f"{escape_identifier(self.column)} = {self.value}"

        if self.where:
            sql = f"UPDATE {escape_identifier(self.table)} SET {set_clause} WHERE {self.where}"
        else:
            sql = f"UPDATE {escape_identifier(self.table)} SET {set_clause}"

        try:
            db.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(ERROR_MESSAGES["sql_execution_error"].format(error=str(e)))

        db.conn.commit()

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "column": self.column,
            "version": db.current_version
        }


class AddForeignKeyOperation(Operation):
    """Operation to add a foreign key constraint to a table."""

    VALID_ON_ACTIONS = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.name = definition.get('name')
        if not self.name:
            raise MigrationError("Error: invalid operation 'add_foreign_key': missing required field 'name'")
        self.columns = definition.get('columns', [])
        self.references = definition.get('references', {})
        self.on_delete = definition.get('on_delete', 'NO ACTION')
        self.on_update = definition.get('on_update', 'NO ACTION')

        # Validate required fields
        if not self.columns:
            raise MigrationError("Error: invalid operation 'add_foreign_key': missing required field 'columns'")
        if not isinstance(self.columns, list):
            raise MigrationError("Error: invalid operation 'add_foreign_key': 'columns' must be an array")
        if not self.references:
            raise MigrationError("Error: invalid operation 'add_foreign_key': missing required field 'references'")
        if not isinstance(self.references, dict):
            raise MigrationError("Error: invalid operation 'add_foreign_key': 'references' must be an object")

        ref_table = self.references.get('table')
        ref_columns = self.references.get('columns', [])
        if not ref_table:
            raise MigrationError("Error: invalid operation 'add_foreign_key': missing required field 'references.table'")
        if not ref_columns:
            raise MigrationError("Error: invalid operation 'add_foreign_key': missing required field 'references.columns'")

        # Validate names
        if not is_valid_identifier(ref_table):
            raise MigrationError(ERROR_MESSAGES["invalid_table_name"].format(name=ref_table))
        if not is_valid_identifier(self.name):
            raise MigrationError(f"Error: invalid constraint name: '{self.name}' (must be alphanumeric + underscore, starting with letter/underscore)")

        for col in self.columns + [ref_table] + ref_columns:
            if not is_valid_identifier(col):
                raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=col))

        # Validate on_delete and on_update
        if self.on_delete.upper() not in self.VALID_ON_ACTIONS:
            raise MigrationError(
                ERROR_MESSAGES["invalid_on_delete_action"].format(action=self.on_delete)
            )
        if self.on_update.upper() not in self.VALID_ON_ACTIONS:
            raise MigrationError(
                ERROR_MESSAGES["invalid_on_update_action"].format(action=self.on_update)
            )

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the add foreign key operation."""
        ref_table = self.references['table']
        ref_columns = self.references['columns']

        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if referenced table exists
        if not db.table_exists(ref_table):
            raise MigrationError(ERROR_MESSAGES["referenced_table_not_found"].format(table=ref_table))

        # Check if foreign key already exists
        if db.foreign_key_exists(self.table, self.name):
            raise MigrationError(ERROR_MESSAGES["foreign_key_already_exists"].format(
                name=self.name, table=self.table
            ))

        # Check column counts match
        if len(self.columns) != len(ref_columns):
            raise MigrationError(ERROR_MESSAGES["column_count_mismatch"].format(
                local_count=len(self.columns), referenced_count=len(ref_columns)
            ))

        # Check that all referencing columns exist
        for col in self.columns:
            if not db.column_exists(self.table, col):
                raise MigrationError(ERROR_MESSAGES["column_not_found"].format(
                    column=col, table=self.table
                ))

        # Check that all referenced columns exist
        for col in ref_columns:
            if not db.column_exists(ref_table, col):
                raise MigrationError(ERROR_MESSAGES["referenced_column_not_found"].format(
                    column=col
                ))

        # Check for circular dependencies
        if db.detect_circular_foreign_key(self.table, self.columns, ref_table):
            raise MigrationError(ERROR_MESSAGES["circular_dependency"].format(table=ref_table))

        # Check column type compatibility
        for local_col, ref_col in zip(self.columns, ref_columns):
            local_type = db.get_column_type(self.table, local_col)
            ref_type = db.get_column_type(ref_table, ref_col)
            if local_type is not None and ref_type is not None:
                if not db.is_column_type_compatible(local_type, ref_type):
                    raise MigrationError(
                        f"Error: cannot add foreign key: column type mismatch "
                        f"(referencing column '{local_col}' is {local_type}, "
                        f"referenced column '{ref_col}' is {ref_type})"
                    )

        # Verify existing data has valid references (if table has data)
        if not db.verify_foreign_key_references_valid(self.table, self.columns, ref_table, ref_columns):
            raise MigrationError(ERROR_MESSAGES["foreign_key_violation"].format(table=ref_table))

        # Add the foreign key constraint by recreating the table (SQLite limitation)
        table_info = db.get_table_info(self.table)
        existing_fks = db.get_foreign_keys(self.table)
        existing_checks = db.get_check_constraints(self.table)
        existing_indexes = db.get_indexes_on_table(self.table)

        # Build column definitions
        column_defs = []
        for col_name, col_info in table_info.items():
            parts = [escape_identifier(col_name), col_info['type']]

            if col_info['pk']:
                parts.append("PRIMARY KEY")
                if col_info.get('auto_increment'):
                    parts.append("AUTOINCREMENT")

            if col_info['not_null']:
                parts.append("NOT NULL")

            if col_info.get('unique'):
                parts.append("UNIQUE")

            if col_info.get('default') is not None:
                parts.append(f"DEFAULT {col_info['default']}")

            column_defs.append(" ".join(parts))

        # Add existing foreign keys
        for fk in existing_fks:
            fk_def = f"""FOREIGN KEY ({escape_identifier(fk[3])})
                    REFERENCES {escape_identifier(fk[2])} ({escape_identifier(fk[4])})
                    ON DELETE {fk[6] if len(fk) > 6 else 'NO ACTION'}
                    ON UPDATE {fk[5] if len(fk) > 5 else 'NO ACTION'}"""
            column_defs.append(fk_def)

        # Add the new foreign key constraint
        fk_sql = f"""FOREIGN KEY ({', '.join(escape_identifier(c) for c in self.columns)})
                    REFERENCES {escape_identifier(ref_table)} ({', '.join(escape_identifier(c) for c in ref_columns)})
                    ON DELETE {self.on_delete.upper()}
                    ON UPDATE {self.on_update.upper()}"""
        column_defs.append(fk_sql)

        # Add existing check constraints
        for check in existing_checks:
            column_defs.append(f"CONSTRAINT {escape_identifier(check['name'])} CHECK ({check['expression']})")

        temp_table = f"{self.table}_temp"
        create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(column_defs)})"
        db.execute(create_sql)

        # Copy data
        cols_str = ", ".join(escape_identifier(col) for col in table_info.keys())
        db.execute(f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(self.table)}")

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(self.table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(self.table)}")

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "name": self.name,
            "version": db.current_version
        }


class DropForeignKeyOperation(Operation):
    """Operation to drop a foreign key constraint from a table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.name = definition.get('name')
        if not self.name:
            raise MigrationError("Error: invalid operation 'drop_foreign_key': missing required field 'name'")

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the drop foreign key operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if foreign key exists
        if not db.foreign_key_exists(self.table, self.name):
            raise MigrationError(ERROR_MESSAGES["foreign_key_not_found"].format(
                name=self.name, table=self.table
            ))

        # Store current state before modification
        table_info = db.get_table_info(self.table)
        pk_columns = db.get_primary_key_columns(self.table)
        all_fks = db.get_foreign_keys(self.table)
        existing_checks = db.get_check_constraints(self.table)
        existing_indexes = db.get_indexes_on_table(self.table)

        # Find the FK to remove
        fk_to_remove_sql = None
        remaining_fks = []

        # Get the full CREATE TABLE statement
        cursor = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (self.table,)
        )
        row = cursor.fetchone()
        original_sql = row[0] if row else ''

        # Parse the FK definition to get reference details
        for fk in all_fks:
            # fk: (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
            fk_name_pattern = f"fk_{self.table}_{fk[3]}"

            if fk_name_pattern == self.name or f"CONSTRAINT {self.name}" in original_sql:
                # This is the FK to remove
                fk_to_remove_sql = {
                    'from_col': fk[3],
                    'to_col': fk[4],
                    'ref_table': fk[2]
                }
            else:
                remaining_fks.append(fk)

        if fk_to_remove_sql is None:
            # Try to parse from the SQL
            pass

        # Build the new CREATE TABLE statement without the FK
        def build_schema_without_fk(table_info, pk_columns):
            """Build CREATE TABLE statement without the dropped foreign key."""
            column_defs = []
            for col_name, col_info in table_info.items():
                parts = [escape_identifier(col_name), col_info['type']]

                if col_info['pk']:
                    parts.append("PRIMARY KEY")
                    if col_info.get('auto_increment'):
                        parts.append("AUTOINCREMENT")

                if col_info['not_null']:
                    parts.append("NOT NULL")

                if col_info.get('unique'):
                    parts.append("UNIQUE")

                if col_info.get('default') is not None:
                    parts.append(f"DEFAULT {col_info['default']}")

                column_defs.append(" ".join(parts))

            # Add remaining foreign keys
            for fk in remaining_fks:
                fk_name = f"fk_{self.table}_{fk[3]}"
                # Parse the FK from the original SQL
                ref_table, ref_from, ref_to = fk[2], fk[3], fk[4]
                fk_def = f"""FOREIGN KEY ({escape_identifier(ref_from)})
                        REFERENCES {escape_identifier(ref_table)} ({escape_identifier(ref_to)})
                        ON DELETE {fk[6] if len(fk) > 6 else 'NO ACTION'}
                        ON UPDATE {fk[5] if len(fk) > 5 else 'NO ACTION'}"""
                column_defs.append(fk_def)

            create_sql = f"CREATE TABLE {escape_identifier('_temp')} ({', '.join(column_defs)})"
            return create_sql

        # Recreate the table without the foreign key
        temp_table = f"{self.table}_temp"

        # Build new CREATE TABLE without the FK
        column_defs = []
        for col_name, col_info in table_info.items():
            parts = [escape_identifier(col_name), col_info['type']]

            if col_info['pk']:
                parts.append("PRIMARY KEY")
                if col_info.get('auto_increment'):
                    parts.append("AUTOINCREMENT")

            if col_info['not_null']:
                parts.append("NOT NULL")

            if col_info.get('unique'):
                parts.append("UNIQUE")

            if col_info.get('default') is not None:
                parts.append(f"DEFAULT {col_info['default']}")

            column_defs.append(" ".join(parts))

        # Add remaining foreign keys
        for fk in remaining_fks:
            fk_def = f"""FOREIGN KEY ({escape_identifier(fk[3])})
                    REFERENCES {escape_identifier(fk[2])} ({escape_identifier(fk[4])})
                    ON DELETE {fk[6] if len(fk) > 6 else 'NO ACTION'}
                    ON UPDATE {fk[5] if len(fk) > 5 else 'NO ACTION'}"""
            column_defs.append(fk_def)

        # Create temp table
        create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(column_defs)})"
        db.execute(create_sql)

        # Copy data
        cols_str = ", ".join(escape_identifier(col) for col in table_info.keys())
        db.execute(f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(self.table)}")

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(self.table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(self.table)}")

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "name": self.name,
            "version": db.current_version
        }


class CreateIndexOperation(Operation):
    """Operation to create an index on a table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.index_name = definition.get('name')
        self.index_columns = definition.get('columns', [])
        self.unique = definition.get('unique', False)

        if not self.index_name:
            raise MigrationError("Error: invalid operation 'create_index': missing required field 'name'")
        if not isinstance(self.index_columns, list) or not self.index_columns:
            raise MigrationError("Error: invalid operation 'create_index': 'columns' must be a non-empty array")

        for col in self.index_columns:
            if not is_valid_identifier(col):
                raise MigrationError(ERROR_MESSAGES["invalid_column_name"].format(name=col))

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the create index operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if index already exists
        if db.index_exists(self.index_name):
            raise MigrationError(ERROR_MESSAGES["index_already_exists"].format(name=self.index_name))

        # Check that all columns exist
        for col in self.index_columns:
            if not db.column_exists(self.table, col):
                raise MigrationError(ERROR_MESSAGES["column_not_found"].format(
                    column=col, table=self.table
                ))

        # Build SQL
        cols_str = ", ".join(escape_identifier(col) for col in self.index_columns)
        unique_keyword = "UNIQUE" if self.unique else ""
        sql = f"CREATE {unique_keyword} INDEX {escape_identifier(self.index_name)} ON {escape_identifier(self.table)} ({cols_str})"

        db.execute(sql)

        return {
            "event": "operation_applied",
            "type": self.type,
            "name": self.index_name,
            "table": self.table,
            "version": db.current_version
        }


class DropIndexOperation(Operation):
    """Operation to drop an index."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.index_name = definition.get('name')
        if not self.index_name:
            raise MigrationError("Error: invalid operation 'drop_index': missing required field 'name'")

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the drop index operation."""
        # Check if index exists
        if not db.index_exists(self.index_name):
            raise MigrationError(ERROR_MESSAGES["index_not_found"].format(name=self.index_name))

        # Drop the index
        sql = f"DROP INDEX {escape_identifier(self.index_name)}"
        db.execute(sql)

        return {
            "event": "operation_applied",
            "type": self.type,
            "name": self.index_name,
            "version": db.current_version
        }


class AddCheckConstraintOperation(Operation):
    """Operation to add a check constraint to a table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.constraint_name = definition.get('name')
        self.expression = definition.get('expression')

        if not self.constraint_name:
            raise MigrationError("Error: invalid operation 'add_check_constraint': missing required field 'name'")
        if not self.expression:
            raise MigrationError("Error: invalid operation 'add_check_constraint': missing required field 'expression'")

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the add check constraint operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if constraint already exists
        if db.check_constraint_exists(self.table, self.constraint_name):
            raise MigrationError(ERROR_MESSAGES["check_constraint_already_exists"].format(
                name=self.constraint_name, table=self.table
            ))

        # Recreate the table with the new check constraint
        table_info = db.get_table_info(self.table)
        pk_columns = db.get_primary_key_columns(self.table)
        existing_fks = db.get_foreign_keys(self.table)
        existing_checks = db.get_check_constraints(self.table)
        existing_indexes = db.get_indexes_on_table(self.table)

        # Build new CREATE TABLE with the new check constraint
        column_defs = []
        for col_name, col_info in table_info.items():
            parts = [escape_identifier(col_name), col_info['type']]

            if col_info['pk']:
                parts.append("PRIMARY KEY")
                if col_info.get('auto_increment'):
                    parts.append("AUTOINCREMENT")

            if col_info['not_null']:
                parts.append("NOT NULL")

            if col_info.get('unique'):
                parts.append("UNIQUE")

            if col_info.get('default') is not None:
                parts.append(f"DEFAULT {col_info['default']}")

            column_defs.append(" ".join(parts))

        # Add existing foreign keys
        for fk in existing_fks:
            fk_def = f"""FOREIGN KEY ({escape_identifier(fk[3])})
                    REFERENCES {escape_identifier(fk[2])} ({escape_identifier(fk[4])})
                    ON DELETE {fk[6] if len(fk) > 6 else 'NO ACTION'}
                    ON UPDATE {fk[5] if len(fk) > 5 else 'NO ACTION'}"""
            column_defs.append(fk_def)

        # Add existing check constraints plus the new one
        for check in existing_checks:
            column_defs.append(f"CONSTRAINT {escape_identifier(check['name'])} CHECK ({check['expression']})")

        column_defs.append(f"CONSTRAINT {escape_identifier(self.constraint_name)} CHECK ({self.expression})")

        temp_table = f"{self.table}_temp"
        create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(column_defs)})"
        db.execute(create_sql)

        # Copy data
        cols_str = ", ".join(escape_identifier(col) for col in table_info.keys())
        db.execute(f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(self.table)}")

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(self.table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(self.table)}")

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "name": self.constraint_name,
            "version": db.current_version
        }


class DropCheckConstraintOperation(Operation):
    """Operation to drop a check constraint from a table."""

    def __init__(self, definition: dict):
        super().__init__(definition)
        self.constraint_name = definition.get('name')
        if not self.constraint_name:
            raise MigrationError("Error: invalid operation 'drop_check_constraint': missing required field 'name'")

    def execute(self, db: 'MigrationDatabase') -> dict:
        """Execute the drop check constraint operation."""
        # Check if table exists
        if not db.table_exists(self.table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=self.table))

        # Check if constraint exists
        if not db.check_constraint_exists(self.table, self.constraint_name):
            raise MigrationError(ERROR_MESSAGES["check_constraint_not_found"].format(
                name=self.constraint_name, table=self.table
            ))

        # Store current state
        table_info = db.get_table_info(self.table)
        pk_columns = db.get_primary_key_columns(self.table)
        existing_fks = db.get_foreign_keys(self.table)
        existing_checks = db.get_check_constraints(self.table)
        existing_indexes = db.get_indexes_on_table(self.table)

        # Build new CREATE TABLE without the dropped constraint
        column_defs = []
        for col_name, col_info in table_info.items():
            parts = [escape_identifier(col_name), col_info['type']]

            if col_info['pk']:
                parts.append("PRIMARY KEY")
                if col_info.get('auto_increment'):
                    parts.append("AUTOINCREMENT")

            if col_info['not_null']:
                parts.append("NOT NULL")

            if col_info.get('unique'):
                parts.append("UNIQUE")

            if col_info.get('default') is not None:
                parts.append(f"DEFAULT {col_info['default']}")

            column_defs.append(" ".join(parts))

        # Add existing foreign keys
        for fk in existing_fks:
            fk_def = f"""FOREIGN KEY ({escape_identifier(fk[3])})
                    REFERENCES {escape_identifier(fk[2])} ({escape_identifier(fk[4])})
                    ON DELETE {fk[6] if len(fk) > 6 else 'NO ACTION'}
                    ON UPDATE {fk[5] if len(fk) > 5 else 'NO ACTION'}"""
            column_defs.append(fk_def)

        # Add remaining check constraints
        for check in existing_checks:
            if check['name'] != self.constraint_name:
                column_defs.append(f"CONSTRAINT {escape_identifier(check['name'])} CHECK ({check['expression']})")

        temp_table = f"{self.table}_temp"
        create_sql = f"CREATE TABLE {escape_identifier(temp_table)} ({', '.join(column_defs)})"
        db.execute(create_sql)

        # Copy data
        cols_str = ", ".join(escape_identifier(col) for col in table_info.keys())
        db.execute(f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(self.table)}")

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(self.table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(self.table)}")

        return {
            "event": "operation_applied",
            "type": self.type,
            "table": self.table,
            "name": self.constraint_name,
            "version": db.current_version
        }


def create_operation(definition: dict) -> Operation:
    """Factory function to create operation instances."""
    op_type = definition.get('type')

    if op_type == "create_table":
        return CreateTableOperation(definition)
    elif op_type == "add_column":
        return AddColumnOperation(definition)
    elif op_type == "drop_column":
        return DropColumnOperation(definition)
    elif op_type == "transform_data":
        return TransformDataOperation(definition)
    elif op_type == "migrate_column_data":
        return MigrateColumnDataOperation(definition)
    elif op_type == "backfill_data":
        return BackfillDataOperation(definition)
    elif op_type == "add_foreign_key":
        return AddForeignKeyOperation(definition)
    elif op_type == "drop_foreign_key":
        return DropForeignKeyOperation(definition)
    elif op_type == "create_index":
        return CreateIndexOperation(definition)
    elif op_type == "drop_index":
        return DropIndexOperation(definition)
    elif op_type == "add_check_constraint":
        return AddCheckConstraintOperation(definition)
    elif op_type == "drop_check_constraint":
        return DropCheckConstraintOperation(definition)
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
        # Enable foreign key support for migrations
        self.conn.execute("PRAGMA foreign_keys = ON")
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

    def foreign_key_exists(self, table_name: str, fk_name: str) -> bool:
        """Check if a foreign key constraint exists on a table."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if row is None:
            return False

        # Get the table's CREATE statement
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return False

        table_sql = row[0]
        # Look for CONSTRAINT name FOREIGN KEY pattern
        pattern = rf'CONSTRAINT\s+{fk_name}\s+FOREIGN\s+KEY'
        return bool(re.search(pattern, table_sql, re.IGNORECASE))

    def index_exists(self, index_name: str) -> bool:
        """Check if an index exists in the database."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        return cursor.fetchone() is not None

    def get_indexes_on_table(self, table_name: str) -> list:
        """Get list of index names for a table."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_%'",
            (table_name,)
        )
        return [row[0] for row in cursor.fetchall()]

    def get_foreign_keys(self, table_name: str) -> list:
        """Get list of foreign key constraint names for a table."""
        cursor = self.conn.execute(
            "PRAGMA foreign_key_list({})".format(escape_identifier(table_name))
        )
        fks = cursor.fetchall()
        # Return foreign key info (id, seq, table, from, to, on_update, on_delete, match)
        return fks

    def get_check_constraints(self, table_name: str) -> list:
        """Get list of check constraint definitions for a table."""
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            return []

        table_sql = row[0]
        # Extract CHECK CONSTRAINT definitions
        constraints = []
        pattern = rf'CONSTRAINT\s+(\w+)\s+CHECK\s*\(([^)]+)\)'
        for match in re.finditer(pattern, table_sql, re.IGNORECASE):
            constraints.append({
                'name': match.group(1),
                'expression': match.group(2)
            })
        return constraints

    def check_constraint_exists(self, table_name: str, constraint_name: str) -> bool:
        """Check if a check constraint exists on a table."""
        constraints = self.get_check_constraints(table_name)
        return any(c['name'] == constraint_name for c in constraints)

    def enable_foreign_keys(self):
        """Enable foreign key constraints for the connection."""
        self.conn.execute("PRAGMA foreign_keys = ON")

    def get_column_type(self, table_name: str, column_name: str) -> str | None:
        """Get the type of a column."""
        info = self.get_table_info(table_name)
        if column_name in info:
            return info[column_name]['type']
        return None

    def get_column_not_null(self, table_name: str, column_name: str) -> bool:
        """Check if column is NOT NULL."""
        info = self.get_table_info(table_name)
        if column_name in info:
            return info[column_name]['not_null']
        return False

    def verify_foreign_key_references_valid(self, table_name: str, columns: list, ref_table: str, ref_columns: list) -> bool:
        """Verify that all referencing data in the table has valid references in the target table."""
        # Build the query to find rows where at least one of the FK columns is not NULL,
        # but doesn't have a matching row in the reference table
        condition_parts = []
        params = []
        for i, col in enumerate(columns):
            condition_parts.append(f"{escape_identifier(col)} IS NOT NULL")
        where_null_check = " OR ".join(condition_parts)

        # Subquery to check existence in referenced table
        ref_condition = " AND ".join([
            f"{escape_identifier(col)} = {escape_identifier(ref)}"
            for col, ref in zip(columns, ref_columns)
        ])

        query = f"""
            SELECT COUNT(*) FROM {escape_identifier(table_name)} t
            WHERE ({where_null_check})
            AND NOT EXISTS (
                SELECT 1 FROM {escape_identifier(ref_table)} r
                WHERE {ref_condition}
            )
        """
        try:
            cursor = self.conn.execute(query)
            count = cursor.fetchone()[0]
            return count == 0
        except sqlite3.Error:
            return False

    def is_column_type_compatible(self, local_type: str, ref_type: str) -> bool:
        """Check if two column types are compatible for foreign key relationship."""
        # Normalize types
        local = local_type.upper().strip()
        ref = ref_type.upper().strip()

        # Integer types
        integer_types = {'INTEGER', 'INT', 'TINYINT', 'SMALLINT', 'MEDIUMINT', 'BIGINT'}
        # Text types
        text_types = {'TEXT', 'CLOB', 'STRING', 'CHAR', 'VARCHAR', 'NVARCHAR', 'CHARACTER'}

        if local in integer_types and ref in integer_types:
            return True
        if local in text_types and ref in text_types:
            return True
        if local == ref:
            return True

        return False

    def detect_circular_foreign_key(self, table_name: str, columns: list, ref_table: str) -> bool:
        """Detect if adding a foreign key would create a circular dependency."""
        # If referencing and referenced table are the same, it's a circular dependency
        if table_name == ref_table:
            return True
        return False

    def _recreate_table_with_modifications(self, db: 'MigrationDatabase', table: str,
                                           get_new_schema_fn) -> None:
        """
        Generic helper to recreate a table with modified schema.
        Used for drop_foreign_key, add_check_constraint, and drop_check_constraint.

        Args:
            db: MigrationDatabase instance
            table: Name of table to recreate
            get_new_schema_fn: Function that takes (table_name, table_info, columns_to_drop, foreign_keys, check_constraints)
                               and returns a function that builds the new create table SQL
        """
        # Get current table info and schema
        if not db.table_exists(table):
            raise MigrationError(ERROR_MESSAGES["table_not_found"].format(table=table))

        table_info = db.get_table_info(table)
        columns = list(table_info.keys())
        pk_columns = db.get_primary_key_columns(table)

        # Get the full CREATE TABLE statement
        cursor = db.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        )
        row = cursor.fetchone()
        if row is None or row[0] is None:
            raise MigrationError(f"Error: cannot get schema for table '{table}'")

        original_sql = row[0]

        # Parse foreign keys from the original SQL
        original_fks = db.get_foreign_keys(table)

        # Parse check constraints from original SQL
        original_checks = db.get_check_constraints(table)

        # Store indexes before recreating table
        existing_indexes = db.get_indexes_on_table(table)

        # Create temporary table name
        temp_table = f"{table}_temp"

        # Get modified create table statement
        create_sql = get_new_schema_fn(table_info, pk_columns)

        # Create new table
        db.execute(create_sql)

        # Copy data
        cols_str = ", ".join(escape_identifier(col) for col in columns)
        insert_sql = f"INSERT INTO {escape_identifier(temp_table)} SELECT {cols_str} FROM {escape_identifier(table)}"
        db.execute(insert_sql)

        # Drop old table
        db.execute(f"DROP TABLE {escape_identifier(table)}")

        # Rename new table
        db.execute(f"ALTER TABLE {escape_identifier(temp_table)} RENAME TO {escape_identifier(table)}")

        # Recreate foreign keys that should be kept
        for fk in original_fks:
            # fk: (id, seq, ref_table, from_col, to_col, on_update, on_delete, match)
            ref_table = fk[2]
            from_col = fk[3]
            to_col = fk[4]
            on_update = fk[5] if len(fk) > 5 else 'NO ACTION'
            on_delete = fk[6] if len(fk) > 6 else 'NO ACTION'

            # Generate FK name
            fk_name = f"fk_{table}_{from_col}"

            sql = f"""ALTER TABLE {escape_identifier(table)}
                      ADD CONSTRAINT {escape_identifier(fk_name)}
                      FOREIGN KEY ({escape_identifier(from_col)})
                      REFERENCES {escape_identifier(ref_table)}({escape_identifier(to_col)})
                      ON DELETE {on_delete}
                      ON UPDATE {on_update}"""
            db.execute(sql)

        # Recreate check constraints
        for check in original_checks:
            # Skip constraints that should be removed
            check_name = check['name']
            expr = check['expression']

            db.execute(f"""ALTER TABLE {escape_identifier(table)}
                         ADD CONSTRAINT {escape_identifier(check_name)}
                         CHECK ({expr})""")

        # Recreate indexes
        for index_name in existing_indexes:
            # Get index definition before table was recreated
            cursor = db.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,)
            )
            idx_row = cursor.fetchone()
            if idx_row and idx_row[0]:
                # Drop the old reference since it still points to the old table name
                # We need to recreate it properly
                try:
                    # Parse table name from index SQL if needed
                    db.execute(idx_row[0])
                except MigrationError:
                    # If that fails, skip
                    pass


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
    operations = [create_operation(op_data) for op_data in operations_data]

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
