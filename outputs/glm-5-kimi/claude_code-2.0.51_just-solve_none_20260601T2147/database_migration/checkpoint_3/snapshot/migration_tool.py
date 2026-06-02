#!/usr/bin/env python3
"""Database Migration Tool - Applies schema migrations to SQLite databases."""

import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}
IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
VALID_FK_ACTIONS = {"CASCADE", "RESTRICT", "SET NULL", "NO ACTION", "SET DEFAULT"}


def error_exit(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def warning(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def output_json(event: Dict[str, Any]) -> None:
    print(json.dumps(event, separators=(',', ':')))


def _require_field(obj: Dict[str, Any], field: str, expected_type: type, type_name: str) -> Any:
    if field not in obj:
        error_exit(f"invalid migration schema: missing required field '{field}'")
    if not isinstance(obj[field], expected_type):
        error_exit(f"invalid migration schema: '{field}' must be {type_name}")
    return obj[field]


def require_string(obj: Dict[str, Any], field: str) -> str:
    return _require_field(obj, field, str, "a string")


def require_int(obj: Dict[str, Any], field: int) -> int:
    return _require_field(obj, field, int, "an integer")


def require_list(obj: Dict[str, Any], field: str) -> List[Any]:
    return _require_field(obj, field, list, "an array")


def require_dict(obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    return _require_field(obj, field, dict, "an object")


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


def validate_transform_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    transformations = require_list(operation, "transformations")
    if not transformations:
        error_exit("invalid migration schema: 'transformations' array cannot be empty")

    for transform in transformations:
        if not isinstance(transform, dict):
            error_exit("invalid migration schema: each transformation must be an object")
        column = require_string(transform, "column")
        validate_identifier(column, "column")
        require_string(transform, "expression")


def validate_migrate_column_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    from_column = require_string(operation, "from_column")
    validate_identifier(from_column, "column")
    to_column = require_string(operation, "to_column")
    validate_identifier(to_column, "column")
    if "default_value" in operation and operation["default_value"] is not None:
        if not isinstance(operation["default_value"], str):
            error_exit("invalid migration schema: 'default_value' must be a string or null")


def validate_backfill_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    column = require_string(operation, "column")
    validate_identifier(column, "column")
    require_string(operation, "value")
    if "where" in operation:
        where_clause = operation["where"]
        if not isinstance(where_clause, str):
            error_exit("invalid migration schema: 'where' must be a string")


def validate_add_foreign_key(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_string(operation, "name")
    validate_identifier(name, "foreign key")

    columns = require_list(operation, "columns")
    if not columns:
        error_exit("invalid migration schema: 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            error_exit("invalid migration schema: each column must be a string")
        validate_identifier(col, "column")

    references = require_dict(operation, "references")
    ref_table = require_string(references, "table")
    validate_identifier(ref_table, "table")

    ref_columns = require_list(references, "columns")
    if not ref_columns:
        error_exit("invalid migration schema: 'references.columns' array cannot be empty")
    for col in ref_columns:
        if not isinstance(col, str):
            error_exit("invalid migration schema: each reference column must be a string")
        validate_identifier(col, "column")

    if len(columns) != len(ref_columns):
        error_exit("invalid foreign key: number of columns must match number of referenced columns")

    for field in ("on_delete", "on_update"):
        if field in operation:
            action = operation[field]
            if not isinstance(action, str):
                error_exit(f"invalid migration schema: '{field}' must be a string")
            if action.upper() not in VALID_FK_ACTIONS:
                error_exit(f"invalid foreign key action: '{action}'")


def validate_drop_foreign_key(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_string(operation, "name")
    validate_identifier(name, "foreign key")


def validate_create_index(operation: Dict[str, Any]) -> None:
    name = require_string(operation, "name")
    validate_identifier(name, "index")
    validate_table_ref(operation)

    columns = require_list(operation, "columns")
    if not columns:
        error_exit("invalid migration schema: 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            error_exit("invalid migration schema: each column must be a string")
        validate_identifier(col, "column")

    if "unique" in operation and not isinstance(operation["unique"], bool):
        error_exit("invalid migration schema: 'unique' must be a boolean")


def validate_drop_index(operation: Dict[str, Any]) -> None:
    name = require_string(operation, "name")
    validate_identifier(name, "index")


def validate_add_check_constraint(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_string(operation, "name")
    validate_identifier(name, "check constraint")
    require_string(operation, "expression")


def validate_drop_check_constraint(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_string(operation, "name")
    validate_identifier(name, "check constraint")


VALIDATORS = {
    "create_table": validate_create_table,
    "add_column": validate_add_column,
    "drop_column": validate_drop_column,
    "transform_data": validate_transform_data,
    "migrate_column_data": validate_migrate_column_data,
    "backfill_data": validate_backfill_data,
    "add_foreign_key": validate_add_foreign_key,
    "drop_foreign_key": validate_drop_foreign_key,
    "create_index": validate_create_index,
    "drop_index": validate_drop_index,
    "add_check_constraint": validate_add_check_constraint,
    "drop_check_constraint": validate_drop_check_constraint,
}


def validate_operation(operation: Dict[str, Any]) -> None:
    op_type = require_string(operation, "type")
    validator = VALIDATORS.get(op_type)
    if not validator:
        error_exit(f"invalid operation type: '{op_type}'")
    validator(operation)


def validate_migration(migration: Dict[str, Any]) -> None:
    version = require_int(migration, "version")
    if version <= 0:
        error_exit("invalid migration schema: 'version' must be a positive integer")

    require_string(migration, "description")

    operations = require_list(migration, "operations")
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


def get_column_names(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return [row[1] for row in cursor.fetchall()]


def get_table_info(cursor: sqlite3.Cursor, table_name: str) -> List[Tuple]:
    """Get full table info from PRAGMA table_info."""
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return cursor.fetchall()


def get_foreign_keys(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    """Get foreign keys for a table from PRAGMA foreign_key_list."""
    cursor.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})")
    fks = []
    for row in cursor.fetchall():
        # id, seq, table, from, to, on_update, on_delete, match
        fks.append({
            "id": row[0],
            "seq": row[1],
            "references_table": row[2],
            "from_column": row[3],
            "to_column": row[4],
            "on_update": row[5],
            "on_delete": row[6],
        })
    return fks


def get_index_info(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    """Get indexes for a table from sqlite_master."""
    cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table_name,)
    )
    indexes = []
    for row in cursor.fetchall():
        indexes.append({
            "name": row[0],
            "sql": row[1],
        })
    return indexes


def get_check_constraints(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    """Parse check constraints from table SQL."""
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return []

    sql = row[0]
    constraints = []

    # Find CHECK constraints in the CREATE TABLE statement
    # Pattern: CONSTRAINT ["']?name["']? CHECK (expression) or CHECK (expression)
    # Handles quoted identifiers like CONSTRAINT "name" CHECK
    pattern = r'(?:CONSTRAINT\s+["\']?(\w+)["\']?\s+)?CHECK\s*\(([^)]+(?:\([^)]*\)[^)]*)*)\)'
    for match in re.finditer(pattern, sql, re.IGNORECASE):
        name = match.group(1)
        expr = match.group(2).strip()
        constraints.append({
            "name": name,
            "expression": expr,
        })

    return constraints


def get_table_sql(cursor: sqlite3.Cursor, table_name: str) -> Optional[str]:
    """Get the CREATE TABLE SQL for a table."""
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    """Check if an index exists."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    return cursor.fetchone() is not None


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check if a table exists."""
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def foreign_key_exists(cursor: sqlite3.Cursor, table_name: str, fk_name: str) -> bool:
    """Check if a foreign key with given name exists in table.

    Note: SQLite doesn't store constraint names directly, so we need to check
    via PRAGMA foreign_key_list and match by parsing original SQL.
    """
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return False

    # Look for CONSTRAINT fk_name FOREIGN KEY pattern (with optional quotes)
    # Handle both: CONSTRAINT fk_name FOREIGN KEY and CONSTRAINT "fk_name" FOREIGN KEY
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(fk_name)}["\']?\s+FOREIGN\s+KEY'
    return bool(re.search(pattern, sql, re.IGNORECASE))


def check_constraint_exists(cursor: sqlite3.Cursor, table_name: str, constraint_name: str) -> bool:
    """Check if a check constraint with given name exists."""
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return False

    # Look for CONSTRAINT constraint_name CHECK pattern (with optional quotes)
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(constraint_name)}["\']?\s+CHECK'
    return bool(re.search(pattern, sql, re.IGNORECASE))


def execute_create_table(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]

    if get_column_names(cursor, table_name):
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

    existing_columns = get_column_names(cursor, table_name)
    if not existing_columns:
        error_exit(f"invalid operation: table '{table_name}' does not exist")
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


def rebuild_table_with_modification(
    cursor: sqlite3.Cursor,
    table_name: str,
    new_table_sql: str,
    columns_to_copy: List[str]
) -> None:
    """Rebuild a table with a new schema, copying data.

    This is needed for operations that SQLite doesn't support directly via ALTER TABLE.
    Also recreates any indexes that reference the table.
    """
    temp_table_name = f"_migration_temp_{table_name}"

    # Save indexes before dropping the table
    index_info = get_index_info(cursor, table_name)

    try:
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Create new table with modified schema
        cursor.execute(new_table_sql)

        # Copy data
        columns_select = ", ".join(quote_identifier(col) for col in columns_to_copy)
        cursor.execute(
            f"INSERT INTO {quote_identifier(temp_table_name)} "
            f"SELECT {columns_select} FROM {quote_identifier(table_name)}"
        )

        # Drop old table (this also drops its indexes)
        cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        cursor.execute(
            f"ALTER TABLE {quote_identifier(temp_table_name)} "
            f"RENAME TO {quote_identifier(table_name)}"
        )

        # Recreate indexes
        for idx in index_info:
            # Adjust the index SQL to reference the actual table name
            # The index SQL references the original table name, which is now correct
            try:
                cursor.execute(idx["sql"])
            except sqlite3.Error:
                pass  # Skip indexes that can't be recreated

    finally:
        cursor.execute("PRAGMA foreign_keys = ON")


def generate_table_sql_with_foreign_key(
    cursor: sqlite3.Cursor,
    table_name: str,
    fk_name: str,
    fk_columns: List[str],
    ref_table: str,
    ref_columns: List[str],
    on_delete: Optional[str],
    on_update: Optional[str]
) -> str:
    """Generate CREATE TABLE SQL with an additional foreign key."""
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)

    # Build column definitions
    col_defs = []
    pk_columns = []

    for row in table_info:
        # cid, name, type, notnull, dflt_value, pk
        col_name = row[1]
        col_type = row[2]
        not_null = row[3]
        default = row[4]
        pk = row[5]

        parts = [quote_identifier(col_name), col_type]

        if pk > 0:
            pk_columns.append((pk, col_name))

        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")

        col_defs.append(" ".join(parts))

    # Add primary key constraint if composite or explicit
    if pk_columns:
        pk_columns_sorted = sorted(pk_columns, key=lambda x: x[0])
        if len(pk_columns_sorted) == 1:
            # Single PK is already handled in column definition
            for i, col_def in enumerate(col_defs):
                if quote_identifier(pk_columns_sorted[0][1]) in col_def:
                    col_defs[i] = col_def + " PRIMARY KEY"
                    break
        else:
            pk_col_names = [quote_identifier(c[1]) for c in pk_columns_sorted]
            col_defs.append(f"PRIMARY KEY ({', '.join(pk_col_names)})")

    # Add existing foreign keys
    fk_groups = {}
    for fk in existing_fks:
        fk_id = fk["id"]
        if fk_id not in fk_groups:
            fk_groups[fk_id] = {
                "columns": [],
                "ref_table": fk["references_table"],
                "ref_columns": [],
                "on_delete": fk["on_delete"],
                "on_update": fk["on_update"],
            }
        fk_groups[fk_id]["columns"].append(fk["from_column"])
        fk_groups[fk_id]["ref_columns"].append(fk["to_column"])

    for fk_id, fk_data in fk_groups.items():
        fk_def = (
            f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) "
            f"REFERENCES {quote_identifier(fk_data['ref_table'])} "
            f"({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        )
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        col_defs.append(fk_def)

    # Add new foreign key
    fk_def = (
        f"CONSTRAINT {quote_identifier(fk_name)} "
        f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_columns)}) "
        f"REFERENCES {quote_identifier(ref_table)} "
        f"({', '.join(quote_identifier(c) for c in ref_columns)})"
    )
    if on_delete:
        fk_def += f" ON DELETE {on_delete.upper()}"
    if on_update:
        fk_def += f" ON UPDATE {on_update.upper()}"
    col_defs.append(fk_def)

    # Add check constraints
    for chk in check_constraints:
        if chk["name"]:
            col_defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            col_defs.append(f"CHECK ({chk['expression']})")

    sql = f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
    return sql


def generate_table_sql_without_foreign_key(
    cursor: sqlite3.Cursor,
    table_name: str,
    fk_name_to_remove: str
) -> str:
    """Generate CREATE TABLE SQL without a specific foreign key."""
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)
    original_sql = get_table_sql(cursor, table_name)

    # Build column definitions
    col_defs = []
    pk_columns = []

    for row in table_info:
        col_name = row[1]
        col_type = row[2]
        not_null = row[3]
        default = row[4]
        pk = row[5]

        parts = [quote_identifier(col_name), col_type]

        if pk > 0:
            pk_columns.append((pk, col_name))

        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")

        col_defs.append(" ".join(parts))

    # Add primary key constraint
    if pk_columns:
        pk_columns_sorted = sorted(pk_columns, key=lambda x: x[0])
        if len(pk_columns_sorted) == 1:
            for i, col_def in enumerate(col_defs):
                if quote_identifier(pk_columns_sorted[0][1]) in col_def:
                    col_defs[i] = col_def + " PRIMARY KEY"
                    break
        else:
            pk_col_names = [quote_identifier(c[1]) for c in pk_columns_sorted]
            col_defs.append(f"PRIMARY KEY ({', '.join(pk_col_names)})")

    # Add existing foreign keys except the one to remove
    fk_groups = {}

    # Get FK id mapping from original SQL
    # Parse CONSTRAINT name FOREIGN KEY to match names to ids
    fk_name_to_id = {}
    fk_pattern = r'CONSTRAINT\s+["\']?(\w+)["\']?\s+FOREIGN\s+KEY'
    for match in re.finditer(fk_pattern, original_sql or "", re.IGNORECASE):
        name = match.group(1)
        # Find corresponding FK in pragma results
        for fk in existing_fks:
            if fk["id"] not in fk_name_to_id.values():
                fk_name_to_id[name] = fk["id"]
                break

    fk_id_to_remove = fk_name_to_id.get(fk_name_to_remove)

    for fk in existing_fks:
        fk_id = fk["id"]
        if fk_id == fk_id_to_remove:
            continue
        if fk_id not in fk_groups:
            fk_groups[fk_id] = {
                "columns": [],
                "ref_table": fk["references_table"],
                "ref_columns": [],
                "on_delete": fk["on_delete"],
                "on_update": fk["on_update"],
            }
        fk_groups[fk_id]["columns"].append(fk["from_column"])
        fk_groups[fk_id]["ref_columns"].append(fk["to_column"])

    for fk_id, fk_data in fk_groups.items():
        fk_def = (
            f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) "
            f"REFERENCES {quote_identifier(fk_data['ref_table'])} "
            f"({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        )
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        col_defs.append(fk_def)

    # Add check constraints
    for chk in check_constraints:
        if chk["name"]:
            col_defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            col_defs.append(f"CHECK ({chk['expression']})")

    sql = f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
    return sql


def generate_table_sql_with_check_constraint(
    cursor: sqlite3.Cursor,
    table_name: str,
    constraint_name: str,
    expression: str
) -> str:
    """Generate CREATE TABLE SQL with an additional check constraint."""
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)

    col_defs = []
    pk_columns = []

    for row in table_info:
        col_name = row[1]
        col_type = row[2]
        not_null = row[3]
        default = row[4]
        pk = row[5]

        parts = [quote_identifier(col_name), col_type]

        if pk > 0:
            pk_columns.append((pk, col_name))

        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")

        col_defs.append(" ".join(parts))

    # Add primary key constraint
    if pk_columns:
        pk_columns_sorted = sorted(pk_columns, key=lambda x: x[0])
        if len(pk_columns_sorted) == 1:
            for i, col_def in enumerate(col_defs):
                if quote_identifier(pk_columns_sorted[0][1]) in col_def:
                    col_defs[i] = col_def + " PRIMARY KEY"
                    break
        else:
            pk_col_names = [quote_identifier(c[1]) for c in pk_columns_sorted]
            col_defs.append(f"PRIMARY KEY ({', '.join(pk_col_names)})")

    # Add existing foreign keys
    fk_groups = {}
    for fk in existing_fks:
        fk_id = fk["id"]
        if fk_id not in fk_groups:
            fk_groups[fk_id] = {
                "columns": [],
                "ref_table": fk["references_table"],
                "ref_columns": [],
                "on_delete": fk["on_delete"],
                "on_update": fk["on_update"],
            }
        fk_groups[fk_id]["columns"].append(fk["from_column"])
        fk_groups[fk_id]["ref_columns"].append(fk["to_column"])

    for fk_id, fk_data in fk_groups.items():
        fk_def = (
            f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) "
            f"REFERENCES {quote_identifier(fk_data['ref_table'])} "
            f"({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        )
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        col_defs.append(fk_def)

    # Add existing check constraints
    for chk in check_constraints:
        if chk["name"]:
            col_defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            col_defs.append(f"CHECK ({chk['expression']})")

    # Add new check constraint
    col_defs.append(f"CONSTRAINT {quote_identifier(constraint_name)} CHECK ({expression})")

    sql = f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
    return sql


def generate_table_sql_without_check_constraint(
    cursor: sqlite3.Cursor,
    table_name: str,
    constraint_name_to_remove: str
) -> str:
    """Generate CREATE TABLE SQL without a specific check constraint."""
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)

    col_defs = []
    pk_columns = []

    for row in table_info:
        col_name = row[1]
        col_type = row[2]
        not_null = row[3]
        default = row[4]
        pk = row[5]

        parts = [quote_identifier(col_name), col_type]

        if pk > 0:
            pk_columns.append((pk, col_name))

        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")

        col_defs.append(" ".join(parts))

    # Add primary key constraint
    if pk_columns:
        pk_columns_sorted = sorted(pk_columns, key=lambda x: x[0])
        if len(pk_columns_sorted) == 1:
            for i, col_def in enumerate(col_defs):
                if quote_identifier(pk_columns_sorted[0][1]) in col_def:
                    col_defs[i] = col_def + " PRIMARY KEY"
                    break
        else:
            pk_col_names = [quote_identifier(c[1]) for c in pk_columns_sorted]
            col_defs.append(f"PRIMARY KEY ({', '.join(pk_col_names)})")

    # Add existing foreign keys
    fk_groups = {}
    for fk in existing_fks:
        fk_id = fk["id"]
        if fk_id not in fk_groups:
            fk_groups[fk_id] = {
                "columns": [],
                "ref_table": fk["references_table"],
                "ref_columns": [],
                "on_delete": fk["on_delete"],
                "on_update": fk["on_update"],
            }
        fk_groups[fk_id]["columns"].append(fk["from_column"])
        fk_groups[fk_id]["ref_columns"].append(fk["to_column"])

    for fk_id, fk_data in fk_groups.items():
        fk_def = (
            f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) "
            f"REFERENCES {quote_identifier(fk_data['ref_table'])} "
            f"({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        )
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        col_defs.append(fk_def)

    # Add check constraints except the one to remove
    for chk in check_constraints:
        if chk["name"] == constraint_name_to_remove:
            continue
        if chk["name"]:
            col_defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            col_defs.append(f"CHECK ({chk['expression']})")

    sql = f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
    return sql


def execute_add_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    fk_name = operation["name"]
    fk_columns = operation["columns"]
    references = operation["references"]
    ref_table = references["table"]
    ref_columns = references["columns"]
    on_delete = operation.get("on_delete")
    on_update = operation.get("on_update")

    # Validate table exists
    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    # Validate referenced table exists
    if not table_exists(cursor, ref_table):
        error_exit(f"cannot add foreign key: referenced table '{ref_table}' does not exist")

    # Validate source columns exist
    table_columns = get_column_names(cursor, table_name)
    for col in fk_columns:
        if col not in table_columns:
            error_exit(f"cannot add foreign key: column '{col}' does not exist in table '{table_name}'")

    # Validate referenced columns exist
    ref_table_columns = get_column_names(cursor, ref_table)
    for col in ref_columns:
        if col not in ref_table_columns:
            error_exit(f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist")

    # Check if foreign key with this name already exists
    if foreign_key_exists(cursor, table_name, fk_name):
        error_exit(f"foreign key '{fk_name}' already exists in table '{table_name}'")

    # Generate new table SQL with foreign key
    new_table_sql = generate_table_sql_with_foreign_key(
        cursor, table_name, fk_name, fk_columns,
        ref_table, ref_columns, on_delete, on_update
    )

    # Rebuild table
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "add_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })


def execute_drop_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    fk_name = operation["name"]

    # Validate table exists
    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    # Validate foreign key exists
    if not foreign_key_exists(cursor, table_name, fk_name):
        error_exit(f"foreign key '{fk_name}' does not exist in table '{table_name}'")

    # Get table columns for data copy
    table_columns = get_column_names(cursor, table_name)

    # Generate new table SQL without the foreign key
    new_table_sql = generate_table_sql_without_foreign_key(cursor, table_name, fk_name)

    # Rebuild table
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "drop_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })


def execute_create_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    index_name = operation["name"]
    table_name = operation["table"]
    columns = operation["columns"]
    unique = operation.get("unique", False)

    # Validate table exists
    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    # Validate columns exist
    table_columns = get_column_names(cursor, table_name)
    for col in columns:
        if col not in table_columns:
            error_exit(f"invalid operation: column '{col}' does not exist in table '{table_name}'")

    # Check if index already exists
    if index_exists(cursor, index_name):
        error_exit(f"index '{index_name}' already exists")

    # Build CREATE INDEX SQL
    unique_sql = "UNIQUE " if unique else ""
    columns_sql = ", ".join(quote_identifier(col) for col in columns)
    sql = f"CREATE {unique_sql}INDEX {quote_identifier(index_name)} ON {quote_identifier(table_name)} ({columns_sql})"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "create_index",
        "name": index_name,
        "table": table_name,
        "version": version
    })


def execute_drop_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    index_name = operation["name"]

    # Check if index exists
    if not index_exists(cursor, index_name):
        error_exit(f"index '{index_name}' does not exist")

    sql = f"DROP INDEX {quote_identifier(index_name)}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "drop_index",
        "name": index_name,
        "version": version
    })


