#!/usr/bin/env python3
"""
Database Migration Tool - Checkpoint 3: Foreign Keys, Indexes, and Constraints

A CLI tool that applies database schema migrations to SQLite databases.
Supports create_table, add_column, drop_column, add_foreign_key, drop_foreign_key,
create_index, drop_index, add_check_constraint, and drop_check_constraint operations.
"""

import json
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any


class MigrationError(Exception):
    """Custom exception for migration-related errors."""
    pass


def validate_identifier(name: str) -> bool:
    """Check if a name is a valid SQLite identifier."""
    if not name:
        return False
    # SQLite identifiers: alphanumeric + underscore, starting with letter/underscore
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def validate_column_definition(col: dict, context: str) -> None:
    """Validate a column definition."""
    if not isinstance(col, dict):
        raise MigrationError(f"invalid column definition in {context}: must be an object")

    if 'name' not in col:
        raise MigrationError(f"invalid column definition in {context}: missing 'name'")

    column_name = col['name']
    if not validate_identifier(column_name):
        raise MigrationError(f"invalid column name '{column_name}' in {context}")

    if 'type' not in col:
        raise MigrationError(f"invalid column definition in {context}: missing 'type'")

    col_type = col['type'].upper()
    valid_types = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}
    if col_type not in valid_types:
        raise MigrationError(f"invalid column type '{col['type']}' for column '{column_name}'")

    # Validate auto_increment constraints
    if col.get('auto_increment', False):
        if col_type != 'INTEGER':
            raise MigrationError(f"column '{column_name}': auto_increment can only be used with INTEGER type")
        if not col.get('primary_key', False):
            raise MigrationError(f"column '{column_name}': auto_increment requires primary_key=true")

    # Validate default is a string or null
    if 'default' in col and col['default'] is not None and not isinstance(col['default'], str):
        raise MigrationError(f"column '{column_name}': default must be a string or null")


