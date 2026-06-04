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
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operations TEXT NOT NULL DEFAULT '[]'
            );
        """)
        # Add operations column if it doesn't exist (for backward compatibility)
        try:
            conn.execute("ALTER TABLE _migrations ADD COLUMN operations TEXT DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()

    def is_migration_applied(self, version: int) -> bool:
        """Check if a migration version has already been applied."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT version FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def record_migration(self, version: int, description: str, operations: list | None = None) -> None:
        """Record a migration as applied with operation details."""
        conn = self._get_connection()
        ops_json = json.dumps(operations) if operations is not None else "[]"
        conn.execute(
            "INSERT INTO _migrations (version, description, operations) VALUES (?, ?, ?)",
            (version, description, ops_json)
        )
        conn.commit()

    def get_migrations(self) -> list[dict]:
        """Get all applied migrations ordered by version."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT version, description, operations, applied_at FROM _migrations ORDER BY version ASC"
        )
        migrations = []
        for row in cursor.fetchall():
            migrations.append({
                "version": row["version"],
                "description": row["description"],
                "operations": json.loads(row["operations"]),
                "applied_at": row["applied_at"]
            })
        return migrations

    def get_latest_migration_version(self) -> int | None:
        """Get the highest applied migration version."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT MAX(version) FROM _migrations"
        )
        result = cursor.fetchone()[0]
        return result

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

    def add_foreign_key(self, table: str, name: str, columns: list[str], references: dict, on_delete: str | None, on_update: str | None, version: int) -> None:
        """Add a foreign key constraint to a table."""
        ref_table = references.get("table")
        ref_columns = references.get("columns", [])

        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")
        if not self.table_exists(ref_table):
            raise MigrationError(f"table '{ref_table}' does not exist")
        if len(columns) != len(ref_columns):
            raise MigrationError(f"number of referencing columns must match referenced columns")

        # Validate columns exist
        for col in columns:
            if not self.column_exists(table, col):
                raise MigrationError(f"column '{col}' does not exist in table '{table}'")
        for col in ref_columns:
            if not self.column_exists(ref_table, col):
                raise MigrationError(f"referenced column '{col}' does not exist in table '{ref_table}'")

        # Validate on_delete and on_update values
        valid_actions = {"CASCADE", "RESTRICT", "SET NULL", "NO ACTION", "SET DEFAULT"}
        if on_delete and on_delete.upper() not in valid_actions:
            raise MigrationError(f"invalid on_delete action: {on_delete}")
        if on_update and on_update.upper() not in valid_actions:
            raise MigrationError(f"invalid on_update action: {on_update}")

        # Check for duplicate foreign key name
        existing_fks = self.get_foreign_keys(table)
        for fk in existing_fks:
            if fk["table"] == ref_table and len(fk) > 0:
                # In SQLite, foreign keys don't have explicit names, but we track them
                pass  # We'll use a simple naming approach

        conn = self._get_connection()

        # Check for existing data that violates the foreign key if tables have data
        cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
        row_count = cursor.fetchone()[0]
        cursor = conn.execute(f"SELECT COUNT(*) FROM {ref_table}")
        ref_count = cursor.fetchone()[0]

        if row_count > 0:
            # Check each referencing column for values not in referenced table
            for col, ref_col in zip(columns, ref_columns):
                # Find all non-null values in referencing column that don't exist in referenced
                validation_sql = f"""
                    SELECT {col} FROM {table}
                    WHERE {col} IS NOT NULL
                    AND {col} NOT IN (SELECT {ref_col} FROM {ref_table})
                    LIMIT 1
                """
                cursor = conn.execute(validation_sql)
                violating_row = cursor.fetchone()
                if violating_row:
                    raise MigrationError(
                        f"foreign key violation: referenced row in '{ref_table}' table does not exist"
                    )

        # Build foreign key SQL
        cols_str = ", ".join(columns)
        ref_cols_str = ", ".join(ref_columns)
        on_delete_str = f"ON DELETE {on_delete.upper()}" if on_delete else ""
        on_update_str = f"ON UPDATE {on_update.upper()}" if on_update else ""

        # SQLite doesn't support named foreign keys in ALTER TABLE ADD CONSTRAINT
        # We need to recreate the table
        fk_sql = f"""
            FOREIGN KEY ({cols_str}) REFERENCES {ref_table} ({ref_cols_str})
            {on_delete_str} {on_update_str}
        """.strip()

        # Get current table schema
        old_schema = self.get_table_schema(table)

        # Create new table with foreign key
        temp_table = f"{table}_temp"

        # Parse the CREATE TABLE statement and add the foreign key
        # Find the closing parenthesis and insert FK before it
        if old_schema.endswith(")"):
            # Insert the FK before the final ) in the column definitions
            # We need to recreate the table with the FK constraint
            pk_match = None
            # Extract columns part (before any table-level constraints)
            # This is complex, so let's use a simpler approach: create new table

        # Get current columns
        columns_info = self.get_table_columns(table)

        # Build CREATE TABLE for temp table
        col_defs = []
        for c in columns_info:
            col_def = f"{c['name']} {c['type']}"
            if c["notnull"] == 1:
                col_def += " NOT NULL"
            if c["pk"] > 0:
                col_def += " PRIMARY KEY"
            col_defs.append(col_def)

        # Add foreign key constraint
        col_defs.append(fk_sql)

        create_sql = f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"

        try:
            conn.execute("PRAGMA foreign_keys = OFF")

            # Create temp table
            conn.execute(create_sql)

            # Copy data
            keep_cols = [c["name"] for c in columns_info]
            select_cols = ", ".join(keep_cols)
            insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table}"
            conn.execute(insert_sql)

            # Drop original table
            conn.execute(f"DROP TABLE {table}")

            # Rename temp table
            conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            print(json.dumps({
                "event": "operation_applied",
                "type": "add_foreign_key",
                "table": table,
                "name": name,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to add foreign key: {e}")

    def drop_foreign_key(self, table: str, name: str, version: int) -> None:
        """Drop a foreign key constraint from a table."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        # Get all foreign keys for the table
        existing_fks = self.get_foreign_keys(table)
        if not existing_fks:
            raise MigrationError(f"no foreign keys found in table '{table}'")

        # Find the foreign key by name (in SQLite, foreign keys aren't explicitly named, so we track them)
        # For this implementation, we'll use the first FK or require matching ref table
        # Since SQLite doesn't support named FKs directly, we'll drop all or use a naming convention

        # Get current table schema
        old_schema = self.get_table_schema(table)
        conn = self._get_connection()

        # Create temp table without foreign key constraints
        temp_table = f"{table}_temp"
        columns_info = self.get_table_columns(table)

        # Build CREATE TABLE without foreign keys
        col_defs = []
        for c in columns_info:
            col_def = f"{c['name']} {c['type']}"
            if c["notnull"] == 1:
                col_def += " NOT NULL"
            if c["pk"] > 0:
                col_def += " PRIMARY KEY"
            col_defs.append(col_def)

        create_sql = f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"

        try:
            conn.execute("PRAGMA foreign_keys = OFF")

            # Create temp table
            conn.execute(create_sql)

            # Copy data
            keep_cols = [c["name"] for c in columns_info]
            select_cols = ", ".join(keep_cols)
            insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table}"
            conn.execute(insert_sql)

            # Drop original table
            conn.execute(f"DROP TABLE {table}")

            # Rename temp table
            conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            print(json.dumps({
                "event": "operation_applied",
                "type": "drop_foreign_key",
                "table": table,
                "name": name,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to drop foreign key: {e}")

    def create_index(self, name: str, table: str, columns: list[str], unique: bool, version: int) -> None:
        """Create an index on one or more columns."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        if self.index_exists(name):
            raise MigrationError(f"index '{name}' already exists")

        # Validate columns exist
        for col in columns:
            if not self.column_exists(table, col):
                raise MigrationError(f"column '{col}' does not exist in table '{table}'")

        cols_str = ", ".join(columns)
        unique_str = "UNIQUE" if unique else ""
        sql = f"CREATE {unique_str} INDEX {name} ON {table} ({cols_str})"

        conn = self._get_connection()
        try:
            conn.execute(sql)
            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "create_index",
                "name": name,
                "table": table,
                "version": version
            }))
        except Exception as e:
            raise MigrationError(f"failed to create index: {e}")

    def drop_index(self, name: str, version: int) -> None:
        """Drop an index from the database."""
        if not self.index_exists(name):
            raise MigrationError(f"index '{name}' does not exist")

        conn = self._get_connection()
        try:
            conn.execute(f"DROP INDEX {name}")
            conn.commit()

            print(json.dumps({
                "event": "operation_applied",
                "type": "drop_index",
                "name": name,
                "version": version
            }))
        except Exception as e:
            raise MigrationError(f"failed to drop index: {e}")

    def add_check_constraint(self, table: str, name: str, expression: str, version: int) -> None:
        """Add a check constraint to a table."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        # Check for duplicate constraint name
        existing_constraints = self.get_check_constraints(table)
        for c in existing_constraints:
            if c.get("name") == name:
                raise MigrationError(f"check constraint '{name}' already exists in table '{table}'")

        conn = self._get_connection()

        # Get current table schema
        old_schema = self.get_table_schema(table)
        columns_info = self.get_table_columns(table)

        # Create temp table with check constraint
        temp_table = f"{table}_temp"

        # Build CREATE TABLE for temp table
        col_defs = []
        for c in columns_info:
            col_def = f"{c['name']} {c['type']}"
            if c["notnull"] == 1:
                col_def += " NOT NULL"
            if c["pk"] > 0:
                col_def += " PRIMARY KEY"
            col_defs.append(col_def)

        # Add check constraint
        col_defs.append(f"CHECK ({expression}) CONSTRAINT {name}")

        create_sql = f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"

        try:
            conn.execute("PRAGMA foreign_keys = OFF")

            # Create temp table
            conn.execute(create_sql)

            # Copy data - first validate existing data against constraint
            # Build a SELECT that would fail if constraint is violated
            validate_sql = f"""
                SELECT COUNT(*) FROM {table}
                WHERE NOT ({expression})
            """
            cursor = conn.execute(validate_sql)
            violating_count = cursor.fetchone()[0]
            if violating_count > 0:
                conn.execute(f"DROP TABLE {temp_table}")
                conn.commit()
                conn.execute("PRAGMA foreign_keys = ON")
                raise MigrationError(
                    f"check constraint violation: {violating_count} row(s) violate constraint '{name}'"
                )

            # Copy data
            keep_cols = [c["name"] for c in columns_info]
            select_cols = ", ".join(keep_cols)
            insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table}"
            conn.execute(insert_sql)

            # Drop original table
            conn.execute(f"DROP TABLE {table}")

            # Rename temp table
            conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            print(json.dumps({
                "event": "operation_applied",
                "type": "add_check_constraint",
                "table": table,
                "name": name,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to add check constraint: {e}")

    def drop_check_constraint(self, table: str, name: str, version: int) -> None:
        """Drop a check constraint from a table."""
        if not self.table_exists(table):
            raise MigrationError(f"table '{table}' does not exist")

        # Check if constraint exists
        existing_constraints = self.get_check_constraints(table)
        constraint_exists = False
        for c in existing_constraints:
            if c.get("name") == name:
                constraint_exists = True
                break

        if not constraint_exists:
            raise MigrationError(f"check constraint '{name}' does not exist in table '{table}'")

        conn = self._get_connection()

        # Get current table schema
        columns_info = self.get_table_columns(table)

        # Create temp table without the check constraint
        temp_table = f"{table}_temp"

        # Build CREATE TABLE for temp table
        col_defs = []
        for c in columns_info:
            col_def = f"{c['name']} {c['type']}"
            if c["notnull"] == 1:
                col_def += " NOT NULL"
            if c["pk"] > 0:
                col_def += " PRIMARY KEY"
            col_defs.append(col_def)

        create_sql = f"CREATE TABLE {temp_table} ({', '.join(col_defs)})"

        try:
            conn.execute("PRAGMA foreign_keys = OFF")

            # Create temp table
            conn.execute(create_sql)

            # Copy data
            keep_cols = [c["name"] for c in columns_info]
            select_cols = ", ".join(keep_cols)
            insert_sql = f"INSERT INTO {temp_table} SELECT {select_cols} FROM {table}"
            conn.execute(insert_sql)

            # Drop original table
            conn.execute(f"DROP TABLE {table}")

            # Rename temp table
            conn.execute(f"ALTER TABLE {temp_table} RENAME TO {table}")

            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

            print(json.dumps({
                "event": "operation_applied",
                "type": "drop_check_constraint",
                "table": table,
                "name": name,
                "version": version
            }))

        except Exception as e:
            conn.rollback()
            raise MigrationError(f"failed to drop check constraint: {e}")

    def get_foreign_keys(self, table: str) -> list[dict]:
        """Get foreign key information for a table using PRAGMA."""
        conn = self._get_connection()
        cursor = conn.execute(f"PRAGMA foreign_key_list({table})")
        # Columns: id, seq, table, from, to, on_update, on_delete, match
        return [
            {
                "id": row[0],
                "seq": row[1],
                "table": row[2],
                "from": row[3],
                "to": row[4],
                "on_update": row[5],
                "on_delete": row[6],
                "match": row[7]
            }
            for row in cursor.fetchall()
        ]

    def get_indexes(self, table: str | None = None) -> list[dict]:
        """Get index information from the database."""
        conn = self._get_connection()
        if table:
            cursor = conn.execute(f"PRAGMA index_list({table})")
            indexes = []
            for row in cursor.fetchall():
                # name, seq, unique, origin, partial
                index_info = {
                    "name": row[1],
                    "unique": row[2] == 1,
                    "table": table
                }
                # Get index columns
                col_cursor = conn.execute(f"PRAGMA index_info({row[1]})")
                index_info["columns"] = [c[2] for c in col_cursor.fetchall()]
                indexes.append(index_info)
            return indexes
        else:
            cursor = conn.execute("""
                SELECT name, tbl_name, sql
                FROM sqlite_master
                WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
            """)
            return [dict(row) for row in cursor.fetchall()]

    def index_exists(self, name: str) -> bool:
        """Check if an index exists in the database."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = ?
        """, (name,))
        return cursor.fetchone() is not None

    def get_check_constraints(self, table: str) -> list[dict]:
        """Get check constraints for a table by parsing CREATE TABLE statement."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = ?
        """, (table,))
        row = cursor.fetchone()
        if not row:
            return []
        sql = row[0] or ""
        # Parse check constraints from CREATE TABLE SQL
        constraints = []
        # Simple regex-based extraction of CHECK constraints
        import re
        # Find all CHECK constraints with their names
        pattern = r'CHECK\s*\(\s*(.*?)\s*\)\s*(?:CONSTRAINT\s+(\w+))?'
        matches = re.findall(pattern, sql, re.IGNORECASE)
        for expr, name in matches:
            constraints.append({
                "expression": expr.strip(),
                "name": name if name else None
            })
        return constraints

    def get_table_schema(self, table: str) -> str:
        """Get the CREATE TABLE statement for a table."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = ?
        """, (table,))
        row = cursor.fetchone()
        if not row:
            raise MigrationError(f"table '{table}' does not exist")
        return row[0] or ""

    def apply_migration(self, migration_path: str, version: int) -> None:
        """Apply a migration from a JSON file."""
        conn = self._get_connection()

        # Enable foreign keys for this migration
        conn.execute("PRAGMA foreign_keys = ON")

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
        applied_operations = []
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
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "add_column":
                    table = op.get("table")
                    column = op.get("column")
                    if not table:
                        raise MigrationError("add_column operation missing 'table'")
                    if not column:
                        raise MigrationError("add_column operation missing 'column'")
                    self.add_column(table, column, migration_version)
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "drop_column":
                    table = op.get("table")
                    column = op.get("column")
                    if not table:
                        raise MigrationError("drop_column operation missing 'table'")
                    if not column:
                        raise MigrationError("drop_column operation missing 'column'")
                    # Store column definition for rollback
                    col_info = self.get_table_columns(table)
                    col_def = next((c for c in col_info if c["name"] == column), None)
                    if col_def:
                        op = op.copy()
                        op["original_definition"] = {
                            "name": col_def["name"],
                            "type": col_def["type"],
                            "not_null": col_def["notnull"] == 1,
                            "default": col_def["dflt_value"]
                        }
                    self.drop_column(table, column, migration_version)
                    applied_operations.append(op)
                    ops_count += 1

                elif op_type == "transform_data":
                    table = op.get("table")
                    transformations = op.get("transformations", [])
                    if not table:
                        raise MigrationError("transform_data operation missing 'table'")
                    if not transformations:
                        raise MigrationError("transform_data operation missing 'transformations'")
                    self.transform_data(table, transformations, migration_version)
                    applied_operations.append(op.copy())
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
                    applied_operations.append(op.copy())
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
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "add_foreign_key":
                    table = op.get("table")
                    name = op.get("name")
                    columns = op.get("columns", [])
                    references = op.get("references", {})
                    on_delete = op.get("on_delete")
                    on_update = op.get("on_update")
                    if not table:
                        raise MigrationError("add_foreign_key operation missing 'table'")
                    if not name:
                        raise MigrationError("add_foreign_key operation missing 'name'")
                    if not columns:
                        raise MigrationError("add_foreign_key operation missing 'columns'")
                    if not references:
                        raise MigrationError("add_foreign_key operation missing 'references'")
                    self.add_foreign_key(table, name, columns, references, on_delete, on_update, migration_version)
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "drop_foreign_key":
                    table = op.get("table")
                    name = op.get("name")
                    if not table:
                        raise MigrationError("drop_foreign_key operation missing 'table'")
                    if not name:
                        raise MigrationError("drop_foreign_key operation missing 'name'")
                    # Store FK definition for rollback
                    fks = self.get_foreign_keys(table)
                    fk_def = next((fk for fk in fks if fk.get("table") == op.get("references", {}).get("table")), None)
                    if fk_def:
                        op = op.copy()
                        op["original_definition"] = fk_def
                    self.drop_foreign_key(table, name, migration_version)
                    applied_operations.append(op)
                    ops_count += 1

                elif op_type == "create_index":
                    name = op.get("name")
                    table = op.get("table")
                    columns = op.get("columns", [])
                    unique = op.get("unique", False)
                    if not name:
                        raise MigrationError("create_index operation missing 'name'")
                    if not table:
                        raise MigrationError("create_index operation missing 'table'")
                    if not columns:
                        raise MigrationError("create_index operation missing 'columns'")
                    self.create_index(name, table, columns, unique, migration_version)
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "drop_index":
                    name = op.get("name")
                    if not name:
                        raise MigrationError("drop_index operation missing 'name'")
                    # Store index definition for rollback
                    indexes = self.get_indexes()
                    idx_def = next((idx for idx in indexes if idx["name"] == name), None)
                    if idx_def:
                        op = op.copy()
                        op["original_definition"] = idx_def
                    self.drop_index(name, migration_version)
                    applied_operations.append(op)
                    ops_count += 1

                elif op_type == "add_check_constraint":
                    table = op.get("table")
                    name = op.get("name")
                    expression = op.get("expression")
                    if not table:
                        raise MigrationError("add_check_constraint operation missing 'table'")
                    if not name:
                        raise MigrationError("add_check_constraint operation missing 'name'")
                    if not expression:
                        raise MigrationError("add_check_constraint operation missing 'expression'")
                    self.add_check_constraint(table, name, expression, migration_version)
                    applied_operations.append(op.copy())
                    ops_count += 1

                elif op_type == "drop_check_constraint":
                    table = op.get("table")
                    name = op.get("name")
                    if not table:
                        raise MigrationError("drop_check_constraint operation missing 'table'")
                    if not name:
                        raise MigrationError("drop_check_constraint operation missing 'name'")
                    # Store constraint definition for rollback
                    constraints = self.get_check_constraints(table)
                    constr_def = next((c for c in constraints if c.get("name") == name), None)
                    if constr_def:
                        op = op.copy()
                        op["original_definition"] = constr_def
                    self.drop_check_constraint(table, name, migration_version)
                    applied_operations.append(op)
                    ops_count += 1

                else:
                    raise MigrationError(f"unknown operation type: {op_type}")

            # Record migration as complete with operations for rollback
            self.record_migration(migration_version, description, applied_operations)
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

    def remove_migration(self, version: int) -> None:
        """Remove a migration record from the _migrations table."""
        conn = self._get_connection()
        conn.execute("DELETE FROM _migrations WHERE version = ?", (version,))
        conn.commit()

    def rollback_operation(self, op: dict, version: int) -> None:
        """Rollback a single operation."""
        op_type = op.get("type")
        conn = self._get_connection()

        # Check if explicit rollback operations are provided
        rollback_ops = op.get("rollback_operations", [])
        if rollback_ops:
            # Use explicit rollback operations
            for rb_op in rollback_ops:
                rb_type = rb_op.get("type")
                if rb_type == "drop_column":
                    table = rb_op.get("table")
                    column = rb_op.get("column")
                    if not table or not column:
                        raise MigrationError("drop_column rollback missing table or column")
                    print(json.dumps({
                        "event": "operation_rolled_back",
                        "type": rb_type,
                        "table": table,
                        "column": column,
                        "version": version
                    }))
                    self.drop_column(table, column, version)
                else:
                    # For other operation types, just print and continue
                    print(json.dumps({
                        "event": "operation_rolled_back",
                        "type": rb_type,
                        "version": version
                    }))
            return

        # Automatic rollback based on operation type
        if op_type == "create_table":
            table = op.get("table")
            if not table:
                raise MigrationError("create_table operation missing 'table' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "drop_table",
                "table": table,
                "version": version
            }))
            if self.table_exists(table):
                # Check for foreign keys referencing this table
                fks = self.get_foreign_keys(table)
                if fks:
                    # Check if there's data that would violate FK constraints
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    if cursor.fetchone()[0] > 0:
                        raise MigrationError("cannot rollback: foreign key constraint violation")
                conn.execute(f"DROP TABLE {table}")
                conn.commit()

        elif op_type == "add_column":
            table = op.get("table")
            column = op.get("column", {}).get("name") or op.get("column")
            if not table or not column:
                raise MigrationError("add_column operation missing 'table' or 'column' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "drop_column",
                "table": table,
                "column": column,
                "version": version
            }))
            if self.column_exists(table, column):
                self.drop_column(table, column, version)

        elif op_type == "drop_column":
            table = op.get("table")
            column = op.get("column")
            original_def = op.get("original_definition")
            if not table or not column:
                raise MigrationError("drop_column operation missing 'table' or 'column' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "add_column",
                "table": table,
                "column": column,
                "version": version
            }))
            # Recreate column with original definition
            if original_def:
                col_spec = {
                    "name": original_def.get("name", column),
                    "type": original_def.get("type", "TEXT"),
                    "not_null": original_def.get("not_null", False),
                    "default": original_def.get("default")
                }
            else:
                # Fallback to basic definition
                col_spec = {"name": column, "type": "TEXT"}
            self.add_column(table, col_spec, version)

        elif op_type == "add_foreign_key":
            table = op.get("table")
            name = op.get("name")
            columns = op.get("columns", [])
            references = op.get("references", {})
            on_delete = op.get("on_delete")
            on_update = op.get("on_update")
            if not table or not name or not columns or not references:
                raise MigrationError("add_foreign_key operation missing required fields for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "drop_foreign_key",
                "table": table,
                "name": name,
                "version": version
            }))
            self.add_foreign_key(table, name, columns, references, on_delete, on_update, version)

        elif op_type == "drop_foreign_key":
            table = op.get("table")
            name = op.get("name")
            original_def = op.get("original_definition")
            if not table or not name:
                raise MigrationError("drop_foreign_key operation missing 'table' or 'name' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "add_foreign_key",
                "table": table,
                "name": name,
                "version": version
            }))
            # Restore FK from original definition if available
            if original_def:
                cols = original_def.get("from", [])
                ref_table = original_def.get("table", "")
                ref_cols = [original_def.get("to")] if original_def.get("to") else []
                on_delete = original_def.get("on_delete")
                on_update = original_def.get("on_update")
                self.add_foreign_key(table, name, cols, {"table": ref_table, "columns": ref_cols},
                                    on_delete, on_update, version)
            else:
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations or original_definition for drop_foreign_key"
                )

        elif op_type == "create_index":
            name = op.get("name")
            table = op.get("table")
            columns = op.get("columns", [])
            unique = op.get("unique", False)
            if not name or not table or not columns:
                raise MigrationError("create_index operation missing required fields for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "drop_index",
                "table": table,
                "name": name,
                "version": version
            }))
            self.drop_index(name, version)

        elif op_type == "drop_index":
            name = op.get("name")
            original_def = op.get("original_definition")
            if not name:
                raise MigrationError("drop_index operation missing 'name' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "create_index",
                "name": name,
                "version": version
            }))
            # Recreate index from original definition if available
            if original_def:
                table = original_def.get("table")
                columns = original_def.get("columns", [])
                unique = original_def.get("unique", False)
                if table and columns:
                    self.create_index(name, table, columns, unique, version)
                else:
                    raise MigrationError(
                        f"cannot rollback version {version}: missing original_definition for drop_index"
                    )
            else:
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations or original_definition for drop_index"
                )

        elif op_type == "add_check_constraint":
            table = op.get("table")
            name = op.get("name")
            expression = op.get("expression")
            if not table or not name or not expression:
                raise MigrationError("add_check_constraint operation missing required fields for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "drop_check_constraint",
                "table": table,
                "name": name,
                "version": version
            }))
            # Can't automatically add back a check constraint without complex schema rewrite
            raise MigrationError(
                f"cannot rollback version {version}: add_check_constraint requires explicit rollback_operations"
            )

        elif op_type == "drop_check_constraint":
            table = op.get("table")
            name = op.get("name")
            original_def = op.get("original_definition")
            if not table or not name:
                raise MigrationError("drop_check_constraint operation missing 'table' or 'name' for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "add_check_constraint",
                "table": table,
                "name": name,
                "version": version
            }))
            # Restore constraint from original definition if available
            if original_def:
                expression = original_def.get("expression")
                if expression:
                    self.add_check_constraint(table, name, expression, version)
                else:
                    raise MigrationError(
                        f"cannot rollback version {version}: missing expression in original_definition for drop_check_constraint"
                    )
            else:
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations or original_definition for drop_check_constraint"
                )

        elif op_type == "transform_data":
            # transform_data cannot be automatically rolled back
            if not op.get("rollback_operations"):
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for transform_data"
                )
            # Explicit rollback operations will be handled above

        elif op_type == "migrate_column_data":
            table = op.get("table")
            from_column = op.get("from_column")
            to_column = op.get("to_column")
            default_value = op.get("default_value")
            if not table or not from_column or not to_column:
                raise MigrationError("migrate_column_data operation missing required fields for rollback")
            print(json.dumps({
                "event": "operation_rolled_back",
                "type": "migrate_column_data",
                "table": table,
                "from_column": to_column,  # Reverse direction
                "to_column": from_column,  # Reverse direction
                "version": version
            }))
            # Reverse the migration - copy from to_column back to from_column
            self.migrate_column_data(table, to_column, from_column, default_value, version)

        elif op_type == "backfill_data":
            # backfill_data cannot be automatically rolled back
            if not op.get("rollback_operations"):
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for backfill_data"
                )
            # Explicit rollback operations will be handled above

        else:
            raise MigrationError(f"unknown operation type for rollback: {op_type}")

    def rollback_migration(self, version: int, migration: dict = None) -> None:
        """Rollback a single migration.

        Args:
            version: The version to rollback
            migration: Optional pre-fetched migration dict (if not provided, fetched from _migrations table)
        """
        conn = self._get_connection()
        conn.execute("PRAGMA foreign_keys = OFF")

        try:
            # Get migration details if not provided
            if migration is None:
                cursor = conn.execute(
                    "SELECT version, description, operations FROM _migrations WHERE version = ?",
                    (version,)
                )
                row = cursor.fetchone()
                if not row:
                    raise MigrationError(f"version {version} not found")
                migration = {
                    "version": row["version"],
                    "description": row["description"],
                    "operations": json.loads(row["operations"])
                }

            description = migration["description"]
            operations = migration["operations"]

            # Print rollback started event
            print(json.dumps({
                "event": "rollback_started",
                "version": version,
                "description": description
            }))

            # Rollback operations in reverse order
            for op in reversed(operations):
                self.rollback_operation(op, version)

            # Remove migration record
            self.remove_migration(version)

            # Print completion event
            print(json.dumps({
                "event": "rollback_complete",
                "version": version
            }))

            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")

        except MigrationError as e:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise
        except Exception as e:
            conn.rollback()
            conn.execute("PRAGMA foreign_keys = ON")
            raise MigrationError(f"rollback failed: {e}")

    def rollback_to_version(self, target_version: int) -> list[int]:
        """Rollback migrations to a specific version (exclusive).

        Args:
            target_version: The version to rollback to (migrations above this will be rolled back)

        Returns:
            List of versions that were rolled back
        """
        migrations = self.get_migrations()
        if not migrations:
            raise MigrationError("no migrations to rollback")

        # Find migrations to rollback (versions > target_version)
        migrations_to_rollback = [m for m in migrations if m["version"] > target_version]

        if not migrations_to_rollback:
            print(json.dumps({
                "event": "rollback_finished",
                "versions_rolled_back": [],
                "final_version": self.get_latest_migration_version() or 0
            }))
            return []

        # Sort by version descending (highest first)
        migrations_to_rollback.sort(key=lambda m: m["version"], reverse=True)

        rolled_back = []
        for migration in migrations_to_rollback:
            self.rollback_migration(migration["version"], migration)
            rolled_back.append(migration["version"])

        final_version = self.get_latest_migration_version()
        print(json.dumps({
            "event": "rollback_finished",
            "versions_rolled_back": rolled_back,
            "final_version": final_version or 0
        }))

        return rolled_back

    def rollback_count(self, count: int) -> list[int]:
        """Rollback the last N migrations.

        Args:
            count: Number of migrations to rollback

        Returns:
            List of versions that were rolled back
        """
        migrations = self.get_migrations()
        if not migrations:
            raise MigrationError("no migrations to rollback")

        if count <= 0:
            raise MigrationError("count must be positive")

        # Get the last N migrations
        migrations_to_rollback = migrations[-count:] if len(migrations) > count else migrations

        # Sort by version descending (highest first)
        migrations_to_rollback.sort(key=lambda m: m["version"], reverse=True)

        rolled_back = []
        for migration in migrations_to_rollback:
            self.rollback_migration(migration["version"], migration)
            rolled_back.append(migration["version"])

        final_version = self.get_latest_migration_version()
        print(json.dumps({
            "event": "rollback_finished",
            "versions_rolled_back": rolled_back,
            "final_version": final_version or 0
        }))

        return rolled_back


def main():
    migrate_usage = "python migration_tool.py migrate <migration_file> <database>"
    rollback_usage = "python migration_tool.py rollback <database> [--to-version <version>] [--count <n>]"

    if len(sys.argv) < 2:
        print(f"Usage: {migrate_usage} or {rollback_usage}", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "migrate":
        if len(sys.argv) < 4:
            print(f"Usage: {migrate_usage}", file=sys.stderr)
            sys.exit(1)

        migration_file = sys.argv[2]
        db_file = sys.argv[3]

        try:
            tool = MigrationTool(db_file)
            tool.apply_migration(migration_file, None)
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif command == "rollback":
        if len(sys.argv) < 3:
            print(f"Usage: {rollback_usage}", file=sys.stderr)
            sys.exit(1)

        db_file = sys.argv[2]
        to_version = None
        count = None

        i = 3
        while i < len(sys.argv):
            if sys.argv[i] == "--to-version" and i + 1 < len(sys.argv):
                try:
                    to_version = int(sys.argv[i + 1])
                except ValueError:
                    print(f"Error: invalid version number", file=sys.stderr)
                    sys.exit(1)
                i += 2
            elif sys.argv[i] == "--count" and i + 1 < len(sys.argv):
                try:
                    count = int(sys.argv[i + 1])
                except ValueError:
                    print(f"Error: invalid count number", file=sys.stderr)
                    sys.exit(1)
                i += 2
            else:
                print(f"Error: unknown option '{sys.argv[i]}'", file=sys.stderr)
                print(f"Usage: {rollback_usage}", file=sys.stderr)
                sys.exit(1)

        # Validate options
        if to_version is not None and count is not None:
            print("Error: cannot use both --to-version and --count", file=sys.stderr)
            sys.exit(1)

        try:
            tool = MigrationTool(db_file)
            if to_version is not None:
                tool.rollback_to_version(to_version)
            else:
                tool.rollback_count(count if count is not None else 1)
        except MigrationError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Error: unknown command '{command}'", file=sys.stderr)
        print(f"Usage: {migrate_usage} or {rollback_usage}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
