"""Forward and rollback operation executors."""

import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .validators import error_exit, output_json, quote_identifier
from .database import (
    build_column_sql,
    get_column_names,
    get_column_full_info,
    get_foreign_keys,
    get_fk_full_info,
    get_index_full_info,
    get_check_constraint_full_info,
    get_check_constraints,
    get_table_info,
    get_table_sql,
    generate_modified_table_sql,
    rebuild_table_with_modification,
    _build_column_defs_for_rebuild,
    _add_primary_key_constraint,
    _build_fk_defs,
    _build_check_defs,
    table_exists,
    index_exists,
    constraint_exists,
)


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

    table_info = get_table_info(cursor, table_name)
    if not table_info:
        error_exit(f"invalid operation: table '{table_name}' does not exist")

    existing_columns = [row[1] for row in table_info]
    if column_name not in existing_columns:
        error_exit(f"invalid operation: column '{column_name}' does not exist in table '{table_name}'")
    if len(existing_columns) == 1:
        error_exit(f"invalid operation: cannot drop the only column in table '{table_name}'")

    col_by_name = {row[1]: row for row in table_info}
    col_row = col_by_name[column_name]
    if col_row[5] > 0:
        error_exit(f"invalid operation: cannot drop PRIMARY KEY column '{column_name}'")

    original_def = get_column_full_info(cursor, table_name, column_name)
    columns_to_keep = [col for col in existing_columns if col != column_name]

    col_defs, pk_columns = _build_column_defs_for_rebuild([row for row in table_info if row[1] != column_name])
    _add_primary_key_constraint(col_defs, pk_columns)

    existing_fks = get_foreign_keys(cursor, table_name)
    original_sql = get_table_sql(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)
    col_defs.extend(_build_fk_defs(existing_fks, original_sql, None, None))
    col_defs.extend(_build_check_defs(check_constraints, None, None))

    new_table_sql = f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
    rebuild_table_with_modification(cursor, table_name, new_table_sql, columns_to_keep)

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

    if constraint_exists(cursor, table_name, fk_name, r"FOREIGN\s+KEY"):
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
    if not constraint_exists(cursor, table_name, fk_name, r"FOREIGN\s+KEY"):
        error_exit(f"foreign key '{fk_name}' does not exist in table '{table_name}'")

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
    if constraint_exists(cursor, table_name, constraint_name, "CHECK"):
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
    if not constraint_exists(cursor, table_name, constraint_name, "CHECK"):
        error_exit(f"check constraint '{constraint_name}' does not exist in table '{table_name}'")

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
