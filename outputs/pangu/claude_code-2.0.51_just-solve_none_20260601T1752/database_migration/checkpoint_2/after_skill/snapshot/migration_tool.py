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

    @classmethod
    def validate_operation(cls, op: Dict[str, Any], index: int) -> None:
        if not isinstance(op, dict):
            raise MigrationError(f"Operation {index} must be a JSON object")

        if "type" not in op:
            raise MigrationError(f"Operation {index}: Missing required field 'type'")

        op_type = op["type"]
        if op_type not in {"create_table", "add_column", "drop_column", "transform_data", "migrate_column_data", "backfill_data"}:
            raise MigrationError(
                f"Operation {index}: Invalid operation type '{op_type}'. "
                f"Must be 'create_table', 'add_column', 'drop_column', 'transform_data', 'migrate_column_data', or 'backfill_data'"
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
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


def print_operation_applied(op_type: str, table: str, column: Optional[str], version: int) -> None:
    output = {
        "event": "operation_applied",
        "type": op_type,
        "table": table,
        "version": version
    }
    if column:
        output["column"] = column
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

                print_operation_applied(op_type, table, column, version)

            # Record migration as applied
            executor.record_migration(version, description)
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
