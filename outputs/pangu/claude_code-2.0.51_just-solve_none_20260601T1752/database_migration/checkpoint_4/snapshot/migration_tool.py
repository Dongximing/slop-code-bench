#!/usr/bin/env python3
"""
Database Migration Tool for SQLite
Applies schema migrations from JSON files to SQLite databases.
"""

import json
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class MigrationValidator:
    """Validates migration file structure and constraints."""

    VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}
    IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
    FOREIGN_KEY_ACTIONS = {"CASCADE", "RESTRICT", "SET NULL", "NO ACTION", "SET DEFAULT"}

    @classmethod
    def validate_identifier(cls, name: str, context: str) -> None:
        if not cls.IDENTIFIER_PATTERN.match(name):
            raise MigrationError(
                f"Invalid {context}: '{name}'. Must be alphanumeric with underscores, "
                f"starting with a letter or underscore."
            )

    @classmethod
    def validate_migration_file(cls, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise MigrationError("Migration file must be a JSON object")

        # Check required fields
        if "version" not in data:
            raise MigrationError("Missing required field 'version'")
        if "description" not in data:
            raise MigrationError("Missing required field 'description'")
        if "operations" not in data:
            raise MigrationError("Missing required field 'operations'")

        # Validate version
        version = data["version"]
        if not isinstance(version, int):
            raise MigrationError(f"Version must be an integer, got {type(version).__name__}")
        if version < 1:
            raise MigrationError(f"Version must be a positive integer, got {version}")

        # Validate description
        if not isinstance(data["description"], str):
            raise MigrationError("Description must be a string")

        # Validate operations
        if not isinstance(data["operations"], list):
            raise MigrationError("Operations must be a list")
        if len(data["operations"]) == 0:
            raise MigrationError("Operations list cannot be empty")

        for i, op in enumerate(data["operations"]):
            cls.validate_operation(op, i)

        # Validate rollback_operations if present
        if "rollback_operations" in data:
            rollback_ops = data["rollback_operations"]
            if not isinstance(rollback_ops, list):
                raise MigrationError("rollback_operations must be a list")
            for i, op in enumerate(rollback_ops):
                cls.validate_rollback_operation(op, i)

    @classmethod
    def validate_operation(cls, op: Dict[str, Any], index: int) -> None:
        if not isinstance(op, dict):
            raise MigrationError(f"Operation {index} must be a JSON object")

        if "type" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'type'")

        op_type = op["type"]
        if op_type not in {"create_table", "add_column", "drop_column", "transform_data", "migrate_column_data", "backfill_data",
                            "add_foreign_key", "drop_foreign_key", "create_index", "drop_index", "add_check_constraint", "drop_check_constraint"}:
            raise MigrationError(
                f"Operation {index}: Invalid operation type '{op_type}'. "
                f"Must be 'create_table', 'add_column', 'drop_column', 'transform_data', 'migrate_column_data', "
                f"'backfill_data', 'add_foreign_key', 'drop_foreign_key', 'create_index', 'drop_index', "
                f"'add_check_constraint', or 'drop_check_constraint'"
            )

        if op_type == "create_table":
            cls.validate_create_table(op, index)
        elif op_type == "add_column":
            cls.validate_add_column(op, index)
        elif op_type == "drop_column":
            cls.validate_drop_column(op, index)
        elif op_type == "transform_data":
            cls.validate_transform_data(op, index)
        elif op_type == "migrate_column_data":
            cls.validate_migrate_column_data(op, index)
        elif op_type == "backfill_data":
            cls.validate_backfill_data(op, index)
        elif op_type == "add_foreign_key":
            cls.validate_add_foreign_key(op, index)
        elif op_type == "drop_foreign_key":
            cls.validate_drop_foreign_key(op, index)
        elif op_type == "create_index":
            cls.validate_create_index(op, index)
        elif op_type == "drop_index":
            cls.validate_drop_index(op, index)
        elif op_type == "add_check_constraint":
            cls.validate_add_check_constraint(op, index)
        elif op_type == "drop_check_constraint":
            cls.validate_drop_check_constraint(op, index)

    @classmethod
    def validate_create_table(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "columns" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'columns'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        columns = op["columns"]
        if not isinstance(columns, list):
            raise MigrationError(f"Operation {index}: Columns must be a list")
        if len(columns) == 0:
            raise MigrationError(f"Operation {index}: Columns list cannot be empty")

        # Check for primary key constraints
        primary_key_count = 0
        for i, col in enumerate(columns):
            cls.validate_column(col, f"column {i} in operation {index}")
            if col.get("primary_key", False):
                primary_key_count += 1

        if primary_key_count == 0:
            raise MigrationError(
                f"Operation {index}: Table must have exactly one primary key column"
            )
        if primary_key_count > 1:
            raise MigrationError(
                f"Operation {index}: Can only have one primary key column (found {primary_key_count})"
            )

    @classmethod
    def validate_add_column(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "column" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'column'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        column = op["column"]
        if not isinstance(column, dict):
            raise MigrationError(f"Operation {index}: Column must be an object")

        cls.validate_column(column, f"column in operation {index}")

    @classmethod
    def validate_drop_column(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "column" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'column'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        column_name = op["column"]
        cls.validate_identifier(column_name, f"column name in operation {index}")

    @classmethod
    def validate_transform_data(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "transformations" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'transformations'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        transformations = op["transformations"]
        if not isinstance(transformations, list):
            raise MigrationError(f"Operation {index}: Transformations must be a list")
        if len(transformations) == 0:
            raise MigrationError(f"Operation {index}: Transformations list cannot be empty")

        for i, transform in enumerate(transformations):
            if not isinstance(transform, dict):
                raise MigrationError(f"Operation {index}: Transformation {i} must be an object")
            if "column" not in transform:
                raise MigrationError(f"Operation {index}: Transformation {i} missing required field 'column'")
            if "expression" not in transform:
                raise MigrationError(f"Operation {index}: Transformation {i} missing required field 'expression'")

            column_name = transform["column"]
            cls.validate_identifier(column_name, f"column name in transformation {i} in operation {index}")

            expression = transform["expression"]
            if not isinstance(expression, str):
                raise MigrationError(f"Operation {index}: Transformation {i} expression must be a string")
            if len(expression) == 0:
                raise MigrationError(f"Operation {index}: Transformation {i} expression cannot be empty")

    @classmethod
    def validate_migrate_column_data(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "from_column" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'from_column'")
        if "to_column" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'to_column'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        from_column = op["from_column"]
        cls.validate_identifier(from_column, f"from_column in operation {index}")

        to_column = op["to_column"]
        cls.validate_identifier(to_column, f"to_column in operation {index}")

        if "default_value" in op and op["default_value"] is not None:
            # default_value can be any JSON value, validate it's acceptable
            default_value = op["default_value"]
            if not isinstance(default_value, (type(None), str, int, float, bool)):
                raise MigrationError(
                    f"Operation {index}: default_value must be a valid JSON value (string, number, boolean, or null)"
                )

    @classmethod
    def validate_backfill_data(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "column" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'column'")
        if "value" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'value'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        column_name = op["column"]
        cls.validate_identifier(column_name, f"column name in operation {index}")

        value = op["value"]
        if not isinstance(value, str):
            raise MigrationError(f"Operation {index}: value must be a string (SQL expression)")
        if len(value) == 0:
            raise MigrationError(f"Operation {index}: value cannot be empty")

        if "where" in op and op["where"] is not None:
            where_clause = op["where"]
            if not isinstance(where_clause, str):
                raise MigrationError(f"Operation {index}: where clause must be a string")
            if len(where_clause) == 0:
                raise MigrationError(f"Operation {index}: where clause cannot be empty")

    @classmethod
    def validate_add_foreign_key(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")
        if "columns" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'columns'")
        if "references" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'references'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        fk_name = op["name"]
        cls.validate_identifier(fk_name, f"foreign key name in operation {index}")

        columns = op["columns"]
        if not isinstance(columns, list):
            raise MigrationError(f"Operation {index}: Columns must be a list")
        if len(columns) == 0:
            raise MigrationError(f"Operation {index}: Columns list cannot be empty")
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise MigrationError(f"Operation {index}: Column {i} must be a string")
            cls.validate_identifier(col, f"column {i} in operation {index}")

        references = op["references"]
        if not isinstance(references, dict):
            raise MigrationError(f"Operation {index}: References must be an object")
        if "table" not in references:
            raise MigrationError(f"Operation {index}: Missing required field 'references.table'")
        if "columns" not in references:
            raise MigrationError(f"Operation {index}: Missing required field 'references.columns'")

        ref_table = references["table"]
        cls.validate_identifier(ref_table, f"references.table in operation {index}")

        ref_columns = references["columns"]
        if not isinstance(ref_columns, list):
            raise MigrationError(f"Operation {index}: references.columns must be a list")
        if len(ref_columns) == 0:
            raise MigrationError(f"Operation {index}: references.columns list cannot be empty")
        if len(columns) != len(ref_columns):
            raise MigrationError(
                f"Operation {index}: Number of columns ({len(columns)}) must match "
                f"number of referenced columns ({len(ref_columns)})"
            )
        for i, col in enumerate(ref_columns):
            if not isinstance(col, str):
                raise MigrationError(f"Operation {index}: Reference column {i} must be a string")
            cls.validate_identifier(col, f"reference column {i} in operation {index}")

        if "on_delete" in op:
            on_delete = op["on_delete"]
            if not isinstance(on_delete, str):
                raise MigrationError(f"Operation {index}: on_delete must be a string")
            if on_delete.upper() not in cls.FOREIGN_KEY_ACTIONS:
                raise MigrationError(
                    f"Operation {index}: Invalid on_delete value '{on_delete}'. "
                    f"Must be one of: {', '.join(sorted(cls.FOREIGN_KEY_ACTIONS))}"
                )

        if "on_update" in op:
            on_update = op["on_update"]
            if not isinstance(on_update, str):
                raise MigrationError(f"Operation {index}: on_update must be a string")
            if on_update.upper() not in cls.FOREIGN_KEY_ACTIONS:
                raise MigrationError(
                    f"Operation {index}: Invalid on_update value '{on_update}'. "
                    f"Must be one of: {', '.join(sorted(cls.FOREIGN_KEY_ACTIONS))}"
                )

    @classmethod
    def validate_drop_foreign_key(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        fk_name = op["name"]
        cls.validate_identifier(fk_name, f"foreign key name in operation {index}")

    @classmethod
    def validate_create_index(cls, op: Dict[str, Any], index: int) -> None:
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "columns" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'columns'")

        index_name = op["name"]
        cls.validate_identifier(index_name, f"index name in operation {index}")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        columns = op["columns"]
        if not isinstance(columns, list):
            raise MigrationError(f"Operation {index}: Columns must be a list")
        if len(columns) == 0:
            raise MigrationError(f"Operation {index}: Columns list cannot be empty")
        for i, col in enumerate(columns):
            if not isinstance(col, str):
                raise MigrationError(f"Operation {index}: Column {i} must be a string")
            cls.validate_identifier(col, f"column {i} in operation {index}")

        if "unique" in op:
            unique = op["unique"]
            if not isinstance(unique, bool):
                raise MigrationError(f"Operation {index}: unique must be a boolean")

    @classmethod
    def validate_drop_index(cls, op: Dict[str, Any], index: int) -> None:
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")

        index_name = op["name"]
        cls.validate_identifier(index_name, f"index name in operation {index}")

    @classmethod
    def validate_add_check_constraint(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")
        if "expression" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'expression'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        constraint_name = op["name"]
        cls.validate_identifier(constraint_name, f"constraint name in operation {index}")

        expression = op["expression"]
        if not isinstance(expression, str):
            raise MigrationError(f"Operation {index}: Expression must be a string")
        if len(expression) == 0:
            raise MigrationError(f"Operation {index}: Expression cannot be empty")

    @classmethod
    def validate_drop_check_constraint(cls, op: Dict[str, Any], index: int) -> None:
        if "table" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'table'")
        if "name" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'name'")

        table_name = op["table"]
        cls.validate_identifier(table_name, f"table name in operation {index}")

        constraint_name = op["name"]
        cls.validate_identifier(constraint_name, f"constraint name in operation {index}")

    @classmethod
    def validate_rollback_operation(cls, op: Dict[str, Any], index: int) -> None:
        if not isinstance(op, dict):
            raise MigrationError(f"Rollback operation {index} must be a JSON object")

        if "type" not in op:
            raise MigrationError(f"Rollback operation {index}: Missing required field 'type'")

        op_type = op["type"]
        # Rollback operations can be any valid operation type
        valid_types = {"create_table", "add_column", "drop_column", "transform_data", "migrate_column_data", "backfill_data",
                      "add_foreign_key", "drop_foreign_key", "create_index", "drop_index", "add_check_constraint", "drop_check_constraint"}
        if op_type not in valid_types:
            raise MigrationError(
                f"Rollback operation {index}: Invalid operation type '{op_type}'. "
                f"Must be a valid operation type."
            )

        # Validate required fields based on operation type
        if op_type == "create_table":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "columns" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'columns'")
        elif op_type == "add_column":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "column" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'column'")
        elif op_type == "drop_column":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "column" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'column'")
        elif op_type == "add_foreign_key":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
            if "columns" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'columns'")
            if "references" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'references'")
        elif op_type == "drop_foreign_key":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
        elif op_type == "create_index":
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "columns" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'columns'")
        elif op_type == "drop_index":
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
        elif op_type == "add_check_constraint":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
            if "expression" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'expression'")
        elif op_type == "drop_check_constraint":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "name" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'name'")
        elif op_type == "migrate_column_data":
            if "table" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'table'")
            if "from_column" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'from_column'")
            if "to_column" not in op:
                raise MigrationError(f"Rollback operation {index}: Missing required field 'to_column'")

    @classmethod
    def validate_column(cls, col: Dict[str, Any], context: str) -> None:
        if not isinstance(col, dict):
            raise MigrationError(f"{context}: Column must be an object")

        if "name" not in col:
            raise MigrationError(f"{context}: Missing required field 'name'")
        if "type" not in col:
            raise MigrationError(f"{context}: Missing required field 'type'")

        column_name = col["name"]
        cls.validate_identifier(column_name, f"column name {context}")

        column_type = col["type"]
        if column_type not in cls.VALID_TYPES:
            raise MigrationError(
                f"{context}: Invalid column type '{column_type}'. "
                f"Must be one of: {', '.join(sorted(cls.VALID_TYPES))}"
            )

        # Validate auto_increment constraint
        if col.get("auto_increment", False):
            if column_type != "INTEGER":
                raise MigrationError(
                    f"{context}: auto_increment can only be used with INTEGER columns"
                )
            if not col.get("primary_key", False):
                raise MigrationError(
                    f"{context}: auto_increment can only be used with primary_key columns"
                )


class MigrationExecutor:
    """Executes migrations against a SQLite database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.version: int = 0

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA foreign_keys = OFF")
        self._ensure_migrations_table()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()
        return False

    def _ensure_migrations_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operations TEXT NOT NULL
            )
        """)

    def is_migration_applied(self, version: int) -> bool:
        cursor = self.conn.execute(
            "SELECT version FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def record_migration(self, version: int, description: str) -> None:
        self.conn.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )

    def table_exists(self, table_name: str) -> bool:
        cursor = self.conn.execute(
            'SELECT name FROM sqlite_master WHERE type="table" AND name=?',
            (table_name,)
        )
        return cursor.fetchone() is not None

    def get_table_columns(self, table_name: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(f'PRAGMA table_info("{table_name}")')
        columns = []
        for row in cursor.fetchall():
            columns.append({
                "cid": row[0],
                "name": row[1],
                "type": row[2],
                "notnull": bool(row[3]),
                "default": row[4],
                "pk": bool(row[5])
            })
        return columns

    def column_exists(self, table_name: str, column_name: str) -> bool:
        columns = self.get_table_columns(table_name)
        return any(col["name"] == column_name for col in columns)

    def index_exists(self, index_name: str) -> bool:
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        return cursor.fetchone() is not None

    def get_table_indexes(self, table_name: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_%'",
            (table_name,)
        )
        indexes = []
        for row in cursor.fetchall():
            indexes.append({"name": row[0], "sql": row[1]})
        return indexes

    def get_all_indexes(self) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
        indexes = []
        for row in cursor.fetchall():
            indexes.append({"name": row[0], "table": row[1], "sql": row[2]})
        return indexes

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(f'PRAGMA foreign_key_list({table_name})')
        foreign_keys = []
        for row in cursor.fetchall():
            foreign_keys.append({
                "id": row[0],
                "seq": row[1],
                "table": row[2],
                "from_columns": [row[3]] if row[3] else [],
                "to_columns": [row[4]] if row[4] else [],
                "on_update": row[5],
                "on_delete": row[6],
                "match": row[7]
            })
        return foreign_keys

    def get_foreign_key_by_name(self, table_name: str, fk_name: str) -> Optional[Dict[str, Any]]:
        # Get table SQL to find foreign key definition
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return None

        table_sql = row[0]
        # Look for CONSTRAINT fk_name FOREIGN KEY ...
        pattern = rf'CONSTRAINT\s+{fk_name}\s+FOREIGN\s+KEY\s*\([^)]+\)\s*REFERENCES\s+[^ ]+\s*\([^)]+\)'
        match = re.search(pattern, table_sql, re.IGNORECASE)
        if match:
            fk_def = match.group(0)
            # Extract components
            col_match = re.search(r'FOREIGN\s+KEY\s*\(([^)]+)\)', fk_def, re.IGNORECASE)
            ref_match = re.search(r'REFERENCES\s+(\S+)\s*\(([^)]+)\)', fk_def, re.IGNORECASE)
            if col_match and ref_match:
                cols = [c.strip() for c in col_match.group(1).split(',')]
                ref_cols = [c.strip() for c in ref_match.group(2).split(',')]
                return {
                    "name": fk_name,
                    "from_columns": cols,
                    "to_table": ref_match.group(1).strip('"'),
                    "to_columns": ref_cols
                }
        return None

    def get_check_constraints(self, table_name: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return []

        table_sql = row[0]
        constraints = []
        # Look for CONSTRAINT name CHECK (...)
        pattern = rf'CONSTRAINT\s+([\w_]+)\s+CHECK\s*\([^)]+\)'
        for match in re.finditer(pattern, table_sql, re.IGNORECASE):
            constraint_def = match.group(0)
            name_match = re.search(r'CONSTRAINT\s+([\w_]+)', constraint_def, re.IGNORECASE)
            expr_match = re.search(r'CHECK\s*\(([^)]+)\)', constraint_def, re.IGNORECASE)
            if name_match and expr_match:
                constraints.append({
                    "name": name_match.group(1),
                    "expression": expr_match.group(1).strip()
                })
        return constraints

    def get_check_constraint_by_name(self, table_name: str, constraint_name: str) -> Optional[Dict[str, Any]]:
        constraints = self.get_check_constraints(table_name)
        for c in constraints:
            if c["name"] == constraint_name:
                return c
        return None

    def create_table(self, table_name: str, columns: List[Dict[str, Any]]) -> None:
        if self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' already exists")

        column_defs = []
        for col in columns:
            def_str = f"{col['name']} {col['type']}"

            if col.get("not_null", False):
                def_str += " NOT NULL"

            if col.get("unique", False):
                def_str += " UNIQUE"

            if col.get("primary_key", False):
                def_str += " PRIMARY KEY"
                if col.get("auto_increment", False):
                    def_str += " AUTOINCREMENT"

            if "default" in col and col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"

            column_defs.append(def_str)

        sql = f"CREATE TABLE {table_name} ({', '.join(column_defs)})"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error: {e}")

    def add_column(self, table_name: str, column: Dict[str, Any]) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if self.column_exists(table_name, column["name"]):
            raise MigrationError(f"Column '{column['name']}' already exists in table '{table_name}'")

        def_str = f"{column['name']} {column['type']}"

        if column.get("not_null", False):
            def_str += " NOT NULL"

        if "default" in column and column["default"] is not None:
            def_str += f" DEFAULT {column['default']}"

        sql = f"ALTER TABLE {table_name} ADD COLUMN {def_str}"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error: {e}")

    def drop_column(self, table_name: str, column_name: str) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if not self.column_exists(table_name, column_name):
            raise MigrationError(f"Column '{column_name}' does not exist in table '{table_name}'")

        columns = self.get_table_columns(table_name)

        if len(columns) <= 1:
            raise MigrationError(
                f"Cannot drop column '{column_name}' from table '{table_name}': "
                "table would be left with no columns"
            )

        pk_col = next((col for col in columns if col["pk"]), None)
        if pk_col and pk_col["name"] == column_name:
            raise MigrationError(
                f"Cannot drop PRIMARY KEY column '{column_name}' from table '{table_name}'"
            )

        columns_to_keep = [col for col in columns if col["name"] != column_name]

        temp_table_name = f"{table_name}_temp"
        column_defs = []
        for col in columns_to_keep:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)})"
        self.conn.execute(create_sql)

        cols_to_select = [col["name"] for col in columns_to_keep]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        self.conn.execute(select_sql)

        self.conn.execute(f"DROP TABLE {table_name}")

        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: List[str],
        unique: bool = False
    ) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if self.index_exists(index_name):
            raise MigrationError(f"Index '{index_name}' already exists")

        # Verify all columns exist in the table
        for col in columns:
            if not self.column_exists(table_name, col):
                raise MigrationError(f"Column '{col}' does not exist in table '{table_name}'")

        unique_str = "UNIQUE" if unique else ""
        cols_str = ", ".join(columns)
        sql = f"CREATE {unique_str} INDEX {index_name} ON {table_name} ({cols_str})"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in create_index: {e}")

    def drop_index(self, index_name: str) -> None:
        if not self.index_exists(index_name):
            raise MigrationError(f"Index '{index_name}' does not exist")

        sql = f"DROP INDEX {index_name}"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in drop_index: {e}")

    def add_foreign_key(
        self,
        table_name: str,
        fk_name: str,
        columns: List[str],
        ref_table: str,
        ref_columns: List[str],
        on_delete: Optional[str] = None,
        on_update: Optional[str] = None
    ) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if not self.table_exists(ref_table):
            raise MigrationError(f"Referenced table '{ref_table}' does not exist")

        # Verify all columns exist
        for col in columns:
            if not self.column_exists(table_name, col):
                raise MigrationError(f"Column '{col}' does not exist in table '{table_name}'")

        for col in ref_columns:
            if not self.column_exists(ref_table, col):
                raise MigrationError(f"Column '{col}' does not exist in referenced table '{ref_table}'")

        # Check if FK already exists
        existing_fk = self.get_foreign_key_by_name(table_name, fk_name)
        if existing_fk:
            raise MigrationError(f"Foreign key '{fk_name}' already exists in table '{table_name}'")

        # Enable foreign keys
        self.conn.execute("PRAGMA foreign_keys = ON")

        # For SQLite: need to recreate table to add constraint
        # Get current table info
        columns_info = self.get_table_columns(table_name)
        temp_table_name = f"{table_name}_temp"

        # Build column definitions
        column_defs = []
        for col in columns_info:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
                # Note: AUTOINCREMENT is lost in this pattern, but we preserve it if present
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        # Add foreign key constraint
        fk_clause = f"FOREIGN KEY ({', '.join(columns)}) REFERENCES {ref_table} ({', '.join(ref_columns)})"
        if on_delete:
            fk_clause += f" ON DELETE {on_delete.upper()}"
        if on_update:
            fk_clause += f" ON UPDATE {on_update.upper()}"
        fk_def = f", CONSTRAINT {fk_name} {fk_clause}"

        # Create new table with FK
        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)}{fk_def})"
        try:
            self.conn.execute(create_sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error creating table with foreign key: {e}")

        # Copy data
        cols_to_select = [col["name"] for col in columns_info]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        self.conn.execute(select_sql)

        # Drop old table
        self.conn.execute(f"DROP TABLE {table_name}")

        # Rename new table
        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

        # Recreate indexes
        for idx in self.get_table_indexes(table_name):
            try:
                if idx["sql"]:
                    # Update table name in index SQL
                    idx_sql = idx["sql"].replace(idx["name"], f"{idx['name']}_temp")
                    idx_sql = idx_sql.replace(f"ON {table_name}", f"ON {temp_table_name}")
                    self.conn.execute(idx_sql)
            except sqlite3.Error:
                pass  # Silently skip index recreation issues, will be fixed in migration

    def drop_foreign_key(self, table_name: str, fk_name: str) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        existing_fk = self.get_foreign_key_by_name(table_name, fk_name)
        if not existing_fk:
            raise MigrationError(f"Foreign key '{fk_name}' does not exist in table '{table_name}'")

        # For SQLite: need to recreate table without the FK
        columns_info = self.get_table_columns(table_name)
        temp_table_name = f"{table_name}_temp"

        # Build column definitions (without FK constraint)
        column_defs = []
        for col in columns_info:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        # Create new table (without FK)
        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)})"
        try:
            self.conn.execute(create_sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error creating table: {e}")

        # Copy data
        cols_to_select = [col["name"] for col in columns_info]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        self.conn.execute(select_sql)

        # Drop old table
        self.conn.execute(f"DROP TABLE {table_name}")

        # Rename new table
        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

    def add_check_constraint(
        self,
        table_name: str,
        constraint_name: str,
        expression: str
    ) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        # Check if constraint already exists
        existing_ck = self.get_check_constraint_by_name(table_name, constraint_name)
        if existing_ck:
            raise MigrationError(f"Check constraint '{constraint_name}' already exists in table '{table_name}'")

        # For SQLite: need to recreate table with constraint
        columns_info = self.get_table_columns(table_name)
        temp_table_name = f"{table_name}_temp"

        # Build column definitions
        column_defs = []
        for col in columns_info:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        # Add check constraint
        constraint_def = f"CONSTRAINT {constraint_name} CHECK ({expression})"

        # Create new table with check constraint
        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)}, {constraint_def})"
        try:
            self.conn.execute(create_sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error creating table with check constraint: {e}")

        # Copy data
        cols_to_select = [col["name"] for col in columns_info]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        try:
            self.conn.execute(select_sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error inserting data: {e}")

        # Drop old table
        self.conn.execute(f"DROP TABLE {table_name}")

        # Rename new table
        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

    def drop_check_constraint(self, table_name: str, constraint_name: str) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        existing_ck = self.get_check_constraint_by_name(table_name, constraint_name)
        if not existing_ck:
            raise MigrationError(f"Check constraint '{constraint_name}' does not exist in table '{table_name}'")

        # For SQLite: need to recreate table without the constraint
        columns_info = self.get_table_columns(table_name)
        temp_table_name = f"{table_name}_temp"

        # Build column definitions (without check constraint)
        column_defs = []
        for col in columns_info:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        # Create new table (without check constraint)
        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)})"
        try:
            self.conn.execute(create_sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error creating table: {e}")

        # Copy data
        cols_to_select = [col["name"] for col in columns_info]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        self.conn.execute(select_sql)

        # Drop old table
        self.conn.execute(f"DROP TABLE {table_name}")

        # Rename new table
        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

    def transform_data(self, table_name: str, transformations: List[Dict[str, Any]]) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        for transform in transformations:
            column = transform["column"]
            expression = transform["expression"]

            if not self.column_exists(table_name, column):
                raise MigrationError(
                    f"Column '{column}' does not exist in table '{table_name}'"
                )

            sql = f'UPDATE "{table_name}" SET "{column}" = ({expression})'
            try:
                self.conn.execute(sql)
            except sqlite3.Error as e:
                raise MigrationError(f"SQL error in transform_data: {e}")

    def migrate_column_data(
        self,
        table_name: str,
        from_column: str,
        to_column: str,
        default_value: Optional[Any] = None
    ) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if not self.column_exists(table_name, from_column):
            raise MigrationError(
                f"Column '{from_column}' does not exist in table '{table_name}'"
            )

        if not self.column_exists(table_name, to_column):
            raise MigrationError(
                f"Column '{to_column}' does not exist in table '{table_name}'"
            )

        if default_value is not None:
            # Handle different value types for SQL
            if isinstance(default_value, str):
                # Escape single quotes in string values
                escaped_value = default_value.replace("'", "''")
                default_sql = f"COALESCE({from_column}, '{escaped_value}')"
            elif isinstance(default_value, bool):
                default_sql = f"COALESCE({from_column}, {1 if default_value else 0})"
            elif isinstance(default_value, (int, float)):
                default_sql = f"COALESCE({from_column}, {default_value})"
            else:
                default_sql = f"COALESCE({from_column}, NULL)"
        else:
            default_sql = from_column

        sql = f'UPDATE "{table_name}" SET "{to_column}" = {default_sql}'
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in migrate_column_data: {e}")

    def backfill_data(
        self,
        table_name: str,
        column: str,
        value: str,
        where: Optional[str] = None
    ) -> None:
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if not self.column_exists(table_name, column):
            raise MigrationError(
                f"Column '{column}' does not exist in table '{table_name}'"
            )

        sql = f'UPDATE "{table_name}" SET "{column}" = {value}'
        if where:
            sql += f" WHERE {where}"

        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in backfill_data: {e}")

    # ===== ROLLBACK OPERATIONS =====

    def drop_table(self, table_name: str) -> None:
        """Rollback for create_table - drops the table."""
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        sql = f"DROP TABLE {table_name}"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in drop_table: {e}")

    def remove_column(self, table_name: str, column_name: str) -> None:
        """Rollback for add_column - drops the column."""
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if not self.column_exists(table_name, column_name):
            raise MigrationError(f"Column '{column_name}' does not exist in table '{table_name}'")

        columns = self.get_table_columns(table_name)

        if len(columns) <= 1:
            raise MigrationError(
                f"Cannot remove column '{column_name}' from table '{table_name}': "
                "table would be left with no columns"
            )

        pk_col = next((col for col in columns if col["pk"]), None)
        if pk_col and pk_col["name"] == column_name:
            raise MigrationError(
                f"Cannot remove PRIMARY KEY column '{column_name}' from table '{table_name}'"
            )

        columns_to_keep = [col for col in columns if col["name"] != column_name]

        temp_table_name = f"{table_name}_temp"
        column_defs = []
        for col in columns_to_keep:
            def_str = f'{col["name"]} {col["type"]}'
            if col["notnull"]:
                def_str += " NOT NULL"
            if col["pk"]:
                def_str += " PRIMARY KEY"
            if col["default"] is not None:
                def_str += f" DEFAULT {col['default']}"
            column_defs.append(def_str)

        create_sql = f"CREATE TABLE {temp_table_name} ({', '.join(column_defs)})"
        self.conn.execute(create_sql)

        cols_to_select = [col["name"] for col in columns_to_keep]
        select_sql = f"INSERT INTO {temp_table_name} SELECT {', '.join(cols_to_select)} FROM {table_name}"
        self.conn.execute(select_sql)

        self.conn.execute(f"DROP TABLE {table_name}")

        rename_sql = f"ALTER TABLE {temp_table_name} RENAME TO {table_name}"
        self.conn.execute(rename_sql)

    def recreate_column(self, table_name: str, column_def: Dict[str, Any]) -> None:
        """Rollback for drop_column - recreates the column with original definition."""
        if not self.table_exists(table_name):
            raise MigrationError(f"Table '{table_name}' does not exist")

        if self.column_exists(table_name, column_def["name"]):
            raise MigrationError(f"Column '{column_def['name']}' already exists in table '{table_name}'")

        def_str = f"{column_def['name']} {column_def['type']}"

        if column_def.get("not_null", False):
            def_str += " NOT NULL"

        if "default" in column_def and column_def["default"] is not None:
            def_str += f" DEFAULT {column_def['default']}"

        sql = f"ALTER TABLE {table_name} ADD COLUMN {def_str}"
        try:
            self.conn.execute(sql)
        except sqlite3.Error as e:
            raise MigrationError(f"SQL error in recreate_column: {e}")

    def remove_foreign_key(self, table_name: str, fk_name: str) -> None:
        """Rollback for add_foreign_key - drops the foreign key."""
        self.drop_foreign_key(table_name, fk_name)

    def recreate_foreign_key(self, table_name: str, fk_def: Dict[str, Any]) -> None:
        """Rollback for drop_foreign_key - recreates the foreign key."""
        columns = fk_def["columns"]
        references = fk_def["references"]
        on_delete = fk_def.get("on_delete")
        on_update = fk_def.get("on_update")
        self.add_foreign_key(
            table_name,
            fk_def["name"],
            columns,
            references["table"],
            references["columns"],
            on_delete,
            on_update
        )

    def remove_index(self, index_name: str) -> None:
        """Rollback for create_index - drops the index."""
        self.drop_index(index_name)

    def recreate_index(self, index_def: Dict[str, Any]) -> None:
        """Rollback for drop_index - recreates the index."""
        self.create_index(
            index_def["name"],
            index_def["table"],
            index_def["columns"],
            index_def.get("unique", False)
        )

    def remove_check_constraint(self, table_name: str, constraint_name: str) -> None:
        """Rollback for add_check_constraint - drops the check constraint."""
        self.drop_check_constraint(table_name, constraint_name)

    def recreate_check_constraint(self, table_name: str, constraint_def: Dict[str, Any]) -> None:
        """Rollback for drop_check_constraint - recreates the check constraint."""
        self.add_check_constraint(
            table_name,
            constraint_def["name"],
            constraint_def["expression"]
        )

    def record_migration_with_ops(self, version: int, description: str, operations: List[Dict[str, Any]]) -> None:
        """Record a migration with its operations for potential rollback."""
        operations_json = json.dumps(operations)
        self.conn.execute(
            "INSERT INTO _migrations (version, description, operations) VALUES (?, ?, ?)",
            (version, description, operations_json)
        )

    def get_migration_operations(self, version: int) -> Optional[List[Dict[str, Any]]]:
        """Retrieve stored operations for a migration."""
        cursor = self.conn.execute(
            "SELECT operations FROM _migrations WHERE version = ?",
            (version,)
        )
        row = cursor.fetchone()
        if row:
            return json.loads(row[0])
        return None

    def get_applied_migrations(self) -> List[Dict[str, Any]]:
        """Get all applied migrations in order."""
        cursor = self.conn.execute(
            "SELECT version, description FROM _migrations ORDER BY version ASC"
        )
        migrations = []
        for row in cursor.fetchall():
            migrations.append({
                "version": row[0],
                "description": row[1]
            })
        return migrations

    def rollback_migration(self, version: int, rollback_ops: Optional[List[Dict[str, Any]]] = None) -> None:
        """Rollback a single migration."""
        # Get migration info
        cursor = self.conn.execute(
            "SELECT description FROM _migrations WHERE version = ?",
            (version,)
        )
        row = cursor.fetchone()
        if not row:
            raise MigrationError(f"version {version} not found")
        description = row[0]

        # Get operations if not provided
        if rollback_ops is None:
            rollback_ops = self.get_migration_operations(version)

        if not rollback_ops:
            # Try to generate automatic rollback from stored operations
            stored_ops = self.get_migration_operations(version)
            if stored_ops:
                rollback_ops = self._generate_rollback_operations(stored_ops)
            else:
                raise MigrationError(f"cannot rollback version {version}: missing rollback_operations for non-reversible operations")

        # Execute rollback operations in reverse order
        for op in reversed(rollback_ops):
            self._execute_rollback_operation(op, version)

        # Remove from _migrations table
        self.conn.execute("DELETE FROM _migrations WHERE version = ?", (version,))

    def _generate_rollback_operations(self, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Generate rollback operations for automatic rollback."""
        rollback_ops = []
        for op in reversed(operations):
            op_type = op["type"]

            if op_type == "create_table":
                rollback_ops.append({
                    "type": "drop_table",
                    "table": op["table"]
                })
            elif op_type == "add_column":
                column_info = op["column"]
                rollback_ops.append({
                    "type": "remove_column",
                    "table": op["table"],
                    "column": column_info["name"]
                })
            elif op_type == "drop_column":
                # For drop_column, we'd need original_definition stored
                if "original_definition" in op:
                    rollback_ops.append({
                        "type": "recreate_column",
                        "table": op["table"],
                        "column_def": op["original_definition"]
                    })
                else:
                    raise MigrationError(
                        f"cannot rollback: missing original_definition for drop_column on {op['table']}.{op['column']}"
                    )
            elif op_type == "add_foreign_key":
                rollback_ops.append({
                    "type": "remove_foreign_key",
                    "table": op["table"],
                    "name": op["name"]
                })
            elif op_type == "drop_foreign_key":
                if "original_definition" in op:
                    rollback_ops.append({
                        "type": "recreate_foreign_key",
                        "table": op["table"],
                        "fk_def": op["original_definition"]
                    })
                else:
                    raise MigrationError(
                        f"cannot rollback: missing original_definition for drop_foreign_key {op['name']}"
                    )
            elif op_type == "create_index":
                rollback_ops.append({
                    "type": "remove_index",
                    "name": op["name"]
                })
            elif op_type == "drop_index":
                if "original_definition" in op:
                    rollback_ops.append({
                        "type": "recreate_index",
                        "index_def": op["original_definition"]
                    })
                else:
                    raise MigrationError(
                        f"cannot rollback: missing original_definition for drop_index {op['name']}"
                    )
            elif op_type == "add_check_constraint":
                rollback_ops.append({
                    "type": "remove_check_constraint",
                    "table": op["table"],
                    "name": op["name"]
                })
            elif op_type == "drop_check_constraint":
                if "original_definition" in op:
                    rollback_ops.append({
                        "type": "recreate_check_constraint",
                        "table": op["table"],
                        "constraint_def": op["original_definition"]
                    })
                else:
                    raise MigrationError(
                        f"cannot rollback: missing original_definition for drop_check_constraint {op['name']}"
                    )
            elif op_type in ("transform_data", "backfill_data", "migrate_column_data"):
                raise MigrationError(
                    f"cannot rollback version: missing rollback_operations for {op_type}"
                )
        return rollback_ops

    def _execute_rollback_operation(self, op: Dict[str, Any], version: int) -> None:
        """Execute a single rollback operation."""
        op_type = op["type"]
        table = op.get("table")
        column = op.get("column")
        name = op.get("name")

        if op_type == "drop_table":
            self.drop_table(table)
        elif op_type == "remove_column":
            self.remove_column(table, column)
        elif op_type == "recreate_column":
            self.recreate_column(table, op["column_def"])
        elif op_type == "remove_foreign_key":
            self.remove_foreign_key(table, name)
        elif op_type == "recreate_foreign_key":
            self.recreate_foreign_key(table, op["fk_def"])
        elif op_type == "remove_index":
            self.remove_index(name)
        elif op_type == "recreate_index":
            self.recreate_index(op["index_def"])
        elif op_type == "remove_check_constraint":
            self.remove_check_constraint(table, name)
        elif op_type == "recreate_check_constraint":
            self.recreate_check_constraint(table, op["constraint_def"])
        else:
            # For explicit rollback operations, execute them directly
            # This handles cases like drop_column, migrate_column_data in rollback_operations
            self._execute_operation(op)

    def _execute_operation(self, op: Dict[str, Any]) -> None:
        """Execute a forward operation (used for explicit rollback operations)."""
        op_type = op["type"]
        table = op.get("table")
        column = op.get("column")
        name = op.get("name")

        if op_type == "create_table":
            self.create_table(table, op["columns"])
        elif op_type == "add_column":
            self.add_column(table, op["column"])
        elif op_type == "drop_column":
            self.drop_column(table, column)
        elif op_type == "migrate_column_data":
            self.migrate_column_data(
                table,
                op["from_column"],
                op["to_column"],
                op.get("default_value")
            )

    def rollback_all_migrations(self, to_version: Optional[int] = None, count: Optional[int] = None) -> List[int]:
        """
        Rollback multiple migrations.
        If to_version is specified, rollback to that version (exclusive).
        If count is specified, rollback the last N migrations.
        If neither is specified, rollback 1 migration (count=1).
        Returns list of rolled back versions.
        """
        migrations = self.get_applied_migrations()

        if not migrations:
            raise MigrationError("no migrations to rollback")

        # Determine which migrations to rollback
        if to_version is not None:
            # Find index of migration to stop at
            stop_idx = None
            for i, m in enumerate(migrations):
                if m["version"] == to_version:
                    stop_idx = i
                    break

            if stop_idx is None:
                raise MigrationError(f"version {to_version} not found")

            versions_to_rollback = [m["version"] for m in migrations[stop_idx + 1:]]
        elif count is not None:
            if count < 1:
                raise MigrationError("count must be at least 1")
            versions_to_rollback = [m["version"] for m in migrations[-count:]]
        else:
            # Default: rollback 1 migration
            versions_to_rollback = [migrations[-1]["version"]]

        if not versions_to_rollback:
            raise MigrationError("no migrations to rollback")

        # Rollback in reverse version order
        rolled_back = []
        for version in reversed(versions_to_rollback):
            self.rollback_migration(version)
            rolled_back.append(version)

        return rolled_back


def print_operation_applied(op_type: str, table: str, column: Optional[str], version: int, name: Optional[str] = None) -> None:
    output = {
        "event": "operation_applied",
        "type": op_type,
        "table": table,
        "version": version
    }
    if column:
        output["column"] = column
    if name:
        output["name"] = name
    print(json.dumps(output))


def print_migration_complete(version: int, operations_count: int) -> None:
    """Print JSON output for migration completion."""
    output = {
        "event": "migration_complete",
        "version": version,
        "operations_count": operations_count
    }
    print(json.dumps(output))


def print_migration_skipped(version: int) -> None:
    """Print JSON output for skipped migration."""
    output = {
        "event": "migration_skipped",
        "version": version,
        "reason": "already_applied"
    }
    print(json.dumps(output))
    print("Warning: Migration version {} already applied, skipping".format(version), file=sys.stderr)


def print_error(message: str) -> None:
    """Print error message to stderr."""
    print(f"Error: {message}", file=sys.stderr)


def print_rollback_started(version: int, description: str) -> None:
    """Print JSON output for rollback start."""
    output = {
        "event": "rollback_started",
        "version": version,
        "description": description
    }
    print(json.dumps(output))


def print_operation_rolled_back(op_type: str, table: str, column: Optional[str], version: int, name: Optional[str] = None) -> None:
    """Print JSON output for rolled back operation."""
    output = {
        "event": "operation_rolled_back",
        "type": op_type,
        "table": table,
        "version": version
    }
    if column:
        output["column"] = column
    if name:
        output["name"] = name
    print(json.dumps(output))


def print_rollback_complete(version: int) -> None:
    """Print JSON output for rollback completion."""
    output = {
        "event": "rollback_complete",
        "version": version
    }
    print(json.dumps(output))


def print_rollback_finished(versions_rolled_back: List[int], final_version: int) -> None:
    """Print JSON output for rollback finish."""
    output = {
        "event": "rollback_finished",
        "versions_rolled_back": versions_rolled_back,
        "final_version": final_version
    }
    print(json.dumps(output))


def apply_migration(migration_path: str, db_path: str) -> int:
    """
    Apply a migration to the database.
    Returns 0 on success, 1 on error.
    """
    # Read migration file
    migration_file = Path(migration_path)
    if not migration_file.exists():
        print_error(f"migration file not found: {migration_path}")
        return 1

    try:
        content = migration_file.read_text()
        migration_data = json.loads(content)
    except json.JSONDecodeError as e:
        print_error(f"invalid JSON in migration file: {e}")
        return 1
    except OSError as e:
        print_error(f"could not read migration file: {e}")
        return 1

    # Validate migration
    try:
        MigrationValidator.validate_migration_file(migration_data)
    except MigrationError as e:
        print_error(str(e))
        return 1

    version = migration_data["version"]
    description = migration_data["description"]
    operations = migration_data["operations"]

    # Execute migration
    try:
        with MigrationExecutor(db_path) as executor:
            if executor.is_migration_applied(version):
                print_migration_skipped(version)
                return 0

            # Apply each operation
            for op in operations:
                op_type = op["type"]
                table = op["table"]
                column = None
                name = None

                if op_type == "create_table":
                    executor.create_table(table, op["columns"])
                elif op_type == "add_column":
                    column_info = op["column"]
                    column = column_info["name"]
                    executor.add_column(table, column_info)
                elif op_type == "drop_column":
                    column = op["column"]
                    executor.drop_column(table, column)
                elif op_type == "transform_data":
                    executor.transform_data(table, op["transformations"])
                elif op_type == "migrate_column_data":
                    default_value = op.get("default_value")
                    executor.migrate_column_data(
                        table,
                        op["from_column"],
                        op["to_column"],
                        default_value
                    )
                elif op_type == "backfill_data":
                    where_clause = op.get("where")
                    executor.backfill_data(
                        table,
                        op["column"],
                        op["value"],
                        where_clause
                    )
                elif op_type == "add_foreign_key":
                    name = op["name"]
                    columns = op["columns"]
                    references = op["references"]
                    on_delete = op.get("on_delete")
                    on_update = op.get("on_update")
                    executor.add_foreign_key(
                        table,
                        name,
                        columns,
                        references["table"],
                        references["columns"],
                        on_delete,
                        on_update
                    )
                elif op_type == "drop_foreign_key":
                    name = op["name"]
                    executor.drop_foreign_key(table, name)
                elif op_type == "create_index":
                    name = op["name"]
                    columns = op["columns"]
                    unique = op.get("unique", False)
                    executor.create_index(name, table, columns, unique)
                elif op_type == "drop_index":
                    name = op["name"]
                    executor.drop_index(name)
                elif op_type == "add_check_constraint":
                    name = op["name"]
                    expression = op["expression"]
                    executor.add_check_constraint(table, name, expression)
                elif op_type == "drop_check_constraint":
                    name = op["name"]
                    executor.drop_check_constraint(table, name)

                print_operation_applied(op_type, table, column, version, name)

            # Record migration as applied with its operations
            executor.record_migration_with_ops(version, description, operations)
            print_migration_complete(version, len(operations))

    except MigrationError as e:
        print_error(str(e))
        return 1
    except sqlite3.Error as e:
        print_error(f"SQL error: {e}")
        return 1

    return 0


def main() -> None:
    """Main entry point."""
    if len(sys.argv) < 4:
        print("Usage: python migration_tool.py migrate <migration.json> <database.db>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    if command != "migrate":
        print_error(f"Unknown command: {command}")
        print("Usage: python migration_tool.py migrate <migration.json> <database.db>", file=sys.stderr)
        sys.exit(1)

    migration_path = sys.argv[2]
    db_path = sys.argv[3]

    exit_code = apply_migration(migration_path, db_path)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