def load_migration_file(path: str) -> dict:
    """Load and parse a migration JSON file."""
    file_path = Path(path)
    if not file_path.exists():
        raise MigrationError(f"migration file not found: {path}")

    try:
        with open(file_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")

    if not isinstance(data, dict):
        raise MigrationError("invalid migration schema: root must be an object")

    # Validate required fields
    if 'version' not in data:
        raise MigrationError("invalid migration schema: missing required field 'version'")

    version = data['version']
    if not isinstance(version, int) or version <= 0:
        raise MigrationError("invalid migration schema: version must be a positive integer")

    if 'description' not in data:
        raise MigrationError("invalid migration schema: missing required field 'description'")

    if not isinstance(data['description'], str):
        raise MigrationError("invalid migration schema: 'description' must be a string")

    if 'operations' not in data:
        raise MigrationError("invalid migration schema: missing required field 'operations'")

    if not isinstance(data['operations'], list):
        raise MigrationError("invalid migration schema: 'operations' must be an array")

    return data


def validate_migration(data: dict) -> None:
    """Validate the entire migration structure."""
    for i, op in enumerate(data['operations']):
        op_type = op.get('type')
        if not op_type:
            raise MigrationError(f"operation {i}: missing 'type'")

        if op_type == 'create_table':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: create_table missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'columns' not in op:
                raise MigrationError(f"operation {i}: create_table missing 'columns'")
            if not isinstance(op['columns'], list):
                raise MigrationError(f"operation {i}: 'columns' must be an array")
            if len(op['columns']) == 0:
                raise MigrationError(f"operation {i}: create_table must have at least one column")

            # Check for at most one primary key
            pk_count = sum(1 for col in op['columns'] if col.get('primary_key', False))
            if pk_count > 1:
                raise MigrationError(f"operation {i}: only one column can have primary_key=true")

            for j, col in enumerate(op['columns']):
                validate_column_definition(col, f"operation {i}, column {j}")

        elif op_type == 'add_column':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: add_column missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: add_column missing 'column'")
            validate_column_definition(op['column'], f"operation {i}, column")

        elif op_type == 'drop_column':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: drop_column missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: drop_column missing 'column'")
            column_name = op['column']
            if not validate_identifier(column_name):
                raise MigrationError(f"operation {i}: invalid column name '{column_name}'")

        elif op_type == 'transform_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: transform_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'transformations' not in op:
                raise MigrationError(f"operation {i}: transform_data missing 'transformations'")
            if not isinstance(op['transformations'], list):
                raise MigrationError(f"operation {i}: 'transformations' must be an array")
            if len(op['transformations']) == 0:
                raise MigrationError(f"operation {i}: transform_data must have at least one transformation")

            for j, trans in enumerate(op['transformations']):
                if not isinstance(trans, dict):
                    raise MigrationError(f"operation {i}: transformation {j} must be an object")
                if 'column' not in trans:
                    raise MigrationError(f"operation {i}: transformation {j} missing 'column'")
                column = trans['column']
                if not validate_identifier(column):
                    raise MigrationError(f"operation {i}: transformation {j} invalid column name '{column}'")
                if 'expression' not in trans:
                    raise MigrationError(f"operation {i}: transformation {j} missing 'expression'")
                expression = trans['expression']
                if not isinstance(expression, str):
                    raise MigrationError(f"operation {i}: transformation {j} 'expression' must be a string")

        elif op_type == 'migrate_column_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'from_column' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'from_column'")
            from_column = op['from_column']
            if not validate_identifier(from_column):
                raise MigrationError(f"operation {i}: migrate_column_data invalid 'from_column' name '{from_column}'")

            if 'to_column' not in op:
                raise MigrationError(f"operation {i}: migrate_column_data missing 'to_column'")
            to_column = op['to_column']
            if not validate_identifier(to_column):
                raise MigrationError(f"operation {i}: migrate_column_data invalid 'to_column' name '{to_column}'")

        elif op_type == 'backfill_data':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'column' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'column'")
            column = op['column']
            if not validate_identifier(column):
                raise MigrationError(f"operation {i}: backfill_data invalid column name '{column}'")

            if 'value' not in op:
                raise MigrationError(f"operation {i}: backfill_data missing 'value'")

            if op.get('where') is not None and not isinstance(op['where'], str):
                raise MigrationError(f"operation {i}: backfill_data 'where' must be a string")

        elif op_type == 'add_foreign_key':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: add_foreign_key missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'name' not in op:
                raise MigrationError(f"operation {i}: add_foreign_key missing 'name'")
            fk_name = op['name']
            if not validate_identifier(fk_name):
                raise MigrationError(f"operation {i}: invalid foreign key name '{fk_name}'")

            if 'columns' not in op:
                raise MigrationError(f"operation {i}: add_foreign_key missing 'columns'")
            if not isinstance(op['columns'], list):
                raise MigrationError(f"operation {i}: 'columns' must be an array")
            if len(op['columns']) == 0:
                raise MigrationError(f"operation {i}: add_foreign_key must have at least one column")
            for col in op['columns']:
                if not validate_identifier(col):
                    raise MigrationError(f"operation {i}: invalid column name '{col}' in foreign key")

            if 'references' not in op:
                raise MigrationError(f"operation {i}: add_foreign_key missing 'references'")
            refs = op['references']
            if not isinstance(refs, dict):
                raise MigrationError(f"operation {i}: 'references' must be an object")
            if 'table' not in refs:
                raise MigrationError(f"operation {i}: add_foreign_key 'references' missing 'table'")
            ref_table = refs['table']
            if not validate_identifier(ref_table):
                raise MigrationError(f"operation {i}: invalid referenced table name '{ref_table}'")
            if 'columns' not in refs:
                raise MigrationError(f"operation {i}: add_foreign_key 'references' missing 'columns'")
            if not isinstance(refs['columns'], list):
                raise MigrationError(f"operation {i}: 'references.columns' must be an array")
            if len(refs['columns']) != len(op['columns']):
                raise MigrationError(f"operation {i}: foreign key columns count must match referenced columns count")
            for col in refs['columns']:
                if not validate_identifier(col):
                    raise MigrationError(f"operation {i}: invalid referenced column name '{col}'")

            # Validate on_delete and on_update if present
            valid_actions = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}
            if 'on_delete' in op:
                if op['on_delete'] not in valid_actions:
                    raise MigrationError(f"operation {i}: invalid on_delete value '{op['on_delete']}'")
            if 'on_update' in op:
                if op['on_update'] not in valid_actions:
                    raise MigrationError(f"operation {i}: invalid on_update value '{op['on_update']}'")

        elif op_type == 'drop_foreign_key':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: drop_foreign_key missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'name' not in op:
                raise MigrationError(f"operation {i}: drop_foreign_key missing 'name'")
            fk_name = op['name']
            if not validate_identifier(fk_name):
                raise MigrationError(f"operation {i}: invalid foreign key name '{fk_name}'")

        elif op_type == 'create_index':
            if 'name' not in op:
                raise MigrationError(f"operation {i}: create_index missing 'name'")
            index_name = op['name']
            if not validate_identifier(index_name):
                raise MigrationError(f"operation {i}: invalid index name '{index_name}'")

            if 'table' not in op:
                raise MigrationError(f"operation {i}: create_index missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'columns' not in op:
                raise MigrationError(f"operation {i}: create_index missing 'columns'")
            if not isinstance(op['columns'], list):
                raise MigrationError(f"operation {i}: 'columns' must be an array")
            if len(op['columns']) == 0:
                raise MigrationError(f"operation {i}: create_index must have at least one column")
            for col in op['columns']:
                if not validate_identifier(col):
                    raise MigrationError(f"operation {i}: invalid column name '{col}' in index")

            if 'unique' in op and not isinstance(op['unique'], bool):
                raise MigrationError(f"operation {i}: 'unique' must be a boolean")

        elif op_type == 'drop_index':
            if 'name' not in op:
                raise MigrationError(f"operation {i}: drop_index missing 'name'")
            index_name = op['name']
            if not validate_identifier(index_name):
                raise MigrationError(f"operation {i}: invalid index name '{index_name}'")

        elif op_type == 'add_check_constraint':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: add_check_constraint missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'name' not in op:
                raise MigrationError(f"operation {i}: add_check_constraint missing 'name'")
            constraint_name = op['name']
            if not validate_identifier(constraint_name):
                raise MigrationError(f"operation {i}: invalid constraint name '{constraint_name}'")

            if 'expression' not in op:
                raise MigrationError(f"operation {i}: add_check_constraint missing 'expression'")
            expression = op['expression']
            if not isinstance(expression, str):
                raise MigrationError(f"operation {i}: 'expression' must be a string")

        elif op_type == 'drop_check_constraint':
            if 'table' not in op:
                raise MigrationError(f"operation {i}: drop_check_constraint missing 'table'")
            table_name = op['table']
            if not validate_identifier(table_name):
                raise MigrationError(f"operation {i}: invalid table name '{table_name}'")

            if 'name' not in op:
                raise MigrationError(f"operation {i}: drop_check_constraint missing 'name'")
            constraint_name = op['name']
            if not validate_identifier(constraint_name):
                raise MigrationError(f"operation {i}: invalid constraint name '{constraint_name}'")

        else:
            raise MigrationError(f"operation {i}: unknown operation type '{op_type}'")


def get_table_info(cursor: sqlite3.Cursor, table_name: str) -> list:
    """Get information about table columns."""
    cursor.execute(f"PRAGMA table_info({table_name})")
    return cursor.fetchall()


def get_primary_key_column(cursor: sqlite3.Cursor, table_name: str) -> str | None:
    """Get the name of the primary key column, if any."""
    info = get_table_info(cursor, table_name)
    for col in info:
        # col[5] is the pk flag (1 for primary key)
        if col[5] == 1:
            return col[1]  # col[1] is the column name
    return None


def get_foreign_keys(cursor: sqlite3.Cursor, table_name: str) -> list:
    """Get foreign key information for a table."""
    cursor.execute("PRAGMA foreign_key_list(?)", (table_name,))
    return cursor.fetchall()


def get_indexes(cursor: sqlite3.Cursor, table_name: str | None = None) -> list:
    """Get index information for the database or a specific table."""
    if table_name:
        cursor.execute("""
            SELECT name, tbl_name, sql FROM sqlite_master
            WHERE type='index' AND tbl_name=? AND name NOT LIKE 'sqlite_%'
        """, (table_name,))
    else:
        cursor.execute("""
            SELECT name, tbl_name, sql FROM sqlite_master
            WHERE type='index' AND name NOT LIKE 'sqlite_%'
        """)
    return cursor.fetchall()


def get_table_sql(cursor: sqlite3.Cursor, table_name: str) -> str:
    """Get the CREATE TABLE SQL statement for a table."""
    cursor.execute("""
        SELECT sql FROM sqlite_master
        WHERE type='table' AND name=?
    """, (table_name,))
    result = cursor.fetchone()
    return result[0] if result else None


def get_check_constraints(cursor: sqlite3.Cursor, table_name: str) -> list:
    """Get check constraints for a table by parsing the CREATE TABLE statement."""
    sql = get_table_sql(cursor, table_name)
    if not sql:
        return []

    # Extract check constraints from the SQL
    constraints = []
    # Look for CHECK(...) in the SQL
    import re
    # Match CHECK constraints (case-insensitive)
    pattern = r'CHECK\s*\(([^)]+)\)'
    matches = re.findall(pattern, sql, re.IGNORECASE)
    for match in matches:
        constraints.append(match)

    return constraints


def parse_table_definition(sql: str) -> dict:
    """Parse a CREATE TABLE statement to extract column definitions and constraints."""
    # Extract the part between parentheses (column definitions)
    import re
    match = re.search(r'\((.*)\)', sql, re.DOTALL)
    if not match:
        return {'columns': [], 'constraints': []}

    body = match.group(1)

    # Split by commas, being careful about nested parentheses
    parts = []
    current = ""
    depth = 0
    for char in body:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())

    columns = []
    constraints = []

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Check if it's a constraint (starts with CONSTRAINT or CHECK or FOREIGN KEY)
        if part.upper().startswith('CONSTRAINT ') or part.upper().startswith('CHECK(') or part.upper().startswith('FOREIGN'):
            constraints.append(part)
        else:
            columns.append(part)

    return {'columns': columns, 'constraints': constraints}


