#!/usr/bin/env python3
"""Database Migration Tool - Applies schema migrations to SQLite databases."""

import json
import sqlite3
import sys
from pathlib import Path


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class MigrationTool:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: sqlite3.Connection | None = None
        self._ensure_migrations_table()

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self.conn is None:
            # Ensure directory exists
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.db_path)
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def _ensure_migrations_table(self) -> None:
        """Create _migrations table if it doesn't exist."""
        conn = self._get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()

    def is_migration_applied(self, version: int) -> bool:
        """Check if a migration version has already been applied."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT version FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def record_migration(self, version: int, description: str) -> None:
        """Record a migration as applied."""
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )
        conn.commit()

    def get_table_columns(self, table: str) -> list[dict]:
        """Get column information for a table using PRAGMA."""
        conn = self._get_connection()
        cursor = conn.execute(f"PRAGMA table_info({table})")
        return [dict(row) for row in cursor.fetchall()]

    def table_exists(self, table: str) -> bool:
        """Check if a table exists."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table' AND name=?
        """, (table,))
        return cursor.fetchone() is not None

    def column_exists(self, table: str, column: str) -> bool:
        """Check if a column exists in a table."""
        columns = self.get_table_columns(table)
        return any(col["name"] == column for col in columns)

    def _build_column_definition(self, col: dict) -> str:
        """Build SQL column definition from column spec."""
        name = col["name"]
        col_type = col.get("type", "TEXT")

        parts = [name, col_type]

        if col.get("not_null"):
            parts.append("NOT NULL")

        if col.get("primary_key"):
            parts.append("PRIMARY KEY")
            if col.get("auto_increment"):
                if col_type.upper() != "INTEGER":
                    raise MigrationError(
                        f"Column '{name}': auto_increment can only be used with INTEGER type"
                    )
                parts.append("AUTOINCREMENT")

        if col.get("unique"):
            parts.append("UNIQUE")

        if col.get("default") is not None:
            parts.append(f"DEFAULT {col['default']}")

        return " ".join(parts)

    def create_table(self, table: str, columns: list[dict], version: int) -> None:
        """Create a new table."""
        if self.table_exists(table):
            raise MigrationError(f"table '{table}' already exists")

        # Validate columns
        primary_key_count = 0
        for col in columns:
            if col.get("primary_key"):
                primary_key_count += 1
                if col.get("auto_increment") and col.get("type", "").upper() != "INTEGER":
                    raise MigrationError(
                        f"Column '{col['name']}': auto_increment can only be used with INTEGER type"
                    )

        if primary_key_count > 1:
            raise MigrationError("only one column can be a primary key")

        # Build column definitions
        col_defs = [self._build_column_definition(col) for col in columns]
        sql = f"CREATE TABLE {table} ({', '.join(col_defs)})"

        conn = self._get_connection()
        conn.execute(sql)

        print(json.dumps({
            "event": "operation_applied",
            "type": "create_table",
            "table": table,
            "version": version
        }))

    def add_column(self, table: str, column: dict, version: int) -> None:
        """Add a column to an existing table."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        col_name = column["name"]
        if self.column_exists(table, col_name):
            raise MigrationError(f"column '{col_name}' already exists in table '{table}'")

        col_def = self._build_column_definition(column)
        sql = f"ALTER TABLE {table} ADD COLUMN {col_def}"

        conn = self._get_connection()
        conn.execute(sql)

        print(json.dumps({
            "event": "operation_applied",
            "type": "add_column",
            "table": table,
            "column": col_name,
            "version": version
        }))

    def drop_column(self, table: str, column: str, version: int) -> None:
        """Drop a column from a table by recreating the table."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        if not self.column_exists(table, column):
            raise MigrationError(f"column '{column}' does not exist in table '{table}'")

        # Get table info
        columns_info = self.get_table_columns(table)
        if len(columns_info) <= 1:
            raise MigrationError(
                f"cannot drop column '{column}': table '{table}' would have no columns"
            )

        # Check if dropping primary key
        pk_columns = [c for c in columns_info if c["pk"] > 0]
        if any(c["name"] == column for c in pk_columns):
            raise MigrationError(f"cannot drop PRIMARY KEY column '{column}'")

        conn = self._get_connection()

        # Start transaction
        conn.execute("PRAGMA foreign_keys = OFF")

        try:
            # Get all columns except the one to drop
            columns_to_keep = [c for c in columns_info if c["name"] != column]

            # Create temp table name
            temp_table = f"{table}_temp"

            # Build CREATE TABLE for temp table
            col_defs = []
            for c in columns_to_keep:
                col_def = f"{c['name']} {c['type']}"
                if c["notnull"] == 1:
                    col_def += " NOT NULL"
                if c["pk"] > 0:
                    col_def += " PRIMARY KEY"
                # Note: UNIQUE, DEFAULT, etc. would need more complex handling
                col_defs.append(col_def)

            create_sql = f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"
            conn.execute(create_sql)

            # Copy data
            keep_cols = [c["name"] for c in columns_to_keep]
            select_cols = ", ".join(keep_cols)
            insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table}"
            conn.execute(insert_sql)

            # Drop original table
            conn.execute(f"DROP TABLE {table}")

            # Rename temp table
            conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "drop_column",
                "table": table,
                "column": column,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to drop column: {e}")

    def transform_data(self, table: str, transformations: list[dict], version: int) -> None:
        """Transform data in a table using SQL expressions."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        if not transformations:
            raise MigrationError("transform_data operation requires at least one transformation")

        conn = self._get_connection()

        try:
            for trans in transformations:
                column = trans.get("column")
                expression = trans.get("expression")

                if not column:
                    raise MigrationError("transformation missing 'column'")
                if not expression:
                    raise MigrationError("transformation missing 'expression'")

                # Check if column exists or will be created
                if not self.column_exists(table, column):
                    # Try to add the column first (for new columns in transformation)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

                # Update column with expression
                update_sql = f"UPDATE {table} SET {column} = ({expression})"
                conn.execute(update_sql)

            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "transform_data",
                "table": table,
                "transformations_count": len(transformations),
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to transform data: {e}")

    def migrate_column_data(self, table: str, from_column: str, to_column: str, default_value: any, version: int) -> None:
        """Migrate data from one column to another."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        if not from_column:
            raise MigrationError("migrate_column_data operation missing 'from_column'")
        if not to_column:
            raise MigrationError("migrate_column_data operation missing 'to_column'")

        if not self.column_exists(table, from_column):
            raise MigrationError(f"column '{from_column}' does not exist in table '{table}'")
        if not self.column_exists(table, to_column):
            raise MigrationError(f"column '{to_column}' does not exist in table '{table}'")

        conn = self._get_connection()

        try:
            # Build the update statement
            if default_value is not None:
                # Use COALESCE to handle NULL values
                update_sql = f"""
                    UPDATE {table}
                    SET {to_column} = COALESCE({from_column}, ?)
                """
                conn.execute(update_sql, (default_value,))
            else:
                # Simply copy from from_column to to_column
                update_sql = f"UPDATE {table} SET {to_column} = {from_column}"
                conn.execute(update_sql)

            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "migrate_column_data",
                "table": table,
                "from_column": from_column,
                "to_column": to_column,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to migrate column data: {e}")

    def backfill_data(self, table: str, column: str, value: str, where: str | None, version: int) -> None:
        """Backfill a column with computed or constant values."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        if not column:
            raise MigrationError("backfill_data operation missing 'column'")
        if not value:
            raise MigrationError("backfill_data operation missing 'value'")

        if not self.column_exists(table, column):
            raise MigrationError(f"column '{column}' does not exist in table '{table}'")

        conn = self._get_connection()

        try:
            # Build the update statement
            if where:
                update_sql = f"UPDATE {table} SET {column} = {value} WHERE {where}"
            else:
                update_sql = f"UPDATE {table} SET {column} = {value}"

            conn.execute(update_sql)
            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "backfill_data",
                "table": table,
                "column": column,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to backfill data: {e}")

    def apply_migration(self, migration_path: str, version: int) -> None:
        """Apply a migration from a JSON file."""
        conn = self._get_connection()

        # Read and parse migration file
        try:
            path = Path(migration_path)
            if not path.exists():
                raise MigrationError(f"migration file not found: {migration_path}")

            with open(path, 'r') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise MigrationError(f"invalid JSON in migration file: {e}")
        except OSError as e:
            raise MigrationError(f"cannot read migration file: {e}")

        # Validate migration schema
        if "version" not in data:
            raise MigrationError("missing required field 'version'")
        if "description" not in data:
            raise MigrationError("missing required field 'description'")
        if "operations" not in data:
            raise MigrationError("missing required field 'operations'")

        migration_version = data["version"]
        description = data["description"]
        operations = data["operations"]

        if not isinstance(migration_version, int) or migration_version < 1:
            raise MigrationError("version must be a positive integer")

        if not isinstance(operations, list):
            raise MigrationError("operations must be a list")

        # Check if already applied
        if self.is_migration_applied(migration_version):
            print(json.dumps({
                "event": "migration_skipped",
                "version": migration_version,
                "reason": "already_applied"
            }))
            print(f"Warning: Migration version {migration_version} already applied, skipping",
                  file=sys.stderr)
            return

        # Apply operations
        ops_count = 0
        try:
            for op in operations:
                op_type = op.get("type")

                if op_type == "create_table":
                    table = op.get("table")
                    columns = op.get("columns", [])
                    if not table:
                        raise MigrationError("create_table operation missing 'table'")
                    if not columns:
                        raise MigrationError("create_table operation missing 'columns'")
                    self.create_table(table, columns, migration_version)
                    ops_count += 1

                elif op_type == "add_column":
                    table = op.get("table")
                    column = op.get("column")
                    if not table:
                        raise MigrationError("add_column operation missing 'table'")
                    if not column:
                        raise MigrationError("add_column operation missing 'column'")
                    self.add_column(table, column, migration_version)
                    ops_count += 1

                elif op_type == "drop_column":
                    table = op.get("table")
                    column = op.get("column")
                    if not table:
                        raise MigrationError("drop_column operation missing 'table'")
                    if not column:
                        raise MigrationError("drop_column operation missing 'column'")
                    self.drop_column(table, column, migration_version)
                    ops_count += 1

                elif op_type == "transform_data":
                    table = op.get("table")
                    transformations = op.get("transformations", [])
                    if not table:
                        raise MigrationError("transform_data operation missing 'table'")
                    if not transformations:
                        raise MigrationError("transform_data operation missing 'transformations'")
                    self.transform_data(table, transformations, migration_version)
                    ops_count += 1

                elif op_type == "migrate_column_data":
                    table = op.get("table")
                    from_column = op.get("from_column")
                    to_column = op.get("to_column")
                    default_value = op.get("default_value")
                    if not table:
                        raise MigrationError("migrate_column_data operation missing 'table'")
                    if not from_column:
                        raise MigrationError("migrate_column_data operation missing 'from_column'")
                    if not to_column:
                        raise MigrationError("migrate_column_data operation missing 'to_column'")
                    self.migrate_column_data(table, from_column, to_column, default_value, migration_version)
                    ops_count += 1

                elif op_type == "backfill_data":
                    table = op.get("table")
                    column = op.get("column")
                    value = op.get("value")
                    where = op.get("where")
                    if not table:
                        raise MigrationError("backfill_data operation missing 'table'")
                    if not column:
                        raise MigrationError("backfill_data operation missing 'column'")
                    if not value:
                        raise MigrationError("backfill_data operation missing 'value'")
                    self.backfill_data(table, column, value, where, migration_version)
                    ops_count += 1

                else:
                    raise MigrationError(f"unknown operation type: {op_type}")

            # Record migration as complete
            self.record_migration(migration_version, description)
            print(json.dumps({
                "event": "migration_complete",
                "version": migration_version,
                "operations_count": ops_count
            }))

        except MigrationError as e:
            conn.rollback()
            raise
        except Exception as e:
            conn.rollback()
            raise MigrationError(f"SQL error: {e}")


def main():
    usage = "python migration_tool.py migrate <migration_file> <database>"

    if len(sys.argv) < 2:
        print(f"Usage: {usage}", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "migrate":
        if len(sys.argv) < 4:
            print(f"Usage: {usage}", file=sys.stderr)
            sys.exit(1)

        migration_file = sys.argv[2]
        db_file = sys.argv[3]

        try:
            tool = MigrationTool(db_file)
            tool.apply_migration(migration_file, None)
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Error: unknown command '{command}'", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
