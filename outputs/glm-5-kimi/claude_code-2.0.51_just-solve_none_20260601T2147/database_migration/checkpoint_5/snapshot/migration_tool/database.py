"""Database introspection and table rebuilding utilities."""

import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from .validators import quote_identifier, error_exit


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


def get_table_info(cursor: sqlite3.Cursor, table_name: str) -> List[Tuple]:
    cursor.execute(f"PRAGMA table_info({quote_identifier(table_name)})")
    return cursor.fetchall()


def get_column_names(cursor: sqlite3.Cursor, table_name: str) -> List[str]:
    return [row[1] for row in get_table_info(cursor, table_name)]


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


def constraint_exists(cursor: sqlite3.Cursor, table_name: str, constraint_name: str, kind: str) -> bool:
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return False
    pattern = rf'CONSTRAINT\s+["\']?{re.escape(constraint_name)}["\']?\s+{kind}'
    return bool(re.search(pattern, sql, re.IGNORECASE))


def get_column_full_info(cursor: sqlite3.Cursor, table_name: str, column_name: str) -> Optional[Dict[str, Any]]:
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
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return None

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
    cursor.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,)
    )
    row = cursor.fetchone()
    if not row:
        return None

    idx_name, tbl_name, sql = row
    unique = bool(sql and "UNIQUE" in sql.upper().split("INDEX")[0])

    cursor.execute(f"PRAGMA index_info({quote_identifier(index_name)})")
    cols = [r[2] for r in cursor.fetchall()]

    return {
        "name": idx_name,
        "table": tbl_name,
        "columns": cols,
        "unique": unique,
    }


def get_check_constraint_full_info(cursor: sqlite3.Cursor, table_name: str, constraint_name: str) -> Optional[Dict[str, Any]]:
    constraints = get_check_constraints(cursor, table_name)
    for chk in constraints:
        if chk["name"] == constraint_name:
            return {
                "name": constraint_name,
                "table": table_name,
                "expression": chk["expression"],
            }
    return None


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


def _build_column_defs_for_rebuild(table_info: List[Tuple]) -> Tuple[List[str], List[Tuple]]:
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
    return col_defs, pk_columns


def _add_primary_key_constraint(col_defs: List[str], pk_columns: List[Tuple]) -> None:
    if not pk_columns:
        return
    pk_sorted = sorted(pk_columns, key=lambda x: x[0])
    if len(pk_sorted) == 1:
        for i, col_def in enumerate(col_defs):
            if quote_identifier(pk_sorted[0][1]) in col_def:
                col_defs[i] = col_def + " PRIMARY KEY"
                break
    else:
        pk_names = [quote_identifier(c[1]) for c in pk_sorted]
        col_defs.append(f"PRIMARY KEY ({', '.join(pk_names)})")


def _build_fk_defs(existing_fks: List[Dict], original_sql: Optional[str], remove_fk: Optional[str], add_fk: Optional[Dict]) -> List[str]:
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

    defs = []
    for fk_data in fk_groups.values():
        fk_def = f"FOREIGN KEY ({', '.join(quote_identifier(c) for c in fk_data['columns'])}) REFERENCES {quote_identifier(fk_data['ref_table'])} ({', '.join(quote_identifier(c) for c in fk_data['ref_columns'])})"
        if fk_data["on_delete"]:
            fk_def += f" ON DELETE {fk_data['on_delete']}"
        if fk_data["on_update"]:
            fk_def += f" ON UPDATE {fk_data['on_update']}"
        defs.append(fk_def)

    if add_fk:
        fk_def = f"CONSTRAINT {quote_identifier(add_fk['name'])} FOREIGN KEY ({', '.join(quote_identifier(c) for c in add_fk['columns'])}) REFERENCES {quote_identifier(add_fk['ref_table'])} ({', '.join(quote_identifier(c) for c in add_fk['ref_columns'])})"
        if add_fk.get("on_delete"):
            fk_def += f" ON DELETE {add_fk['on_delete'].upper()}"
        if add_fk.get("on_update"):
            fk_def += f" ON UPDATE {add_fk['on_update'].upper()}"
        defs.append(fk_def)

    return defs


def _build_check_defs(check_constraints: List[Dict], remove_check: Optional[str], add_check: Optional[Dict]) -> List[str]:
    defs = []
    for chk in check_constraints:
        if chk["name"] == remove_check:
            continue
        if chk["name"]:
            defs.append(f"CONSTRAINT {quote_identifier(chk['name'])} CHECK ({chk['expression']})")
        else:
            defs.append(f"CHECK ({chk['expression']})")
    if add_check:
        defs.append(f"CONSTRAINT {quote_identifier(add_check['name'])} CHECK ({add_check['expression']})")
    return defs


def generate_modified_table_sql(
    cursor: sqlite3.Cursor,
    table_name: str,
    add_fk: Optional[Dict[str, Any]] = None,
    remove_fk: Optional[str] = None,
    add_check: Optional[Dict[str, Any]] = None,
    remove_check: Optional[str] = None,
) -> str:
    table_info = get_table_info(cursor, table_name)
    existing_fks = get_foreign_keys(cursor, table_name)
    check_constraints = get_check_constraints(cursor, table_name)
    original_sql = get_table_sql(cursor, table_name)

    col_defs, pk_columns = _build_column_defs_for_rebuild(table_info)
    _add_primary_key_constraint(col_defs, pk_columns)
    col_defs.extend(_build_fk_defs(existing_fks, original_sql, remove_fk, add_fk))
    col_defs.extend(_build_check_defs(check_constraints, remove_check, add_check))

    return f"CREATE TABLE {quote_identifier(f'_migration_temp_{table_name}')} ({', '.join(col_defs)})"
