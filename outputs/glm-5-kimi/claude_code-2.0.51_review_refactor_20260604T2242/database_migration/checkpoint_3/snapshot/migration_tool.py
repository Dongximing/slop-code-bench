#!/usr/bin/env python3
"""
Database Migration Tool for SQLite
Applies schema migrations from JSON specification files.
"""

import argparse
import json
import sqlite3
import sys
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class MigrationTool:
    """Handles database schema migrations for SQLite."""

    # Valid SQLite data types
    VALID_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}

    # Valid foreign key actions
    VALID_FK_ACTIONS = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self.conn = sqlite3.connect(self.db_path)
        # Enable foreign keys by default for migrations
        self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def _validate_identifier(self, identifier: str, identifier_type: str) -> None:
        if not identifier:
            raise MigrationError(f"invalid {identifier_type}: cannot be empty")

        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier):
            raise MigrationError(
                f"invalid {identifier_type}: '{identifier}' is not a valid SQLite identifier"
            )

    def _quote_identifier(self, identifier: str) -> str:
        return f'"{identifier.replace(chr(34), chr(34)+chr(34))}"'

    def _build_column_def(self, column: Dict[str, Any]) -> str:
        name = column.get('name')
        col_type = column.get('type', 'TEXT').upper()

        if col_type not in self.VALID_TYPES:
            raise MigrationError(
                f"invalid column type: '{col_type}' (valid types: {', '.join(self.VALID_TYPES)})"
            )

        self._validate_identifier(name, 'column name')

        parts = [self._quote_identifier(name), col_type]

        is_primary_key = column.get('primary_key', False)
        auto_increment = column.get('auto_increment', False)
        not_null = column.get('not_null', False)
        unique = column.get('unique', False)
        default = column.get('default')

        if auto_increment:
            if not is_primary_key:
                raise MigrationError(
                    f"auto_increment can only be true for primary key columns"
                )
            if col_type != 'INTEGER':
                raise MigrationError(
                    f"auto_increment can only be used with INTEGER type, got '{col_type}'"
                )

        if is_primary_key:
            if auto_increment:
                parts.append('PRIMARY KEY AUTOINCREMENT')
            else:
                parts.append('PRIMARY KEY')

        if not_null and not is_primary_key:
            parts.append('NOT NULL')

        if unique and not is_primary_key:
            parts.append('UNIQUE')

        if default is not None:
            parts.append(f'DEFAULT {default}')

        return ' '.join(parts)

    def _ensure_migrations_table(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.commit()

    def _is_migration_applied(self, version: int) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def _record_migration(self, version: int, description: str) -> None:
        self.conn.execute(
            "INSERT INTO _migrations (version, description) VALUES (?, ?)",
            (version, description)
        )
        self.conn.commit()

    def _table_exists(self, table_name: str) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cursor.fetchone() is not None

    def _get_table_columns(self, table_name: str) -> List[str]:
        cursor = self.conn.execute(f'PRAGMA table_info({self._quote_identifier(table_name)})')
        return [row[1] for row in cursor.fetchall()]

    def _get_column_info(self, table_name: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(f'PRAGMA table_info({self._quote_identifier(table_name)})')
        columns = []
        for row in cursor.fetchall():
            columns.append({
                'cid': row[0],
                'name': row[1],
                'type': row[2],
                'notnull': row[3],
                'default': row[4],
                'pk': row[5]
            })
        return columns

    def _get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Get all foreign keys for a table."""
        cursor = self.conn.execute(f'PRAGMA foreign_key_list({self._quote_identifier(table_name)})')
        fks = []
        for row in cursor.fetchall():
            fks.append({
                'id': row[0],
                'seq': row[1],
                'table': row[2],
                'from': row[3],
                'to': row[4],
                'on_update': row[5],
                'on_delete': row[6],
                'match': row[7]
            })
        return fks

    def _get_fk_constraint_name(self, table_name: str, fk_id: int) -> Optional[str]:
        """Get the name of a foreign key constraint from sqlite_master."""
        cursor = self.conn.execute(
            """SELECT sql FROM sqlite_master
               WHERE type='table' AND name=?""",
            (table_name,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            sql = row[0]
            # Parse FK names from CREATE TABLE statement
            # This is a simplified approach - look for CONSTRAINT name FOREIGN KEY
            pattern = r'CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY'
            matches = re.findall(pattern, sql, re.IGNORECASE)
            # Return name at index if available
            if fk_id < len(matches):
                return matches[fk_id]
        return None

    def _index_exists(self, index_name: str) -> bool:
        """Check if an index exists in the database."""
        cursor = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        return cursor.fetchone() is not None

    def _get_indexes_for_table(self, table_name: str) -> List[Dict[str, Any]]:
        """Get all indexes for a table."""
        cursor = self.conn.execute(
            """SELECT name, sql FROM sqlite_master
               WHERE type='index' AND tbl_name=? AND sql IS NOT NULL""",
            (table_name,)
        )
        indexes = []
        for row in cursor.fetchall():
            indexes.append({
                'name': row[0],
                'sql': row[1]
            })
        return indexes

    def _recreate_indexes(self, old_table_name: str, new_table_name: str) -> None:
        """Recreate indexes after table recreation, updating table references."""
        indexes = self._get_indexes_for_table(old_table_name)
        for idx in indexes:
            if idx['sql']:
                # Replace old table name with new table name in index SQL
                # Handle both quoted and unquoted table names
                new_sql = re.sub(
                    rf'ON\s+["\']?{re.escape(old_table_name)}["\']?',
                    f'ON {self._quote_identifier(new_table_name)}',
                    idx['sql'],
                    flags=re.IGNORECASE
                )
                self.conn.execute(new_sql)

    def _get_check_constraints(self, table_name: str) -> List[Dict[str, Any]]:
        """Get all check constraints for a table by parsing the CREATE TABLE statement."""
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            return []

        sql = row[0]
        constraints = []

        # Parse CHECK constraints from CREATE TABLE
        # Pattern matches: CONSTRAINT "name" CHECK (expression) or CONSTRAINT name CHECK (expression) or CHECK (expression)
        pattern = r'(?:CONSTRAINT\s+["\']?(\w+)["\']?\s+)?CHECK\s*\(([^)]+(?:\([^)]*\)[^)]*)*)\)'
        matches = re.finditer(pattern, sql, re.IGNORECASE)

        for i, match in enumerate(matches):
            name = match.group(1) if match.group(1) else f'_check_{i}'
            expression = match.group(2).strip()
            constraints.append({
                'name': name,
                'expression': expression
            })

        return constraints

    def _create_table(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        table_name = operation.get('table')
        columns = operation.get('columns', [])

        self._validate_identifier(table_name, 'table name')

        if not columns:
            raise MigrationError("create_table requires at least one column")

        if self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' already exists")

        primary_key_count = 0
        for col in columns:
            if col.get('primary_key', False):
                primary_key_count += 1

        if primary_key_count > 1:
            raise MigrationError("at most one column can have primary_key: true")

        col_defs = [self._build_column_def(col) for col in columns]

        sql = f"CREATE TABLE {self._quote_identifier(table_name)} ({', '.join(col_defs)})"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'create_table',
            'table': table_name,
            'version': version
        }

    def _add_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        table_name = operation.get('table')
        column = operation.get('column')

        self._validate_identifier(table_name, 'table name')

        if not column:
            raise MigrationError("add_column requires a 'column' specification")

        column_name = column.get('name')
        self._validate_identifier(column_name, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        existing_columns = self._get_table_columns(table_name)
        if column_name in existing_columns:
            raise MigrationError(
                f"invalid operation: column '{column_name}' already exists in table '{table_name}'"
            )

        col_def = self._build_column_def(column)

        sql = f"ALTER TABLE {self._quote_identifier(table_name)} ADD COLUMN {col_def}"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'add_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _drop_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        table_name = operation.get('table')
        column_name = operation.get('column')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(column_name, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        columns_info = self._get_column_info(table_name)
        column_names = [col['name'] for col in columns_info]

        if column_name not in column_names:
            raise MigrationError(
                f"invalid operation: column '{column_name}' does not exist in table '{table_name}'"
            )

        if len(columns_info) == 1:
            raise MigrationError(
                f"cannot drop column '{column_name}': it's the only column in table '{table_name}'"
            )

        for col in columns_info:
            if col['name'] == column_name and col['pk'] > 0:
                raise MigrationError(
                    f"cannot drop column '{column_name}': it's a PRIMARY KEY column"
                )

        columns_to_keep = [col for col in columns_info if col['name'] != column_name]

        new_table_name = f"_{table_name}_new"

        col_defs = []
        for col in columns_to_keep:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(col_defs)})"
        self.conn.execute(create_sql)

        keep_col_names = [self._quote_identifier(col['name']) for col in columns_to_keep]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(keep_col_names)}) SELECT {', '.join(keep_col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'drop_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _add_foreign_key(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Add a foreign key constraint to a table."""
        table_name = operation.get('table')
        fk_name = operation.get('name')
        columns = operation.get('columns', [])
        references = operation.get('references', {})
        on_delete = operation.get('on_delete', 'NO ACTION')
        on_update = operation.get('on_update', 'NO ACTION')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(fk_name, 'foreign key name')

        if not columns:
            raise MigrationError("add_foreign_key requires 'columns' array")

        if not references:
            raise MigrationError("add_foreign_key requires 'references' object")

        ref_table = references.get('table')
        ref_columns = references.get('columns', [])

        if not ref_table:
            raise MigrationError("add_foreign_key requires referenced table")

        if not ref_columns:
            raise MigrationError("add_foreign_key requires referenced columns")

        self._validate_identifier(ref_table, 'referenced table name')

        # Validate ON DELETE and ON UPDATE actions
        on_delete_upper = on_delete.upper()
        on_update_upper = on_update.upper()

        if on_delete_upper not in self.VALID_FK_ACTIONS:
            raise MigrationError(
                f"invalid on_delete action: '{on_delete}' (valid: {', '.join(self.VALID_FK_ACTIONS)})"
            )

        if on_update_upper not in self.VALID_FK_ACTIONS:
            raise MigrationError(
                f"invalid on_update action: '{on_update}' (valid: {', '.join(self.VALID_FK_ACTIONS)})"
            )

        # Check that the table exists
        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check that the referenced table exists
        if not self._table_exists(ref_table):
            raise MigrationError(
                f"cannot add foreign key: referenced table '{ref_table}' does not exist"
            )

        # Validate that referencing columns exist
        table_columns = self._get_table_columns(table_name)
        for col in columns:
            self._validate_identifier(col, 'column name')
            if col not in table_columns:
                raise MigrationError(
                    f"cannot add foreign key: column '{col}' does not exist in table '{table_name}'"
                )

        # Validate that referenced columns exist
        ref_table_columns = self._get_table_columns(ref_table)
        for col in ref_columns:
            self._validate_identifier(col, 'referenced column name')
            if col not in ref_table_columns:
                raise MigrationError(
                    f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist"
                )

        # Validate column count match
        if len(columns) != len(ref_columns):
            raise MigrationError(
                f"foreign key column count mismatch: {len(columns)} columns reference {len(ref_columns)} columns"
            )

        # Check for duplicate foreign key name in the table
        existing_fks = self._get_foreign_keys(table_name)
        # SQLite doesn't store FK names directly, so we need to check the CREATE TABLE statement
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            if f'CONSTRAINT {fk_name}' in row[0] or f'CONSTRAINT "{fk_name}"' in row[0]:
                raise MigrationError(
                    f"foreign key '{fk_name}' already exists in table '{table_name}'"
                )

        # We need to recreate the table to add a foreign key in SQLite
        # Get current table schema
        columns_info = self._get_column_info(table_name)
        existing_fks = self._get_foreign_keys(table_name)
        existing_checks = self._get_check_constraints(table_name)

        # Build new table with additional FK
        new_table_name = f"_{table_name}_new"

        col_defs = []
        for col in columns_info:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        # Add existing foreign keys
        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        # Add new foreign key
        fk_col_list = ', '.join(self._quote_identifier(c) for c in columns)
        ref_col_list = ', '.join(self._quote_identifier(c) for c in ref_columns)
        fk_defs.append(
            f"CONSTRAINT {self._quote_identifier(fk_name)} "
            f"FOREIGN KEY ({fk_col_list}) "
            f"REFERENCES {self._quote_identifier(ref_table)} ({ref_col_list}) "
            f"ON DELETE {on_delete_upper} ON UPDATE {on_update_upper}"
        )

        # Add existing check constraints
        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        all_defs = col_defs + fk_defs + check_defs

        # Get indexes before dropping the table (to recreate after)
        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        # Copy data
        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        # Drop old table (this will cascade drop indexes)
        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        # Recreate indexes with updated table name
        for idx in indexes_to_recreate:
            if idx['sql']:
                # Replace old table name with new table name in index SQL
                new_sql = re.sub(
                    rf'ON\s+["\']?{re.escape(table_name)}["\']?',
                    f'ON {self._quote_identifier(table_name)}',
                    idx['sql'],
                    flags=re.IGNORECASE
                )
                self.conn.execute(new_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'add_foreign_key',
            'table': table_name,
            'name': fk_name,
            'version': version
        }

    def _drop_foreign_key(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Remove a foreign key constraint from a table."""
        table_name = operation.get('table')
        fk_name = operation.get('name')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(fk_name, 'foreign key name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Get current table schema
        cursor = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        row = cursor.fetchone()
        if not row or not row[0]:
            raise MigrationError(f"cannot read schema for table '{table_name}'")

        # Check if the foreign key exists
        sql = row[0]
        # Look for the constraint name in the SQL (handle both quoted and unquoted identifiers)
        fk_pattern = rf'CONSTRAINT\s+["\']?{re.escape(fk_name)}["\']?\s+FOREIGN\s+KEY'
        if not re.search(fk_pattern, sql, re.IGNORECASE):
            # Also check without CONSTRAINT keyword (SQLite auto-generated names)
            existing_fks = self._get_foreign_keys(table_name)
            found = False
            for i, fk in enumerate(existing_fks):
                # Try to match by position/name
                if self._get_fk_constraint_name(table_name, i) == fk_name:
                    found = True
                    break
            if not found:
                raise MigrationError(
                    f"foreign key '{fk_name}' does not exist in table '{table_name}'"
                )

        # Recreate table without this FK
        columns_info = self._get_column_info(table_name)
        existing_fks = self._get_foreign_keys(table_name)
        existing_checks = self._get_check_constraints(table_name)

        new_table_name = f"_{table_name}_new"

        col_defs = []
        for col in columns_info:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        # Add remaining foreign keys (excluding the one being dropped)
        fk_defs = []
        fk_pattern = rf'CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY'
        for i, fk in enumerate(existing_fks):
            fk_constraint_name = self._get_fk_constraint_name(table_name, i)
            if fk_constraint_name == fk_name:
                continue  # Skip the FK being dropped

            # Build FK definition
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        # Add existing check constraints
        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        all_defs = col_defs + fk_defs + check_defs

        # Get indexes before dropping the table (to recreate after)
        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        # Copy data
        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        # Drop old table (this will cascade drop indexes)
        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        # Recreate indexes with updated table name
        for idx in indexes_to_recreate:
            if idx['sql']:
                # Replace old table name with new table name in index SQL
                new_sql = re.sub(
                    rf'ON\s+["\']?{re.escape(table_name)}["\']?',
                    f'ON {self._quote_identifier(table_name)}',
                    idx['sql'],
                    flags=re.IGNORECASE
                )
                self.conn.execute(new_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'drop_foreign_key',
            'table': table_name,
            'name': fk_name,
            'version': version
        }

    def _create_index(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Create an index on one or more columns."""
        index_name = operation.get('name')
        table_name = operation.get('table')
        columns = operation.get('columns', [])
        unique = operation.get('unique', False)

        self._validate_identifier(index_name, 'index name')
        self._validate_identifier(table_name, 'table name')

        if not columns:
            raise MigrationError("create_index requires 'columns' array")

        # Check that table exists
        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check that index doesn't already exist
        if self._index_exists(index_name):
            raise MigrationError(f"index '{index_name}' already exists")

        # Validate columns exist
        table_columns = self._get_table_columns(table_name)
        for col in columns:
            self._validate_identifier(col, 'column name')
            if col not in table_columns:
                raise MigrationError(
                    f"cannot create index: column '{col}' does not exist in table '{table_name}'"
                )

        # Build CREATE INDEX statement
        unique_str = "UNIQUE " if unique else ""
        col_list = ', '.join(self._quote_identifier(c) for c in columns)
        sql = f"CREATE {unique_str}INDEX {self._quote_identifier(index_name)} ON {self._quote_identifier(table_name)} ({col_list})"

        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'create_index',
            'name': index_name,
            'table': table_name,
            'version': version
        }

    def _drop_index(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop an index from the database."""
        index_name = operation.get('name')

        self._validate_identifier(index_name, 'index name')

        # Check that index exists
        if not self._index_exists(index_name):
            raise MigrationError(f"index '{index_name}' does not exist")

        sql = f"DROP INDEX {self._quote_identifier(index_name)}"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'drop_index',
            'name': index_name,
            'version': version
        }

    def _add_check_constraint(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Add a check constraint to a table."""
        table_name = operation.get('table')
        constraint_name = operation.get('name')
        expression = operation.get('expression')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(constraint_name, 'constraint name')

        if not expression:
            raise MigrationError("add_check_constraint requires 'expression'")

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check for duplicate constraint name
        existing_checks = self._get_check_constraints(table_name)
        for check in existing_checks:
            if check['name'] == constraint_name:
                raise MigrationError(
                    f"check constraint '{constraint_name}' already exists in table '{table_name}'"
                )

        # Recreate table with new constraint
        columns_info = self._get_column_info(table_name)
        existing_fks = self._get_foreign_keys(table_name)
        existing_checks = self._get_check_constraints(table_name)

        new_table_name = f"_{table_name}_new"

        col_defs = []
        for col in columns_info:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        # Add existing foreign keys
        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        # Add existing check constraints
        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        # Add new check constraint
        check_defs.append(
            f"CONSTRAINT {self._quote_identifier(constraint_name)} CHECK ({expression})"
        )

        all_defs = col_defs + fk_defs + check_defs

        # Get indexes before dropping the table (to recreate after)
        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        # Copy data (this will validate the constraint)
        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        try:
            self.conn.execute(insert_sql)
        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            # Drop the new table
            self.conn.execute(f"DROP TABLE IF EXISTS {self._quote_identifier(new_table_name)}")
            raise MigrationError(f"check constraint violation: {expression}")

        # Drop old table (this will cascade drop indexes)
        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        # Recreate indexes with updated table name
        for idx in indexes_to_recreate:
            if idx['sql']:
                # Replace old table name with new table name in index SQL
                new_sql = re.sub(
                    rf'ON\s+["\']?{re.escape(table_name)}["\']?',
                    f'ON {self._quote_identifier(table_name)}',
                    idx['sql'],
                    flags=re.IGNORECASE
                )
                self.conn.execute(new_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'add_check_constraint',
            'table': table_name,
            'name': constraint_name,
            'version': version
        }

    def _drop_check_constraint(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Remove a check constraint from a table."""
        table_name = operation.get('table')
        constraint_name = operation.get('name')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(constraint_name, 'constraint name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        # Check that constraint exists
        existing_checks = self._get_check_constraints(table_name)
        constraint_found = False
        for check in existing_checks:
            if check['name'] == constraint_name:
                constraint_found = True
                break

        if not constraint_found:
            raise MigrationError(
                f"check constraint '{constraint_name}' does not exist in table '{table_name}'"
            )

        # Recreate table without this constraint
        columns_info = self._get_column_info(table_name)
        existing_fks = self._get_foreign_keys(table_name)

        new_table_name = f"_{table_name}_new"

        col_defs = []
        for col in columns_info:
            col_def_parts = [self._quote_identifier(col['name']), col['type']]

            if col['pk'] > 0:
                col_def_parts.append('PRIMARY KEY')

            if col['notnull'] and col['pk'] == 0:
                col_def_parts.append('NOT NULL')

            if col['default'] is not None:
                col_def_parts.append(f"DEFAULT {col['default']}")

            col_defs.append(' '.join(col_def_parts))

        # Add existing foreign keys
        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        # Add remaining check constraints (excluding the one being dropped)
        check_defs = []
        for check in existing_checks:
            if check['name'] != constraint_name:
                check_defs.append(
                    f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
                )

        all_defs = col_defs + fk_defs + check_defs

        # Get indexes before dropping the table (to recreate after)
        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        # Copy data
        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        # Drop old table (this will cascade drop indexes)
        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        # Rename new table
        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        # Recreate indexes with updated table name
        for idx in indexes_to_recreate:
            if idx['sql']:
                # Replace old table name with new table name in index SQL
                new_sql = re.sub(
                    rf'ON\s+["\']?{re.escape(table_name)}["\']?',
                    f'ON {self._quote_identifier(table_name)}',
                    idx['sql'],
                    flags=re.IGNORECASE
                )
                self.conn.execute(new_sql)

        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'drop_check_constraint',
            'table': table_name,
            'name': constraint_name,
            'version': version
        }

    def _validate_migration_file(self, migration_data: Dict[str, Any]) -> None:
        required_fields = ['version', 'description', 'operations']

        for field in required_fields:
            if field not in migration_data:
                raise MigrationError(f"invalid migration schema: missing required field '{field}'")

        version = migration_data['version']
        if not isinstance(version, int) or version <= 0:
            raise MigrationError("migration version must be a positive integer")

        operations = migration_data['operations']
        if not isinstance(operations, list):
            raise MigrationError("invalid migration schema: 'operations' must be an array")

        valid_operations = {
            'create_table', 'add_column', 'drop_column',
            'add_foreign_key', 'drop_foreign_key',
            'create_index', 'drop_index',
            'add_check_constraint', 'drop_check_constraint'
        }

        for i, op in enumerate(operations):
            if not isinstance(op, dict):
                raise MigrationError(f"invalid operation at index {i}: must be an object")

            op_type = op.get('type')
            if not op_type:
                raise MigrationError(f"invalid operation at index {i}: missing 'type' field")

            if op_type not in valid_operations:
                raise MigrationError(f"invalid operation type: '{op_type}'")

            if 'table' not in op and op_type not in ('drop_index',):
                raise MigrationError(f"invalid operation at index {i}: missing 'table' field")

            # Validate operation-specific requirements
            if op_type == 'create_table':
                if 'columns' not in op:
                    raise MigrationError(f"invalid create_table operation: missing 'columns' field")

            elif op_type == 'add_column':
                if 'column' not in op:
                    raise MigrationError(f"invalid add_column operation: missing 'column' field")

            elif op_type == 'drop_column':
                if 'column' not in op:
                    raise MigrationError(f"invalid drop_column operation: missing 'column' field")

            elif op_type == 'add_foreign_key':
                if 'name' not in op:
                    raise MigrationError(f"invalid add_foreign_key operation: missing 'name' field")
                if 'columns' not in op:
                    raise MigrationError(f"invalid add_foreign_key operation: missing 'columns' field")
                if 'references' not in op:
                    raise MigrationError(f"invalid add_foreign_key operation: missing 'references' field")

            elif op_type == 'drop_foreign_key':
                if 'name' not in op:
                    raise MigrationError(f"invalid drop_foreign_key operation: missing 'name' field")

            elif op_type == 'create_index':
                if 'name' not in op:
                    raise MigrationError(f"invalid create_index operation: missing 'name' field")
                if 'columns' not in op:
                    raise MigrationError(f"invalid create_index operation: missing 'columns' field")

            elif op_type == 'drop_index':
                if 'name' not in op:
                    raise MigrationError(f"invalid drop_index operation: missing 'name' field")

            elif op_type == 'add_check_constraint':
                if 'name' not in op:
                    raise MigrationError(f"invalid add_check_constraint operation: missing 'name' field")
                if 'expression' not in op:
                    raise MigrationError(f"invalid add_check_constraint operation: missing 'expression' field")

            elif op_type == 'drop_check_constraint':
                if 'name' not in op:
                    raise MigrationError(f"invalid drop_check_constraint operation: missing 'name' field")

    def apply_migration(self, migration_path: str) -> None:
        if not Path(migration_path).exists():
            raise MigrationError(f"migration file not found: {migration_path}")

        try:
            with open(migration_path, 'r') as f:
                migration_data = json.load(f)
        except json.JSONDecodeError as e:
            raise MigrationError(f"invalid JSON in migration file: {e}")

        self._validate_migration_file(migration_data)

        version = migration_data['version']
        description = migration_data['description']
        operations = migration_data['operations']

        self._ensure_migrations_table()

        if self._is_migration_applied(version):
            print(
                json.dumps({
                    'event': 'migration_skipped',
                    'version': version,
                    'reason': 'already_applied'
                }),
                file=sys.stdout
            )
            print(f"Warning: Migration version {version} already applied, skipping", file=sys.stderr)
            return

        # Enable foreign keys for this migration
        self.conn.execute("PRAGMA foreign_keys = ON")

        operations_count = 0
        for op in operations:
            op_type = op['type']

            if op_type == 'create_table':
                result = self._create_table(op, version)
            elif op_type == 'add_column':
                result = self._add_column(op, version)
            elif op_type == 'drop_column':
                result = self._drop_column(op, version)
            elif op_type == 'add_foreign_key':
                result = self._add_foreign_key(op, version)
            elif op_type == 'drop_foreign_key':
                result = self._drop_foreign_key(op, version)
            elif op_type == 'create_index':
                result = self._create_index(op, version)
            elif op_type == 'drop_index':
                result = self._drop_index(op, version)
            elif op_type == 'add_check_constraint':
                result = self._add_check_constraint(op, version)
            elif op_type == 'drop_check_constraint':
                result = self._drop_check_constraint(op, version)

            print(json.dumps(result), file=sys.stdout)
            operations_count += 1

        self._record_migration(version, description)

        print(
            json.dumps({
                'event': 'migration_complete',
                'version': version,
                'operations_count': operations_count
            }),
            file=sys.stdout
        )


def main():
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()

    if args.command != 'migrate':
        parser.print_help()
        sys.exit(1)

    tool = MigrationTool(args.database)

    try:
        tool.connect()
        tool.apply_migration(args.migration_file)
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Error: SQL error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        tool.close()


if __name__ == '__main__':
    main()