def column_definition_to_sql(col: dict) -> str:
    """Convert a column definition dict to SQL column definition."""
    parts = []
    parts.append(f"{col['name']} {col['type'].upper()}")

    if col.get('not_null', False):
        parts.append("NOT NULL")

    if col.get('primary_key', False):
        parts.append("PRIMARY KEY")
        if col.get('auto_increment', False):
            parts.append("AUTOINCREMENT")

    if col.get('unique', False):
        parts.append("UNIQUE")

    if 'default' in col and col['default'] is not None:
        parts.append(f"DEFAULT {col['default']}")

    return " ".join(parts)


def apply_create_table(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a create_table operation."""
    table_name = op['table']

    # Check if table already exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if cursor.fetchone():
        raise MigrationError(f"table '{table_name}' already exists")

    # Build column definitions
    columns_sql = [column_definition_to_sql(col) for col in op['columns']]
    sql = f"CREATE TABLE {table_name} ({', '.join(columns_sql)})"

    cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "create_table",
        "table": table_name,
        "version": version
    }


def apply_add_column(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply an add_column operation."""
    table_name = op['table']
    column = op['column']
    column_name = column['name']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if column already exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column_name in existing_columns:
        raise MigrationError(f"column '{column_name}' already exists in table '{table_name}'")

    # Build ALTER TABLE statement
    column_sql = column_definition_to_sql(column)
    sql = f"ALTER TABLE {table_name} ADD COLUMN {column_sql}"

    cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "add_column",
        "table": table_name,
        "column": column_name,
        "version": version
    }


