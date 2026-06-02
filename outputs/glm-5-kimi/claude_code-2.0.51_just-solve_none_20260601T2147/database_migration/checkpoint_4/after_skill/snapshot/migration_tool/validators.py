"""Migration validation logic."""

import re
import sys
from typing import Any, Dict

VALID_TYPES = {"INTEGER", "TEXT", "REAL", "BLOB", "TIMESTAMP"}
IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')
VALID_FK_ACTIONS = {"CASCADE", "RESTRICT", "SET NULL", "NO ACTION", "SET DEFAULT"}
NON_REVERSIBLE_OPS = {"transform_data", "backfill_data"}


def error_exit(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def warning(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def output_json(event: Dict[str, Any]) -> None:
    print(__import__('json').dumps(event, separators=(',', ':')))


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