def execute_add_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    constraint_name = operation["name"]
    expression = operation["expression"]

    # Validate table exists
    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    # Check if constraint with this name already exists
    if check_constraint_exists(cursor, table_name, constraint_name):
        error_exit(f"check constraint '{constraint_name}' already exists in table '{table_name}'")

    # Get table columns for data copy
    table_columns = get_column_names(cursor, table_name)

    # Save indexes before dropping the table
    index_info = get_index_info(cursor, table_name)

    # Generate new table SQL with check constraint
    new_table_sql = generate_table_sql_with_check_constraint(
        cursor, table_name, constraint_name, expression
    )

    try:
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Create new table
        cursor.execute(new_table_sql)

        # Copy data - this will validate the check constraint
        columns_select = ", ".join(quote_identifier(col) for col in table_columns)
        cursor.execute(
            f"INSERT INTO {quote_identifier(f'_migration_temp_{table_name}')} "
            f"SELECT {columns_select} FROM {quote_identifier(table_name)}"
        )

        # Drop old table
        cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")

        # Rename new table
        cursor.execute(
            f"ALTER TABLE {quote_identifier(f'_migration_temp_{table_name}')} "
            f"RENAME TO {quote_identifier(table_name)}"
        )

        # Recreate indexes
        for idx in index_info:
            try:
                cursor.execute(idx["sql"])
            except sqlite3.Error:
                pass

    except sqlite3.IntegrityError as e:
        error_exit(f"check constraint violation: {expression}")
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")
    finally:
        cursor.execute("PRAGMA foreign_keys = ON")

    output_json({
        "event": "operation_applied",
        "type": "add_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })


def execute_drop_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    constraint_name = operation["name"]

    # Validate table exists
    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    # Validate check constraint exists
    if not check_constraint_exists(cursor, table_name, constraint_name):
        error_exit(f"check constraint '{constraint_name}' does not exist in table '{table_name}'")

    # Get table columns for data copy
    table_columns = get_column_names(cursor, table_name)

    # Generate new table SQL without the check constraint
    new_table_sql = generate_table_sql_without_check_constraint(cursor, table_name, constraint_name)

    # Rebuild table
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "drop_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })


def execute_drop_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column_name = operation["column"]

    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    col_info = cursor.fetchall()
    if not col_info:
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = [row[1] for row in col_info]
    if column_name not in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")
    if len(existing_columns) == 1:
        error_exit(f"invalid operation: cannot drop the only column in table '{table_name}'")

    col_by_name = {row[1]: row for row in col_info}
    col_row = col_by_name[column_name]
    if col_row[5] > 0:
        error_exit(f"invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    columns_to_keep = [col for col in existing_columns if col != column_name]

    new_columns_sql_parts = []
    for col_name in columns_to_keep:
        row = col_by_name[col_name]
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


def execute_transform_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    transformations = operation["transformations"]

    existing_columns = get_column_names(cursor, table_name)
    if not existing_columns:
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    for transform in transformations:
        column_name = transform["column"]
        expression = transform["expression"]

        if column_name not in existing_columns:
            error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

        sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {expression}"

        try:
            cursor.execute(sql)
        except sqlite3.Error as e:
            error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "transform_data",
        "table": table_name,
        "version": version
    })


def execute_migrate_column_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    from_column = operation["from_column"]
    to_column = operation["to_column"]
    default_value = operation.get("default_value")

    existing_columns = get_column_names(cursor, table_name)
    if not existing_columns:
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    if from_column not in existing_columns:
        error_exit(f"invalid operation: column '{from_column}' does not exist in table '{table_name}'")
    if to_column not in existing_columns:
        error_exit(f"invalid operation: column '{to_column}' does not exist in table '{table_name}'")

    to_col = quote_identifier(to_column)
    from_col = quote_identifier(from_column)
    value_expr = (
        f"CASE WHEN {from_col} IS NOT NULL THEN {from_col} ELSE {default_value} END"
        if default_value is not None else from_col
    )
    sql = f"UPDATE {quote_identifier(table_name)} SET {to_col} = {value_expr}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "migrate_column_data",
        "table": table_name,
        "from_column": from_column,
        "to_column": to_column,
        "version": version
    })


