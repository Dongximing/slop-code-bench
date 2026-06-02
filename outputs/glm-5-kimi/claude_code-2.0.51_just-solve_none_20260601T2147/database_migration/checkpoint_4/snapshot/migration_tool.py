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

# Operations that cannot be automatically rolled back
NON_REVERSIBLE_OPS = {"transform_data", "backfill_data"}


def error_exit(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def warning(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def output_json(event: Dict[str, Any]) -> None:
    print(json.dumps(event, separators=(',', ':')))


def require_field(obj: Dict[str, Any], field: str, expected_type: type, type_name: str) -> Any:
    if field not in obj:
        error_exit(f"invalid migration schema: missing required field '{field}'")
    if not isinstance(obj[field], expected_type):
        error_exit(f"invalid migration schema: '{field}' must be {type_name}")
    return obj[field]


def validate_identifier(name: str, kind: str) -> str:
    if not IDENTIFIER_PATTERN.match(name):
        error_exit(f"invalid {kind} name: '{name}'")
    return name


def quote_identifier(name: str) -> str:
    return f'"{name.replace(chr(34), chr(34) + chr(34))}"'


def validate_column(column: Dict[str, Any]) -> None:
    name = require_field(column, "name", str, "a string")
    col_type = require_field(column, "type", str, "a string")
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
    table = require_field(operation, "table", str, "a string")
    return validate_identifier(table, "table")


def validate_create_table(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    columns = require_field(operation, "columns", list, "an array")
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
    column = require_field(operation, "column", dict, "an object")
    validate_column(column)


def validate_drop_column(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    column = require_field(operation, "column", str, "a string")
    validate_identifier(column, "column")


def validate_transform_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    transformations = require_field(operation, "transformations", list, "an array")
    if not transformations:
        error_exit("invalid migration schema: 'transformations' array cannot be empty")

    for transform in transformations:
        if not isinstance(transform, dict):
            error_exit("invalid migration schema: each transformation must be an object")
        column = require_field(transform, "column", str, "a string")
        validate_identifier(column, "column")
        require_field(transform, "expression", str, "a string")


def validate_migrate_column_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    from_column = require_field(operation, "from_column", str, "a string")
    validate_identifier(from_column, "column")
    to_column = require_field(operation, "to_column", str, "a string")
    validate_identifier(to_column, "column")
    if "default_value" in operation and operation["default_value"] is not None:
        if not isinstance(operation["default_value"], str):
            error_exit("invalid migration schema: 'default_value' must be a string or null")


def validate_backfill_data(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    column = require_field(operation, "column", str, "a string")
    validate_identifier(column, "column")
    require_field(operation, "value", str, "a string")
    if "where" in operation:
        if not isinstance(operation["where"], str):
            error_exit("invalid migration schema: 'where' must be a string")


def validate_add_foreign_key(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_field(operation, "name", str, "a string")
    validate_identifier(name, "foreign key")

    columns = require_field(operation, "columns", list, "an array")
    if not columns:
        error_exit("invalid migration schema: 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            error_exit("invalid migration schema: each column must be a string")
        validate_identifier(col, "column")

    references = require_field(operation, "references", dict, "an object")
    ref_table = require_field(references, "table", str, "a string")
    validate_identifier(ref_table, "table")

    ref_columns = require_field(references, "columns", list, "an array")
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
    name = require_field(operation, "name", str, "a string")
    validate_identifier(name, "foreign key")


def validate_create_index(operation: Dict[str, Any]) -> None:
    name = require_field(operation, "name", str, "a string")
    validate_identifier(name, "index")
    validate_table_ref(operation)

    columns = require_field(operation, "columns", list, "an array")
    if not columns:
        error_exit("invalid migration schema: 'columns' array cannot be empty")
    for col in columns:
        if not isinstance(col, str):
            error_exit("invalid migration schema: each column must be a string")
        validate_identifier(col, "column")

    if "unique" in operation and not isinstance(operation["unique"], bool):
        error_exit("invalid migration schema: 'unique' must be a boolean")


def validate_drop_index(operation: Dict[str, Any]) -> None:
    name = require_field(operation, "name", str, "a string")
    validate_identifier(name, "index")


def validate_add_check_constraint(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_field(operation, "name", str, "a string")
    validate_identifier(name, "check constraint")
    require_field(operation, "expression", str, "a string")


def validate_drop_check_constraint(operation: Dict[str, Any]) -> None:
    validate_table_ref(operation)
    name = require_field(operation, "name", str, "a string")
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
    op_type = require_field(operation, "type", str, "a string")
    validator = VALIDATORS.get(op_type)
    if not validator:
        error_exit(f"invalid operation type: '{op_type}'")
    validator(operation)


def validate_migration(migration: Dict[str, Any]) -> None:
    version = require_field(migration, "version", int, "an integer")
    if version <= 0:
        error_exit("invalid migration schema: 'version' must be a positive integer")

    require_field(migration, "description", str, "a string")

    operations = require_field(migration, "operations", list, "an array")
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
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return cursor.fetchall()


def get_foreign_keys(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    cursor.execute(f"PRAGMA foreign_key_list({quote_identifier(table_name)})")
    fks = []
    for row in cursor.fetchall():
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
    cursor.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table_name,)
    )
    return [{"name": row[0], "sql": row[1]} for row in cursor.fetchall()]


def get_check_constraints(cursor: sqlite3.Cursor, table_name: str) -> List[Dict[str, Any]]:
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    row = cursor.fetchone()
    if not row or not row[0]:
        return []

    pattern = r'(?:CONSTRAINT\s+["\']?(\w+)["\']?\s+)?CHECK\s*\(([^)]+(?:\([^)]*\)[^)]*)*)\)'
    return [{"name": m.group(1), "expression": m.group(2).strip()} for m in re.finditer(pattern, row[0], re.IGNORECASE)]


def get_table_sql(cursor: sqlite3.Cursor, table_name: str) -> Optional[str]:
    cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    row = cursor.fetchone()
    return row[0] if row else None


def index_exists(cursor: sqlite3.Cursor, index_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    return cursor.fetchone() is not None


def table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    )
    return cursor.fetchone() is not None


def foreign_key_exists(cursor: sqlite3.Cursor, table_name: str, fk_name: str) -> bool:
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return False
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(fk_name)}["\']?\s+FOREIGN\s+KEY'
    return bool(re.search(pattern, sql, re.IGNORECASE))


def check_constraint_exists(cursor: sqlite3.Cursor, table_name: str, constraint_name: str) -> bool:
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return False
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(constraint_name)}["\']?\s+CHECK'
    return bool(re.search(pattern, sql, re.IGNORECASE))


# ─── Metadata helpers ────────────────────────────────────────────────────

def get_column_full_info(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> Optional[Dict[str, Any]]:
    """Get complete column definition info for rollback metadata."""
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    for row in cursor.fetchall():
        if row[1] == column_name:
            col_info = {
                "name": row[1],
                "type": row[2],
                "not_null": bool(row[3]),
            }
            if row[4] is not None:
                col_info["default"] = str(row[4])
            if row[5] > 0:
                col_info["primary_key"] = True
            return col_info
    return None


def get_fk_full_info(cursor: sqlite3.Cursor, table_name: str, fk_name: str) -> Optional[Dict[str, Any]]:
    """Get full foreign key info for rollback metadata by parsing table SQL."""
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return None

    # Find the FK constraint with this name - handle quoted identifiers
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(fk_name)}["\']?\s+FOREIGN\s+KEY\s*\(([^)]+)\)\s*REFERENCES\s+["\']?(\w+)["\']?\s*\(([^)]+)\)(?:\s+ON\s+DELETE\s+(\w+))?(?:\s+ON\s+UPDATE\s+(\w+))?'
    m = re.search(pattern, sql, re.IGNORECASE)
    if not m:
        return None

    fk_columns = [c.strip().strip('"') for c in m.group(1).split(",")]
    ref_table = m.group(2)
    ref_columns = [c.strip().strip('"') for c in m.group(3).split(",")]
    on_delete = m.group(4)
    on_update = m.group(5)

    return {
        "name": fk_name,
        "table": table_name,
        "columns": fk_columns,
        "referenced_table": ref_table,
        "referenced_columns": ref_columns,
        "on_delete": on_delete,
        "on_update": on_update,
    }


def get_index_full_info(cursor: sqlite3.Cursor, index_name: str) -> Optional[Dict[str, Any]]:
    """Get full index info for rollback metadata."""
    cursor.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    row = cursor.fetchone()
    if not row:
        return None

    idx_name, tbl_name, sql = row
    unique = bool(sql and "UNIQUE" in sql.upper().split("INDEX")[0])

    # Get columns from index_info
    cursor.execute(f"PRAGMA index_info({quote_identifier(index_name)})")
    cols = [r[2] for r in cursor.fetchall()]

    return {
        "name": idx_name,
        "table": tbl_name,
        "columns": cols,
        "unique": unique,
    }


def get_check_constraint_full_info(cursor: sqlite3.Cursor, table_name: str, constraint_name: str) -> Optional[Dict[str, Any]]:
    """Get full check constraint info for rollback metadata."""
    constraints = get_check_constraints(cursor, table_name)
    for chk in constraints:
        if chk["name"] == constraint_name:
            return {
                "name": constraint_name,
                "table": table_name,
                "expression": chk["expression"],
            }
    return None


# ─── Table rebuild helpers ───────────────────────────────────────────────

def rebuild_table_with_modification(
    cursor: sqlite3.Cursor,
    table_name: str,
    new_table_sql: str,
    columns_to_copy: List[str]
) -> None:
    temp_table_name = f"_migration_temp_{table_name}"
    index_info = get_index_info(cursor, table_name)

    try:
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute(new_table_sql)

        columns_select = ", ".join(quote_identifier(col) for col in columns_to_copy)
        cursor.execute(
            f"INSERT INTO {quote_identifier(temp_table_name)} "
            f"SELECT {columns_select} FROM {quote_identifier(table_name)}"
        )
        cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
        cursor.execute(
            f"ALTER TABLE {quote_identifier(temp_table_name)} "
            f"RENAME TO {quote_identifier(table_name)}"
        )

        for idx in index_info:
            try:
                cursor.execute(idx["sql"])
            except sqlite3.Error:
                pass
    finally:
        cursor.execute("PRAGMA foreign_keys = ON")


def generate_modified_table_sql(
    cursor: sqlite3.Cursor,
    table_name: str,
    add_fk: Optional[Dict[str, Any]] = None,
    remove_fk: Optional[str] = None,
    add_check: Optional[Dict[str, Any]] = None,
    remove_check: Optional[str] = None,
) -> str:
    """Generate CREATE TABLE SQL with optional FK/check constraint modifications."""
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)
    original_sql = get_table_sql(cursor, table_name)

    col_defs = []
    pk_columns = []

    for row in table_info:
        col_name, col_type, not_null, default, pk = row[1], row[2], row[3], row[4], row[5]
        parts = [quote_identifier(col_name), col_type]
        if pk > 0:
            pk_columns.append((pk, col_name))
        if not_null:
            parts.append("NOT NULL")
        if default is not None:
            parts.append(f"DEFAULT {default}")
        col_defs.append(" ".join(parts))

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

    fk_name_to_id = {}
    if remove_fk and original_sql:
        for match in re.finditer(r'CONSTRAINT\s+["\']?(\w+)["\']?\s+FOREIGN\s+KEY', original_sql, re.IGNORECASE):
            for fk in existing_fks:
                if fk["id"] not in fk_name_to_id.values():
                    fk_name_to_id[match.group(1)] = fk["id"]
                    break
    fk_id_to_remove = fk_name_to_id.get(remove_fk)

    fk_groups = {}
    for fk in existing_fks:
        if fk["id"] == fk_id_to_remove:
            continue
        fk_id = fk["id"]
        if fk_id not in fk_groups:
            fk_groups[fk_id] = {"columns": [], "ref_table": fk["references_table"], "ref_columns": [], "on_delete": fk["on_delete"], "on_update": fk["on_update"]}
        fk_groups[fk_id]["columns"].append(fk["from_column"])
        fk_groups[fk_id]["ref_columns"].append(fk["to_column"])

    for fk_data in fk_groups.values():
        fk_def = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) REFERENCES {quote_identifier(fk_data['ref_table'])} ({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        col_defs.append(fk_def)

    if add_fk:
        fk_def = f"CONSTRAINT {quote_identifier(add_fk['name'])} FOREIGN KEY ({', '.join(quote_identifier(c) for c in add_fk['columns'])}) REFERENCES {quote_identifier(add_fk['ref_table'])} ({', '.join(quote_identifier(c) for c in add_fk['ref_columns'])})"
        if add_fk.get("on_delete"):
            fk_def += f" ON DELETE {add_fk['on_delete'].upper()}"
        if add_fk.get("on_update"):
            fk_def += f" ON UPDATE {add_fk['on_update'].upper()}"
        col_defs.append(fk_def)

    for chk in check_constraints:
        if chk["name"] == remove_check:
            continue
        if chk["name"]:
            col_defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            col_defs.append(f"CHECK ({chk['expression']})")

    if add_check:
        col_defs.append(f"CONSTRAINT {quote_identifier(add_check['name'])} CHECK ({add_check['expression']})")

    return f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"


# ─── Forward operation executors (with metadata enrichment) ──────────────

def execute_create_table(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    # Return enriched operation for storage
    return operation.copy()


def execute_add_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    return operation.copy()


def execute_drop_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    # Store original column definition for rollback
    original_def = get_column_full_info(cursor, table_name, column_name)

    columns_to_keep = [col for col in existing_columns if col != column_name]

    new_columns_sql_parts = []
    for c_name in columns_to_keep:
        row = col_by_name[c_name]
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

    enriched = operation.copy()
    if original_def:
        enriched["original_definition"] = original_def
    return enriched


def execute_transform_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    return operation.copy()


def execute_migrate_column_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    return operation.copy()


def execute_backfill_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
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

    return operation.copy()


def execute_add_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    table_name = operation["table"]
    fk_name = operation["name"]
    fk_columns = operation["columns"]
    references = operation["references"]
    ref_table = references["table"]
    ref_columns = references["columns"]
    on_delete = operation.get("on_delete")
    on_update = operation.get("on_update")

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")
    if not table_exists(cursor, ref_table):
        error_exit(f"cannot add foreign key: referenced table '{ref_table}' does not exist")

    table_columns = get_column_names(cursor, table_name)
    for col in fk_columns:
        if col not in table_columns:
            error_exit(f"cannot add foreign key: column '{col}' does not exist in table '{table_name}'")

    ref_table_columns = get_column_names(cursor, ref_table)
    for col in ref_columns:
        if col not in ref_table_columns:
            error_exit(f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist")

    if foreign_key_exists(cursor, table_name, fk_name):
        error_exit(f"foreign key '{fk_name}' already exists in table '{table_name}'")

    new_table_sql = generate_modified_table_sql(
        cursor, table_name,
        add_fk={"name": fk_name, "columns": fk_columns, "ref_table": ref_table,
                 "ref_columns": ref_columns, "on_delete": on_delete, "on_update": on_update}
    )
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "add_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })

    return operation.copy()


def execute_drop_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    table_name = operation["table"]
    fk_name = operation["name"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")
    if not foreign_key_exists(cursor, table_name, fk_name):
        error_exit(f"foreign key '{fk_name}' does not exist in table '{table_name}'")

    # Store original FK definition for rollback
    original_def = get_fk_full_info(cursor, table_name, fk_name)

    table_columns = get_column_names(cursor, table_name)
    new_table_sql = generate_modified_table_sql(cursor, table_name, remove_fk=fk_name)
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "drop_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })

    enriched = operation.copy()
    if original_def:
        enriched["original_definition"] = original_def
    return enriched


def execute_create_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    index_name = operation["name"]
    table_name = operation["table"]
    columns = operation["columns"]
    unique = operation.get("unique", False)

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    table_columns = get_column_names(cursor, table_name)
    for col in columns:
        if col not in table_columns:
            error_exit(f"invalid operation: column '{col}' does not exist in table '{table_name}'")

    if index_exists(cursor, index_name):
        error_exit(f"index '{index_name}' already exists")

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

    return operation.copy()


def execute_drop_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    index_name = operation["name"]

    if not index_exists(cursor, index_name):
        error_exit(f"index '{index_name}' does not exist")

    # Store original index definition for rollback
    original_def = get_index_full_info(cursor, index_name)

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

    enriched = operation.copy()
    if original_def:
        enriched["original_definition"] = original_def
    return enriched


def execute_add_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    table_name = operation["table"]
    constraint_name = operation["name"]
    expression = operation["expression"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")
    if check_constraint_exists(cursor, table_name, constraint_name):
        error_exit(f"check constraint '{constraint_name}' already exists in table '{table_name}'")

    table_columns = get_column_names(cursor, table_name)
    new_table_sql = generate_modified_table_sql(
        cursor, table_name,
        add_check={"name": constraint_name, "expression": expression}
    )

    try:
        rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)
    except sqlite3.IntegrityError:
        error_exit(f"check constraint violation: {expression}")

    output_json({
        "event": "operation_applied",
        "type": "add_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })

    return operation.copy()


def execute_drop_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
    table_name = operation["table"]
    constraint_name = operation["name"]

    if not table_exists(cursor, table_name):
        error_exit(f"invalid operation: table '{table_name}' does not exist")
    if not check_constraint_exists(cursor, table_name, constraint_name):
        error_exit(f"check constraint '{constraint_name}' does not exist in table '{table_name}'")

    # Store original constraint definition for rollback
    original_def = get_check_constraint_full_info(cursor, table_name, constraint_name)

    table_columns = get_column_names(cursor, table_name)
    new_table_sql = generate_modified_table_sql(cursor, table_name, remove_check=constraint_name)
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)

    output_json({
        "event": "operation_applied",
        "type": "drop_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })

    enriched = operation.copy()
    if original_def:
        enriched["original_definition"] = original_def
    return enriched


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


# ─── Migration table management ──────────────────────────────────────────

def init_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Create _migrations table with operations column for rollback support."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operations TEXT NOT NULL DEFAULT '[]'
        )
    """)

    # Migrate old table: add operations column if it doesn't exist
    cursor.execute("PRAGMA table_info(_migrations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "operations" not in columns:
        cursor.execute("ALTER TABLE _migrations ADD COLUMN operations TEXT NOT NULL DEFAULT '[]'")


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str, operations_json: str) -> None:
    cursor.execute(
        "INSERT INTO _migrations (version, description, operations) VALUES (?, ?, ?)",
        (version, description, operations_json)
    )


def get_applied_migrations(cursor: sqlite3.Cursor) -> List[Tuple[int, str, str]]:
    """Get all applied migrations as (version, description, operations_json)."""
    cursor.execute("SELECT version, description, operations FROM _migrations ORDER BY version")
    return cursor.fetchall()


def get_current_version(cursor: sqlite3.Cursor) -> int:
    cursor.execute("SELECT MAX(version) FROM _migrations")
    result = cursor.fetchone()
    return result[0] if result[0] is not None else 0


# ─── Rollback operation executors ────────────────────────────────────────

def rollback_create_table(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    output_json({
        "event": "operation_rolled_back",
        "type": "create_table",
        "table": table_name,
        "version": version
    })
    cursor.execute(f"DROP TABLE IF EXISTS {quote_identifier(table_name)}")


def rollback_add_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column = operation.get("column", {})
    col_name = column.get("name") if isinstance(column, dict) else column
    output_json({
        "event": "operation_rolled_back",
        "type": "add_column",
        "table": table_name,
        "column": col_name,
        "version": version
    })
    cursor.execute(f"ALTER TABLE {quote_identifier(table_name)} DROP COLUMN {quote_identifier(col_name)}")


def rollback_drop_column(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    column_name = operation["column"]
    output_json({
        "event": "operation_rolled_back",
        "type": "drop_column",
        "table": table_name,
        "column": column_name,
        "version": version
    })

    # Get original definition
    orig_def = operation.get("original_definition")
    if orig_def:
        col_sql = build_column_sql(orig_def)
    else:
        col_sql = f"{quote_identifier(column_name)} TEXT"

    cursor.execute(f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN {col_sql}")


def rollback_add_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    fk_name = operation.get("name", "")
    output_json({
        "event": "operation_rolled_back",
        "type": "add_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })

    # To remove a FK, we need to rebuild the table without it
    table_columns = get_column_names(cursor, table_name)
    new_table_sql = generate_modified_table_sql(cursor, table_name, remove_fk=fk_name)
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)


def rollback_drop_foreign_key(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    fk_name = operation.get("name", "")
    output_json({
        "event": "operation_rolled_back",
        "type": "drop_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    })

    orig_def = operation.get("original_definition")
    if orig_def:
        table_columns = get_column_names(cursor, table_name)
        new_table_sql = generate_modified_table_sql(
            cursor, table_name,
            add_fk={
                "name": orig_def["name"],
                "columns": orig_def["columns"],
                "ref_table": orig_def["referenced_table"],
                "ref_columns": orig_def["referenced_columns"],
                "on_delete": orig_def.get("on_delete"),
                "on_update": orig_def.get("on_update"),
            }
        )
        rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)


def rollback_create_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    index_name = operation["name"]
    output_json({
        "event": "operation_rolled_back",
        "type": "create_index",
        "name": index_name,
        "table": operation.get("table", ""),
        "version": version
    })
    cursor.execute(f"DROP INDEX IF EXISTS {quote_identifier(index_name)}")


def rollback_drop_index(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    index_name = operation["name"]
    output_json({
        "event": "operation_rolled_back",
        "type": "drop_index",
        "name": index_name,
        "version": version
    })

    orig_def = operation.get("original_definition")
    if orig_def:
        unique_sql = "UNIQUE " if orig_def.get("unique") else ""
        cols_sql = ", ".join(quote_identifier(c) for c in orig_def["columns"])
        sql = f"CREATE {unique_sql}INDEX {quote_identifier(index_name)} ON {quote_identifier(orig_def['table'])} ({cols_sql})"
        cursor.execute(sql)


def rollback_add_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    constraint_name = operation["name"]
    output_json({
        "event": "operation_rolled_back",
        "type": "add_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })

    table_columns = get_column_names(cursor, table_name)
    new_table_sql = generate_modified_table_sql(cursor, table_name, remove_check=constraint_name)
    rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)


def rollback_drop_check_constraint(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    constraint_name = operation["name"]
    output_json({
        "event": "operation_rolled_back",
        "type": "drop_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    })

    orig_def = operation.get("original_definition")
    if orig_def:
        table_columns = get_column_names(cursor, table_name)
        new_table_sql = generate_modified_table_sql(
            cursor, table_name,
            add_check={"name": orig_def["name"], "expression": orig_def["expression"]}
        )
        rebuild_table_with_modification(cursor, table_name, new_table_sql, table_columns)


def rollback_migrate_column_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    table_name = operation["table"]
    from_column = operation["from_column"]
    to_column = operation["to_column"]
    output_json({
        "event": "operation_rolled_back",
        "type": "migrate_column_data",
        "table": table_name,
        "from_column": from_column,
        "to_column": to_column,
        "version": version
    })

    # Reverse: copy from to_column back to from_column
    sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(from_column)} = {quote_identifier(to_column)}"
    cursor.execute(sql)


def rollback_transform_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute transform_data as explicit rollback operation."""
    table_name = operation["table"]
    transformations = operation["transformations"]
    output_json({
        "event": "operation_rolled_back",
        "type": "transform_data",
        "table": table_name,
        "version": version
    })

    for transform in transformations:
        column_name = transform["column"]
        expression = transform["expression"]
        sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {expression}"
        cursor.execute(sql)


def rollback_backfill_data(cursor: sqlite3.Cursor, operation: Dict[str, Any], version: int) -> None:
    """Execute backfill_data as explicit rollback operation."""
    table_name = operation["table"]
    column_name = operation["column"]
    value = operation["value"]
    where_clause = operation.get("where")
    output_json({
        "event": "operation_rolled_back",
        "type": "backfill_data",
        "table": table_name,
        "column": column_name,
        "version": version
    })

    sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column_name)} = {value}"
    if where_clause:
        sql += f" WHERE {where_clause}"
    cursor.execute(sql)


ROLLBACK_EXECUTORS = {
    "create_table": rollback_create_table,
    "add_column": rollback_add_column,
    "drop_column": rollback_drop_column,
    "add_foreign_key": rollback_add_foreign_key,
    "drop_foreign_key": rollback_drop_foreign_key,
    "create_index": rollback_create_index,
    "drop_index": rollback_drop_index,
    "add_check_constraint": rollback_add_check_constraint,
    "drop_check_constraint": rollback_drop_check_constraint,
    "migrate_column_data": rollback_migrate_column_data,
    "transform_data": rollback_transform_data,
    "backfill_data": rollback_backfill_data,
}


# ─── Commands ────────────────────────────────────────────────────────────

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

        # Execute operations and collect enriched metadata for rollback
        enriched_operations = []
        for operation in operations:
            enriched_op = EXECUTORS[operation["type"]](cursor, operation, version)
            enriched_operations.append(enriched_op)

        record_migration(cursor, version, description, json.dumps(enriched_operations))
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


def run_rollback(db_file: str, to_version: Optional[int] = None, count: Optional[int] = None) -> None:
    """Execute the rollback command."""
    if not os.path.exists(db_file):
        error_exit(f"database file not found: {db_file}")

    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        error_exit(f"error connecting to database: {e}")

    try:
        init_migrations_table(cursor)

        # Get all applied migrations
        applied = get_applied_migrations(cursor)

        if not applied:
            error_exit("no migrations to rollback")

        current_version = get_current_version(cursor)

        # Determine which versions to rollback
        if to_version is not None:
            # --to-version: rollback everything above this version (exclusive)
            target_version = to_version
            versions_to_rollback = [v for v, _, _ in applied if v > target_version]

            if not versions_to_rollback:
                # Check if the target version exists at all
                applied_versions = set(v for v, _, _ in applied)
                if target_version not in applied_versions and target_version > current_version:
                    error_exit(f"version {target_version} not found")
                # Nothing to rollback
                error_exit(f"version {target_version} not found")

        elif count is not None:
            if count <= 0:
                error_exit("count must be positive")
            # Rollback last N migrations (highest versions first)
            sorted_versions = sorted([v for v, _, _ in applied], reverse=True)
            versions_to_rollback = sorted_versions[:count]
        else:
            # Default: rollback last migration
            versions_to_rollback = [current_version]

        # Sort descending (rollback highest version first)
        versions_to_rollback = sorted(versions_to_rollback, reverse=True)

        all_rolled_back = []

        for version in versions_to_rollback:
            # Find migration record
            cursor.execute(
                "SELECT description, operations FROM _migrations WHERE version = ?",
                (version,)
            )
            row = cursor.fetchone()
            if not row:
                error_exit(f"version {version} not found")

            description = row[0]
            operations_json = row[1]

            output_json({
                "event": "rollback_started",
                "version": version,
                "description": description
            })

            # Try to load migration file for explicit rollback_operations
            rollback_ops = None
            # Try common locations for migration files
            for search_dir in [".", "migrations", os.path.dirname(db_file)]:
                for pattern in [f"{version}.json", f"v{version}.json", f"migration_{version}.json"]:
                    candidate = os.path.join(search_dir, pattern)
                    if os.path.exists(candidate):
                        try:
                            with open(candidate, 'r') as f:
                                migration_data = json.load(f)
                            rollback_ops = migration_data.get("rollback_operations")
                            if rollback_ops is not None:
                                break
                        except (json.JSONDecodeError, IOError):
                            pass
                if rollback_ops is not None:
                    break

            # Parse stored operations
            try:
                stored_operations = json.loads(operations_json) if operations_json else []
            except json.JSONDecodeError:
                stored_operations = []

            if rollback_ops is not None:
                # Use explicit rollback operations - execute as forward ops
                for op in reversed(rollback_ops):
                    op_type = op["type"]
                    executor = EXECUTORS.get(op_type)
                    if executor:
                        executor(cursor, op, version)
                    else:
                        error_exit(f"cannot rollback: unknown operation type '{op_type}'")
            else:
                # Check for non-reversible operations
                for op in stored_operations:
                    if op["type"] in NON_REVERSIBLE_OPS:
                        error_exit(f"cannot rollback version {version}: missing rollback_operations for {op['type']}")

                # Execute automatic rollback using reverse executors
                for op in reversed(stored_operations):
                    op_type = op["type"]
                    rollback_fn = ROLLBACK_EXECUTORS.get(op_type)
                    if rollback_fn:
                        rollback_fn(cursor, op, version)
                    else:
                        error_exit(f"cannot rollback: unknown operation type '{op_type}'")

            # Remove migration record
            cursor.execute("DELETE FROM _migrations WHERE version = ?", (version,))

            output_json({
                "event": "rollback_complete",
                "version": version
            })

            all_rolled_back.append(version)

        conn.commit()

        final_version = get_current_version(cursor)
        output_json({
            "event": "rollback_finished",
            "versions_rolled_back": sorted(all_rolled_back),
            "final_version": final_version
        })

    except sqlite3.Error as e:
        conn.rollback()
        err_msg = str(e).lower()
        if "foreign key" in err_msg:
            error_exit("cannot rollback: foreign key constraint violation")
        elif "no such table" in err_msg:
            error_exit(f"rollback failed: {str(e)}")
        else:
            error_exit(f"rollback failed: {str(e)}")
    finally:
        conn.close()


def run_status(db_file: str) -> None:
    """Show migration status."""
    if not os.path.exists(db_file):
        output_json({
            "event": "status",
            "database": db_file,
            "current_version": 0,
            "applied_migrations": []
        })
        return

    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        error_exit(f"error connecting to database: {e}")

    try:
        init_migrations_table(cursor)
        applied = get_applied_migrations(cursor)
        current_version = get_current_version(cursor)

        applied_list = []
        for version, description, _ in applied:
            applied_list.append({
                "version": version,
                "description": description
            })

        output_json({
            "event": "status",
            "database": db_file,
            "current_version": current_version,
            "applied_migrations": applied_list
        })

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Database Migration Tool")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    migrate_parser = subparsers.add_parser("migrate", help="Apply a migration")
    migrate_parser.add_argument("migration_file", help="Path to the migration JSON file")
    migrate_parser.add_argument("database", help="Path to the SQLite database file")

    rollback_parser = subparsers.add_parser("rollback", help="Rollback migrations")
    rollback_parser.add_argument("database", help="Path to the SQLite database file")
    rollback_parser.add_argument("--to-version", type=int, default=None,
                                 help="Rollback to a specific version (exclusive - that version remains applied)")
    rollback_parser.add_argument("--count", type=int, default=None,
                                 help="Rollback the last N migrations")

    status_parser = subparsers.add_parser("status", help="Show migration status")
    status_parser.add_argument("database", help="Path to the SQLite database file")

    args = parser.parse_args()

    if args.command == "migrate":
        run_migration(args.migration_file, args.database)
    elif args.command == "rollback":
        run_rollback(args.database, args.to_version, args.count)
    elif args.command == "status":
        run_status(args.database)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
