#!/usr/bin/env python3
"""Database Migration Tool - Applies schema migrations to SQLite databases."""

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List

VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}
IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def error_exit(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def warning(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def output_json(event: Dict[str, Any]) -> None:
    print(json.dumps(event, separators=(',', ':')))


def require_string(obj: Dict[str, Any], field: str) -> str:
    if field not in obj:
        error_exit(f"invalid migration schema: missing required field '{field}'")
    if not isinstance(obj[field], str):
        error_exit(f"invalid migration schema: '{field}' must be a string")
    return obj[field]


def require_list(obj: Dict[str, Any], field: str) -> List[Any]:
    if field not in obj:
        error_exit(f"invalid migration schema: missing required field '{field}'")
    if not isinstance(obj[field], list):
        error_exit(f"invalid migration schema: '{field}' must be an array")
    return obj[field]


def require_dict(obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    if field not in obj:
        error_exit(f"invalid migration schema: missing required field '{field}'")
    if not isinstance(obj[field], dict):
        error_exit(f"invalid migration schema: '{field}' must be an object")
    return obj[field]


def validate_identifier(name: str, kind: str) -> str:
    if not IDENTIFIER_PATTERN.match(name):
        error_exit(f"invalid {kind} name: '{name}'")
    return name


def quote_identifier(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) + chr(34))}"'


def validate_column(column: Dict[str, Any]) -> None:
    name = require_string(column, "name")
    col_type = require_string(column, "type")
    validate_identifier(name, "column")

    if col_type.upper() not in VALID_TYPES:
        error_exit(f"invalid column type: '{col_type}'")

    for bool_field in ("primary_key", "auto_increment", "not_null", "unique"):
        if bool_field in column and not isinstance(column[bool_field], bool):
            error_exit(f"invalid migration schema: '{bool_field}' must be a boolean")

    if "default" in column and column["default"] is not None and not isinstance(column["default"], str):
        error_exit("invalid migration schema: 'default' must be a string or null")

    if column.get("auto_increment"):
        if col_type.upper() != "INTEGER":
            error_exit("invalid column definition: auto_increment can only be used with INTEGER type")
        if not column.get("primary_key"):
            error_exit("invalid column definition: auto_increment requires primary_key to be true")


def validate_table_ref(operation: Dict[str, Any]) -> str:
    table = require_string(operation, "table")
    return validate_identifier(table, "table")


def validate_create_table(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    columns = require_list(operation, "columns")
    if not columns:
        error_exit("invalid migration schema: 'columns' array cannot be empty")

    pk_count = 0
    for col in columns:
        validate_column(col)
        if col.get("primary_key"):
            pk_count += 1

    if pk_count > 1:
        error_exit("invalid table definition: at most one column can have primary_key: true")


def validate_add_column(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    column = require_dict(operation, "column")
    validate_column(column)


def validate_drop_column(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    column = require_string(operation, "column")
    validate_identifier(column, "column")


VALIDATORS = {
    "create_table": validate_create_table,
    "add_column": validate_add_column,
    "drop_column": validate_drop_column,
}


def validate_operation(operation: Dict[str, Any]) -> None:
    op_type = require_string(operation, "type")
    validator = VALIDATORS.get(op_type)
    if not validator:
        error_exit(f"invalid operation type: '{op_type}'")
    validator(operation)


def validate_migration(migration: Dict[str, Any]) -> None:
    version = migration.get("version")
    if version is None:
        error_exit("invalid migration schema: missing required field 'version'")
    if not isinstance(version, int):
        error_exit("invalid migration schema: 'version' must be an integer")
    if version <= 0:
        error_exit("invalid migration schema: 'version' must be a positive integer")

    description = migration.get("description")
    if description is None:
        error_exit("invalid migration schema: missing required field 'description'")
    if not isinstance(description, str):
        error_exit("invalid migration schema: 'description' must be a string")

    operations = migration.get("operations")
    if operations is None:
        error_exit("invalid migration schema: missing required field 'operations'")
    if not isinstance(operations, list):
        error_exit("invalid migration schema: 'operations' must be an array")
    if not operations:
        error_exit("invalid migration schema: 'operations' array cannot be empty")

    for op in operations:
        validate_operation(op)


def build_column_sql(column: Dict[str, Any]) -> str:
    parts = [quote_identifier(column["name"]), column["type"].upper()]

    if column.get("primary_key"):
        parts.append("PRIMARY KEY")
    if column.get("auto_increment"):
        parts.append("AUTOINCREMENT")
    if column.get("not_null"):
        parts.append("NOT NULL")
    if column.get("unique"):
        parts.append("UNIQUE")
    if column.get("default") is not None:
        parts.append(f"DEFAULT {column['default']}")

    return " ".join(parts)


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return [row[1] for row in cursor.fetchall()]


def get_column_pk_flag(cursor: sqlite3.Cursor, table_name: str) -> Dict[str, bool]:
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return {row[1]: row[5] > 0 for row in cursor.fetchall()}


def execute_create_table(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]

    if table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' already exists")

    columns_sql = ", ".join(build_column_sql(col) for col in operation["columns"])
    sql = f"CREATE TABLE {quote_identifier(table_name)} ({columns_sql})"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "create_table",
        "table": table_name,
        "version": version
    })


def execute_add_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column = operation["column"]
    column_name = column["name"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    if column_name in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' already exists in table '{table_name}'")

    column_sql = build_column_sql(column)
    sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {column_sql}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "add_column",
        "table": table_name,
        "column": column_name,
        "version": version
    })


def execute_drop_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column_name = operation["column"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    if column_name not in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

    if len(existing_columns) == 1:
        error_exit(f"invalid operation: cannot drop the only column in table '{table_name}'")

    pk_flags = get_column_pk_flag(cursor, table_name)
    if pk_flags.get(column_name):
        error_exit(f"invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    columns_to_keep = [col for col in existing_columns if col != column_name]

    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    col_info_map = {row[1]: row for row in cursor.fetchall()}

    new_columns_sql_parts = []
    for col_name in columns_to_keep:
        row = col_info_map[col_name]
        col_def = f"{quote_identifier(row[1])} {row[2]}"
        if row[5] > 0:
            col_def += " PRIMARY KEY"
        if row[3]:
            col_def += " NOT NULL"
        if row[4] is not None:
            col_def += f" DEFAULT {row[4]}"
        new_columns_sql_parts.append(col_def)

    new_table_name = f"_migration_temp_{table_name}"
    new_columns_sql = ", ".join(new_columns_sql_parts)

    try:
        cursor.execute("PRAGMA foreign_keys = OFF")

        cursor.execute(f"CREATE TABLE {quote_identifier(new_table_name)} ({new_columns_sql})")

        columns_select = ", ".join(quote_identifier(col) for col in columns_to_keep)
        cursor.execute(f"INSERT INTO {quote_identifier(new_table_name)} SELECT {columns_select} FROM {quote_identifier(table_name)}")

        cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")

        cursor.execute(f"ALTER TABLE {quote_identifier(new_table_name)} RENAME TO {quote_identifier(table_name)}")

        cursor.execute("PRAGMA foreign_keys = ON")

    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "drop_column",
        "table": table_name,
        "column": column_name,
        "version": version
    })


EXECUTORS = {
    "create_table": execute_create_table,
    "add_column": execute_add_column,
    "drop_column": execute_drop_column,
}


def execute_operation(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    op_type = operation["type"]
    EXECUTORS[op_type](cursor, operation, version)


def init_migrations_table(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str) -> None:
    cursor.execute(
        "INSERT INTO _migrations (version, description) VALUES (?, ?)",
        (version, description)
    )


def run_migration(migration_file: str, db_file: str) -> None:
    if not os.path.exists(migration_file):
        error_exit(f"migration file not found: {migration_file}")

    try:
        with open(migration_file, 'r') as f:
            migration = json.load(f)
    except json.JSONDecodeError as e:
        error_exit(f"invalid JSON in migration file: {e}")
    except IOError as e:
        error_exit(f"error reading migration file: {e}")

    validate_migration(migration)

    version = migration["version"]
    description = migration["description"]
    operations = migration["operations"]

    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        error_exit(f"error connecting to database: {e}")

    try:
        init_migrations_table(cursor)

        if is_migration_applied(cursor, version):
            warning(f"Migration version {version} already applied, skipping")
            output_json({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            })
            conn.close()
            return

        cursor.execute("PRAGMA foreign_keys = OFF")

        for operation in operations:
            execute_operation(cursor, operation, version)

        record_migration(cursor, version, description)

        cursor.execute("PRAGMA foreign_keys = ON")

        conn.commit()

        output_json({
            "event": "migration_complete",
            "version": version,
            "operations_count": len(operations)
        })

    except sqlite3.Error as e:
        conn.rollback()
        error_exit(f"SQL error: {e}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Database Migration Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    migrate_parser = subparsers.add_parser("migrate", help="Apply a migration")
    migrate_parser.add_argument("migration_file", help="Path to the migration JSON file")
    migrate_parser.add_argument("database", help="Path to the SQLite database file")

    args = parser.parse_args()

    if args.command == "migrate":
        run_migration(args.migration_file, args.database)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