def apply_drop_column(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a drop_column operation."""
    table_name = op['table']
    column_name = op['column']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if column exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column_name not in existing_columns:
        raise MigrationError(f"column '{column_name}' does not exist in table '{table_name}'")

    # Cannot drop if it's the only column
    if len(existing_columns) == 1:
        raise MigrationError(f"cannot drop column '{column_name}': it is the only column in table '{table_name}'")

    # Cannot drop PRIMARY KEY column
    pk_column = get_primary_key_column(cursor, table_name)
    if pk_column == column_name:
        raise MigrationError(f"cannot drop column '{column_name}': it is a PRIMARY KEY column")

    # Get columns to keep (excluding the one to drop)
    columns_to_keep = [col for col in existing_columns if col != column_name]

    # Create temp table name
    temp_table_name = f"{table_name}_temp"

    # Build the new column definitions from the old table info, excluding the dropped column
    # We need to preserve all column properties from the original table
    new_columns_sql = []
    for col in table_info:
        col_name = col[1]
        if col_name == column_name:
            continue

        col_type = col[2]  # type
        not_null = col[3] == 1  # notnull
        default_value = col[4]  # default_value
        pk = col[5]  # pk flag

        col_def = {"name": col_name, "type": col_type}
        if not_null:
            col_def["not_null"] = True
        if pk:
            col_def["primary_key"] = True
        if default_value is not None:
            # SQLite stores default values as strings, need to handle quotes
            col_def["default"] = repr(default_value)

        new_columns_sql.append(column_definition_to_sql(col_def))

    # Create new table without the dropped column
    create_new_table_sql = f"CREATE TABLE {temp_table_name} ({', '.join(new_columns_sql)})"
    cursor.execute(create_new_table_sql)

    # Copy data from old table to new table
    select_cols = ", ".join(columns_to_keep)
    insert_sql = f"INSERT INTO {temp_table_name} SELECT {select_cols} FROM {table_name}"
    cursor.execute(insert_sql)

    # Drop old table
    cursor.execute(f"DROP TABLE {table_name}")

    # Rename new table to original name
    cursor.execute(f"ALTER TABLE {temp_table_name} RENAME TO {table_name}")

    return {
        "event": "operation_applied",
        "type": "drop_column",
        "table": table_name,
        "column": column_name,
        "version": version
    }


def apply_transform_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a transform_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Get existing columns
    table_info = get_table_info(cursor, table_name)
    existing_columns = {col[1]: col for col in table_info}

    # Get primary key for the table
    pk_column = get_primary_key_column(cursor, table_name)
    if pk_column is None:
        raise MigrationError(f"table '{table_name}' has no primary key, required for transform_data")

    transformations = op.get('transformations', [])
    if not isinstance(transformations, list):
        raise MigrationError(f"operation: transform_data 'transformations' must be an array")

    for i, trans in enumerate(transformations):
        if not isinstance(trans, dict):
            raise MigrationError(f"operation: transformation {i} must be an object")

        column = trans.get('column')
        if not column:
            raise MigrationError(f"operation: transformation {i} missing 'column'")
        if not validate_identifier(column):
            raise MigrationError(f"operation: transformation {i} invalid column name '{column}'")

        expression = trans.get('expression')
        if not expression:
            raise MigrationError(f"operation: transformation {i} missing 'expression'")
        if not isinstance(expression, str):
            raise MigrationError(f"operation: transformation {i} 'expression' must be a string")

        # Check if column exists
        if column not in existing_columns:
            raise MigrationError(f"column '{column}' does not exist in table '{table_name}'")

        # Build and execute UPDATE statement
        sql = f"UPDATE {table_name} SET {column} = ({expression})"
        cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "transform_data",
        "table": table_name,
        "transformations": len(transformations),
        "version": version
    }


def apply_migrate_column_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a migrate_column_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    from_column = op.get('from_column')
    to_column = op.get('to_column')

    if not from_column:
        raise MigrationError("operation: migrate_column_data missing 'from_column'")
    if not to_column:
        raise MigrationError("operation: migrate_column_data missing 'to_column'")

    if not validate_identifier(from_column):
        raise MigrationError(f"operation: migrate_column_data invalid 'from_column' name '{from_column}'")
    if not validate_identifier(to_column):
        raise MigrationError(f"operation: migrate_column_data invalid 'to_column' name '{to_column}'")

    # Get existing columns
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]

    if from_column not in existing_columns:
        raise MigrationError(f"column '{from_column}' does not exist in table '{table_name}'")
    if to_column not in existing_columns:
        raise MigrationError(f"column '{to_column}' does not exist in table '{table_name}'")

    default_value = op.get('default_value')

    # Build SET expression
    if default_value is not None:
        # Use COALESCE to set to default when from_column is NULL
        sql = f"UPDATE {table_name} SET {to_column} = COALESCE({from_column}, ?)"
        cursor.execute(sql, (default_value,))
    else:
        sql = f"UPDATE {table_name} SET {to_column} = {from_column}"
        cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "migrate_column_data",
        "table": table_name,
        "from_column": from_column,
        "to_column": to_column,
        "version": version
    }


def apply_backfill_data(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a backfill_data operation."""
    table_name = op['table']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    column = op.get('column')
    if not column:
        raise MigrationError("operation: backfill_data missing 'column'")
    if not validate_identifier(column):
        raise MigrationError(f"operation: backfill_data invalid column name '{column}'")

    # Check if column exists
    table_info = get_table_info(cursor, table_name)
    existing_columns = [col[1] for col in table_info]
    if column not in existing_columns:
        raise MigrationError(f"column '{column}' does not exist in table '{table_name}'")

    value = op.get('value')
    if value is None:
        raise MigrationError("operation: backfill_data missing 'value'")

    where = op.get('where')

    # Build SET clause
    if isinstance(value, str):
        # Treat as SQL expression
        set_clause = f"{column} = {value}"
    else:
        # Treat as constant - use parameter
        set_clause = f"{column} = ?"

    # Build SQL
    if where:
        if not isinstance(where, str):
            raise MigrationError("operation: backfill_data 'where' must be a string")
        sql = f"UPDATE {table_name} SET {set_clause} WHERE {where}"
        if not isinstance(value, str):
            cursor.execute(sql, (value,))
        else:
            cursor.execute(sql)
    else:
        sql = f"UPDATE {table_name} SET {set_clause}"
        if not isinstance(value, str):
            cursor.execute(sql, (value,))
        else:
            cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "backfill_data",
        "table": table_name,
        "column": column,
        "version": version
    }