def execute_backfill_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column_name = operation["column"]
    value = operation["value"]
    where_clause = operation.get("where")

    existing_columns = get_column_names(cursor, table_name)
    if not existing_columns:
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    if column_name not in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")

    sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {value}"
    if where_clause:
        sql += f" WHERE {where_clause}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        error_exit(f"SQL error: {e}")

    output_json({
        "event": "operation_applied",
        "type": "backfill_data",
        "table": table_name,
        "column": column_name,
        "version": version
    })


EXECUTORS = {
    "create_table": execute_create_table,
    "add_column": execute_add_column,
    "drop_column": execute_drop_column,
    "transform_data": execute_transform_data,
    "migrate_column_data": execute_migrate_column_data,
    "backfill_data": execute_backfill_data,
    "add_foreign_key": execute_add_foreign_key,
    "drop_foreign_key": execute_drop_foreign_key,
    "create_index": execute_create_index,
    "drop_index": execute_drop_index,
    "add_check_constraint": execute_add_check_constraint,
    "drop_check_constraint": execute_drop_check_constraint,
}


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
        # Enable foreign keys at the start
        cursor.execute("PRAGMA foreign_keys = ON")

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

        # Process each operation
        # Foreign key operations and check constraints require temporarily disabling FK
        for operation in operations:
            op_type = operation["type"]

            # For operations that rebuild tables, we need to manage FK pragma
            if op_type in ("add_foreign_key", "drop_foreign_key", "add_check_constraint", "drop_check_constraint", "drop_column"):
                # These operations handle their own PRAGMA management
                EXECUTORS[op_type](cursor, operation, version)
            else:
                EXECUTORS[op_type](cursor, operation, version)

        record_migration(cursor, version, description)

        # Ensure foreign keys are enabled
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