def initialize_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Create the _migrations table if it doesn't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def record_migration(cursor: sqlite3.Cursor, version: int, description: str) -> None:
    """Record that a migration has been applied."""
    cursor.execute(
        "INSERT INTO _migrations (version, description) VALUES (?, ?)",
        (version, description)
    )


def apply_migration(db_path: str, migration_data: dict) -> int:
    """
    Apply a migration to the database.
    Returns 0 for success, 1 for skip, 2 for error.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Initialize migrations table
        initialize_migrations_table(cursor)

        version = migration_data['version']
        description = migration_data['description']

        # Check if already applied
        if is_migration_applied(cursor, version):
            print(json.dumps({
                "event": "migration_skipped",
                "version": version,
                "reason": "already_applied"
            }))
            sys.stderr.write(f"Warning: Migration version {version} already applied, skipping\n")
            conn.close()
            return 0

        # Validate the migration
        validate_migration(migration_data)

        # Apply operations
        applied_count = 0
        for op in migration_data['operations']:
            op_type = op['type']

            if op_type == 'create_table':
                result = apply_create_table(cursor, op, version)
            elif op_type == 'add_column':
                result = apply_add_column(cursor, op, version)
            elif op_type == 'drop_column':
                result = apply_drop_column(cursor, op, version)
            elif op_type == 'transform_data':
                result = apply_transform_data(cursor, op, version)
            elif op_type == 'migrate_column_data':
                result = apply_migrate_column_data(cursor, op, version)
            elif op_type == 'backfill_data':
                result = apply_backfill_data(cursor, op, version)
            elif op_type == 'add_foreign_key':
                result = apply_add_foreign_key(cursor, op, version)
            elif op_type == 'drop_foreign_key':
                result = apply_drop_foreign_key(cursor, op, version)
            elif op_type == 'create_index':
                result = apply_create_index(cursor, op, version)
            elif op_type == 'drop_index':
                result = apply_drop_index(cursor, op, version)
            elif op_type == 'add_check_constraint':
                result = apply_add_check_constraint(cursor, op, version)
            elif op_type == 'drop_check_constraint':
                result = apply_drop_check_constraint(cursor, op, version)
            else:
                # Should not happen due to validation
                raise MigrationError(f"unknown operation type: {op_type}")

            print(json.dumps(result))
            applied_count += 1

        # Record the migration
        record_migration(cursor, version, description)

        # Output completion event
        print(json.dumps({
            "event": "migration_complete",
            "version": version,
            "operations_count": applied_count
        }))

        conn.commit()
        conn.close()
        return 0

    except MigrationError as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: {e}\n")
        return 2
    except sqlite3.Error as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: SQL error: {e}\n")
        return 2
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        sys.stderr.write(f"Error: {e}\n")
        return 2


def apply_add_foreign_key(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply an add_foreign_key operation."""
    table_name = op['table']
    fk_name = op['name']
    columns = op['columns']
    refs = op['references']
    ref_table = refs['table']
    ref_columns = refs['columns']
    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if referenced table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (ref_table,))
    if not cursor.fetchone():
        raise MigrationError(f"referenced table '{ref_table}' does not exist")

    # Check if columns exist in the table
    table_info = get_table_info(cursor, table_name)
    existing_columns = {col[1]: col for col in table_info}
    for col in columns:
        if col not in existing_columns:
            raise MigrationError(f"cannot add foreign key: column '{col}' does not exist in table '{table_name}'")

    # Check if referenced columns exist
    ref_table_info = get_table_info(cursor, ref_table)
    ref_existing_columns = {col[1]: col for col in ref_table_info}
    for col in ref_columns:
        if col not in ref_existing_columns:
            raise MigrationError(f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist")

    # Check if foreign key already exists
    existing_fks = get_foreign_keys(cursor, table_name)
    for fk in existing_fks:
        # fk[0] is the ID, fk[1] is the sequence, fk[2] is the foreign table name
        # We check for the referenced table name
        if fk[2] == ref_table:
            # Check if the columns match
            existing_columns_in_fk = [row[3] for row in existing_fks if row[0] == fk[0]]
            if existing_columns_in_fk == columns:
                raise MigrationError(f"foreign key constraint '{fk_name}' already exists")

    # Enable foreign key enforcement for validation
    cursor.execute("PRAGMA foreign_keys = ON")

    # Verify that all referenced data exists (if table has data)
    # This is done by attempting to insert or checking manually
    # Actually, SQLite will check this when we add the constraint if data exists

    # Build the ALTER TABLE statement
    cols_str = ', '.join(columns)
    ref_cols_str = ', '.join(ref_columns)
    sql = f"ALTER TABLE {table_name} ADD CONSTRAINT {fk_name} FOREIGN KEY ({cols_str}) REFERENCES {ref_table} ({ref_cols_str}) ON DELETE {on_delete} ON UPDATE {on_update}"

    try:
        cursor.execute(sql)
    except sqlite3.Error as e:
        # Check if it's a foreign key violation
        error_msg = str(e).lower()
        if 'foreign key' in error_msg or 'references' in error_msg:
            raise MigrationError(f"foreign key violation: referenced row in '{ref_table}' table does not exist")
        raise

    return {
        "event": "operation_applied",
        "type": "add_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    }


def apply_drop_foreign_key(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a drop_foreign_key operation."""
    table_name = op['table']
    fk_name = op['name']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Get current foreign keys
    existing_fks = get_foreign_keys(cursor, table_name)
    if not existing_fks:
        raise MigrationError(f"no foreign key constraints found in table '{table_name}'")

    # Find the foreign key by name
    fk_to_drop = None
    for fk in existing_fks:
        # SQLite doesn't store constraint names in PRAGMA foreign_key_list
        # We need to parse the CREATE TABLE statement to find the constraint
        pass

    # Since SQLite doesn't let us easily get the constraint name from PRAGMA,
    # we need to parse the table's SQL to find it
    table_sql = get_table_sql(cursor, table_name)
    if not table_sql:
        raise MigrationError(f"could not get SQL definition for table '{table_name}'")

    # Parse the table definition to find the constraint
    import re
    # Look for CONSTRAINT name in the SQL
    pattern = rf'CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY'
    if not re.search(pattern, table_sql, re.IGNORECASE):
        raise MigrationError(f"foreign key constraint '{fk_name}' does not exist in table '{table_name}'")

    # SQLite doesn't support DROP CONSTRAINT directly, so we need to recreate the table
    # without this foreign key constraint

    # Get table info for all columns
    table_info = get_table_info(cursor, table_name)
    columns = [col[1] for col in table_info]

    # Build the new table SQL without the foreign key constraint
    # Get the full CREATE TABLE statement and remove the specific constraint

    # Create a temporary table name
    temp_table = f"{table_name}_temp_{version}"

    # Reconstruct the CREATE TABLE statement without the specific constraint
    # Parse the original SQL and rebuild it
    # This is complex, so let's take a simpler approach:
    # Create a new table with the same columns, then copy data

    # Get the existing column definitions
    new_columns_sql = []
    for col in table_info:
        col_name = col[1]
        col_type = col[2]
        not_null = col[3] == 1
        default_value = col[4]
        pk = col[5] == 1

        col_def = {
            'name': col_name,
            'type': col_type,
            'not_null': not_null,
            'primary_key': pk == 1
        }
        if default_value is not None:
            col_def['default'] = repr(default_value)

        new_columns_sql.append(column_definition_to_sql(col_def))

    # Create temporary table
    create_temp_sql = f"CREATE TABLE {temp_table} ({', '.join(new_columns_sql)})"
    cursor.execute(create_temp_sql)

    # Copy data
    select_cols = ', '.join(columns)
    insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table_name}"
    cursor.execute(insert_sql)

    # Drop original table
    cursor.execute(f"DROP TABLE {table_name}")

    # Rename temporary table to original name
    cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table_name}")

    return {
        "event": "operation_applied",
        "type": "drop_foreign_key",
        "table": table_name,
        "name": fk_name,
        "version": version
    }


def apply_create_index(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a create_index operation."""
    index_name = op['name']
    table_name = op['table']
    columns = op['columns']
    unique = op.get('unique', False)

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # Check if index already exists
    existing_indexes = get_indexes()
    for idx in existing_indexes:
        if idx[0] == index_name:
            raise MigrationError(f"index '{index_name}' already exists")

    # Check if columns exist
    table_info = get_table_info(cursor, table_name)
    existing_columns = {col[1]: col for col in table_info}
    for col in columns:
        if col not in existing_columns:
            raise MigrationError(f"column '{col}' does not exist in table '{table_name}'")

    # Build CREATE INDEX statement
    cols_str = ', '.join(columns)
    unique_str = "UNIQUE" if unique else ""
    sql = f"CREATE {unique_str} INDEX {index_name} ON {table_name} ({cols_str})"

    cursor.execute(sql)

    return {
        "event": "operation_applied",
        "type": "create_index",
        "name": index_name,
        "table": table_name,
        "version": version
    }


def apply_drop_index(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a drop_index operation."""
    index_name = op['name']

    # Check if index exists
    existing_indexes = get_indexes()
    idx_to_drop = None
    for idx in existing_indexes:
        if idx[0] == index_name:
            idx_to_drop = idx
            break

    if not idx_to_drop:
        raise MigrationError(f"index '{index_name}' does not exist")

    # Drop the index
    cursor.execute(f"DROP INDEX {index_name}")

    return {
        "event": "operation_applied",
        "type": "drop_index",
        "name": index_name,
        "version": version
    }


def apply_add_check_constraint(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply an add_check_constraint operation."""
    table_name = op['table']
    constraint_name = op['name']
    expression = op['expression']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we need to recreate the table
    table_info = get_table_info(cursor, table_name)
    columns = [col[1] for col in table_info]

    # Get the existing table SQL to extract existing constraints
    table_sql = get_table_sql(cursor, table_name)

    # Create temporary table
    temp_table = f"{table_name}_temp_{version}"

    # Reconstruct the column definitions
    new_columns_sql = []
    for col in table_info:
        col_name = col[1]
        col_type = col[2]
        not_null = col[3] == 1
        default_value = col[4]
        pk = col[5] == 1

        col_def = {
            'name': col_name,
            'type': col_type,
            'not_null': not_null,
            'primary_key': pk == 1
        }
        if default_value is not None:
            col_def['default'] = repr(default_value)

        new_columns_sql.append(column_definition_to_sql(col_def))

    # Add the new check constraint
    check_constraint = f"CONSTRAINT {constraint_name} CHECK ({expression})"

    # Create temporary table with the new constraint
    create_temp_sql = f"CREATE TABLE {temp_table} ({', '.join(new_columns_sql)}, {check_constraint})"
    cursor.execute(create_temp_sql)

    # Copy data
    select_cols = ', '.join(columns)
    insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table_name}"

    # Check for constraint violations
    try:
        cursor.execute(insert_sql)
    except sqlite3.Error as e:
        # Drop temp table on error
        cursor.execute(f"DROP TABLE {temp_table}")
        error_msg = str(e).lower()
        if 'check constraint' in error_msg or 'constraint' in error_msg:
            raise MigrationError(f"check constraint violation: data violates constraint '{constraint_name}'")
        raise

    # Drop original table
    cursor.execute(f"DROP TABLE {table_name}")

    # Rename temporary table to original name
    cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table_name}")

    return {
        "event": "operation_applied",
        "type": "add_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    }


def apply_drop_check_constraint(cursor: sqlite3.Cursor, op: dict, version: int) -> dict:
    """Apply a drop_check_constraint operation."""
    table_name = op['table']
    constraint_name = op['name']

    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    if not cursor.fetchone():
        raise MigrationError(f"table '{table_name}' does not exist")

    table_info = get_table_info(cursor, table_name)
    columns = [col[1] for col in table_info]

    # Get the table SQL to verify constraint exists
    table_sql = get_table_sql(cursor, table_name)

    import re
    # Check if constraint exists
    pattern = rf'CONSTRAINT\s+{re.escape(constraint_name)}\s+CHECK'
    if not re.search(pattern, table_sql, re.IGNORECASE):
        raise MigrationError(f"check constraint '{constraint_name}' does not exist in table '{table_name}'")

    # SQLite doesn't support dropping constraints, so we need to recreate the table
    temp_table = f"{table_name}_temp_{version}"

    # Reconstruct column definitions without the constraint
    new_columns_sql = []
    for col in table_info:
        col_name = col[1]
        col_type = col[2]
        not_null = col[3] == 1
        default_value = col[4]
        pk = col[5] == 1

        col_def = {
            'name': col_name,
            'type': col_type,
            'not_null': not_null,
            'primary_key': pk == 1
        }
        if default_value is not None:
            col_def['default'] = repr(default_value)

        new_columns_sql.append(column_definition_to_sql(col_def))

    # Create temporary table without the constraint
    create_temp_sql = f"CREATE TABLE {temp_table} ({', '.join(new_columns_sql)})"
    cursor.execute(create_temp_sql)

    # Copy data
    select_cols = ', '.join(columns)
    insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table_name}"
    cursor.execute(insert_sql)

    # Drop original table
    cursor.execute(f"DROP TABLE {table_name}")

    # Rename temporary table to original name
    cursor.execute(f"ALTER TABLE {temp_table} RENAME TO {table_name}")

    return {
        "event": "operation_applied",
        "type": "drop_check_constraint",
        "table": table_name,
        "name": constraint_name,
        "version": version
    }


def main():
    """Main entry point."""
    if len(sys.argv) < 4:
        sys.stderr.write("Usage: python migration_tool.py migrate <migration.json> <database.db>\n")
        sys.exit(1)

    command = sys.argv[1]

    if command != 'migrate':
        sys.stderr.write(f"Error: unknown command '{command}'\n")
        sys.stderr.write("Usage: python migration_tool.py migrate <migration.json> <database.db>\n")
        sys.exit(1)

    migration_file = sys.argv[2]
    database_file = sys.argv[3]

    # Load migration
    try:
        migration_data = load_migration_file(migration_file)
    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    # Apply migration
    result = apply_migration(database_file, migration_data)
    sys.exit(result)


if __name__ == '__main__':
    main()
