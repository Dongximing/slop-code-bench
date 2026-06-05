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
from typing import Any, Dict, List, Optional


class MigrationError(Exception):
    """Custom exception for migration errors."""
    pass


class DependencyGraph:
    """Handles dependency resolution and cycle detection for migrations."""

    # Colors for DFS cycle detection
    WHITE = 0  # Unvisited
    GRAY = 1   # Currently being visited (in recursion stack)
    BLACK = 2  # Completely visited

    def __init__(self):
        self.migrations: Dict[int, Dict[str, Any]] = {}  # version -> migration data
        self.dependencies: Dict[int, List[int]] = {}  # version -> list of dependencies
        self.file_paths: Dict[int, str] = {}  # version -> file path

    def add_migration(self, migration_data: Dict[str, Any], file_path: Optional[str] = None) -> None:
        """Add a migration to the graph."""
        version = migration_data['version']
        if version in self.migrations:
            raise MigrationError(f"duplicate migration version: {version}")

        self.migrations[version] = migration_data
        self.dependencies[version] = migration_data.get('depends_on', [])
        self.file_paths[version] = file_path or ''

    def get_all_versions(self) -> List[int]:
        """Get all migration versions."""
        return list(self.migrations.keys())

    def validate_dependencies(self) -> List[Dict[str, Any]]:
        """Validate all dependencies. Returns list of validation events."""
        events = []

        for version, deps in self.dependencies.items():
            for dep in deps:
                if dep not in self.migrations:
                    raise MigrationError(f"dependency version {dep} not found")
            events.append({
                'event': 'dependency_check',
                'version': version,
                'depends_on': deps,
                'status': 'ok'
            })

        return events

    def validate_no_future_dependencies(self) -> None:
        """Validate that no migration depends on a future version.

        This check is only valid if there are no circular dependencies.
        """
        for version, deps in self.dependencies.items():
            for dep in deps:
                if dep >= version:
                    raise MigrationError(f"migration version {version} cannot depend on future version {dep}")

    def detect_cycle(self) -> Optional[List[int]]:
        """Detect if there's a cycle in the dependency graph.

        Returns the cycle path if found, None otherwise.
        Uses DFS with color marking.
        """
        colors = {v: self.WHITE for v in self.migrations}
        parent = {v: None for v in self.migrations}

        def dfs(node: int) -> Optional[List[int]]:
            colors[node] = self.GRAY

            for dep in self.dependencies.get(node, []):
                if dep not in colors:
                    # Dependency doesn't exist - will be caught by validate_dependencies
                    continue

                if colors[dep] == self.GRAY:
                    # Found a cycle - reconstruct path
                    cycle = [dep]
                    current = node
                    while current != dep:
                        cycle.append(current)
                        current = parent[current]
                    cycle.append(dep)
                    return list(reversed(cycle))

                if colors[dep] == self.WHITE:
                    parent[dep] = node
                    result = dfs(dep)
                    if result:
                        return result

            colors[node] = self.BLACK
            return None

        for version in self.migrations:
            if colors[version] == self.WHITE:
                result = dfs(version)
                if result:
                    return result

        return None

    def topological_sort(self) -> List[int]:
        """Perform topological sort using Kahn's algorithm.

        Returns migrations in dependency order (dependencies first).
        """
        # Build adjacency list: version -> versions that depend on it
        dependents = {v: [] for v in self.migrations}
        for version, deps in self.dependencies.items():
            for dep in deps:
                if dep in dependents:
                    dependents[dep].append(version)

        # Calculate in-degree for each version (number of dependencies)
        in_degree = {v: len(self.dependencies.get(v, [])) for v in self.migrations}

        # Start with versions that have no dependencies
        queue = sorted([v for v in self.migrations if in_degree[v] == 0])
        result = []

        while queue:
            version = queue.pop(0)
            result.append(version)

            # For each version that depends on this one
            for dependent in dependents[version]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    # Insert in sorted order for deterministic results
                    inserted = False
                    for i, v in enumerate(queue):
                        if dependent < v:
                            queue.insert(i, dependent)
                            inserted = True
                            break
                    if not inserted:
                        queue.append(dependent)

        if len(result) != len(self.migrations):
            # There's a cycle
            raise MigrationError("dependency resolution failed: circular dependency")

        return result


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
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                operations TEXT NOT NULL DEFAULT '[]',
                rollback_operations TEXT DEFAULT NULL
            )
        """)
        # For existing databases, try to add the new columns
        try:
            self.conn.execute("ALTER TABLE _migrations ADD COLUMN operations TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            self.conn.execute("ALTER TABLE _migrations ADD COLUMN rollback_operations TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def _is_migration_applied(self, version: int) -> bool:
        cursor = self.conn.execute(
            "SELECT 1 FROM _migrations WHERE version = ?",
            (version,)
        )
        return cursor.fetchone() is not None

    def _record_migration(self, version: int, description: str, operations: List[Dict[str, Any]], rollback_operations: Optional[List[Dict[str, Any]]] = None) -> None:
        operations_json = json.dumps(operations)
        rollback_json = json.dumps(rollback_operations) if rollback_operations else None
        self.conn.execute(
            "INSERT INTO _migrations (version, description, operations, rollback_operations) VALUES (?, ?, ?, ?)",
            (version, description, operations_json, rollback_json)
        )
        self.conn.commit()

    def _get_applied_migrations(self) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            "SELECT version, description, operations, rollback_operations FROM _migrations ORDER BY version"
        )
        migrations = []
        for row in cursor.fetchall():
            rollback_ops = None
            if len(row) > 3 and row[3]:
                rollback_ops = json.loads(row[3])
            migrations.append({
                'version': row[0],
                'description': row[1],
                'operations': json.loads(row[2]),
                'rollback_operations': rollback_ops
            })
        return migrations

    def _remove_migration(self, version: int) -> None:
        self.conn.execute(
            "DELETE FROM _migrations WHERE version = ?",
            (version,)
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
            pattern = r'CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY'
            matches = re.findall(pattern, sql, re.IGNORECASE)
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
        pattern = r'(?:CONSTRAINT\s+["\']?(\w+)["\']?\s+)?CHECK\s*\(([^)]+(?:\([^)]*\)[^)]*)*)\)'
        matches = re.finditer(pattern, sql, re.IGNORECASE)

        constraints = []

        for i, match in enumerate(matches):
            constraints.append({
                'name': match.group(1) if match.group(1) else f'_check_{i}',
                'expression': match.group(2).strip()
            })

        return constraints

    def _get_index_info(self, index_name: str) -> Optional[Dict[str, Any]]:
        """Get information about an index."""
        cursor = self.conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,)
        )
        row = cursor.fetchone()
        if not row or not row[2]:
            return None

        sql = row[2]
        unique = 'UNIQUE' in sql.upper()
        col_pattern = r'\(([^)]+)\)'
        col_match = re.search(col_pattern, sql)
        columns = []
        if col_match:
            col_str = col_match.group(1)
            columns = [c.strip().strip('"').strip("'") for c in col_str.split(',')]

        return {
            'name': row[0],
            'table': row[1],
            'unique': unique,
            'columns': columns,
            'sql': sql
        }

    def _rollback_create_table(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop the table."""
        table_name = operation.get('table')
        self._validate_identifier(table_name, 'table name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Check if any foreign keys reference this table
        all_tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE '_%'"
        ).fetchall()
        for (tbl_name,) in all_tables:
            fks = self._get_foreign_keys(tbl_name)
            for fk in fks:
                if fk['table'] == table_name:
                    raise MigrationError(
                        f"cannot rollback: foreign key constraint violation - "
                        f"table '{tbl_name}' references '{table_name}'"
                    )

        self.conn.execute(f"DROP TABLE {self._quote_identifier(table_name)}")
        self.conn.commit()

        return {
            'event': 'operation_rolled_back',
            'type': 'create_table',
            'table': table_name,
            'version': version
        }

    def _rollback_add_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop the column."""
        table_name = operation.get('table')
        column_name = operation.get('column')
        if isinstance(column_name, dict):
            column_name = column_name.get('name')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(column_name, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use drop_column operation
        drop_op = {'type': 'drop_column', 'table': table_name, 'column': column_name}
        self._drop_column(drop_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'add_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _rollback_drop_column(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Re-add the column (with original definition if stored)."""
        table_name = operation.get('table')
        column_name = operation.get('column')
        original_definition = operation.get('original_definition', {
            'name': column_name,
            'type': 'TEXT',
            'not_null': False
        })

        self._validate_identifier(table_name, 'table name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use add_column operation with original definition
        add_op = {'type': 'add_column', 'table': table_name, 'column': original_definition}
        self._add_column(add_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'drop_column',
            'table': table_name,
            'column': column_name,
            'version': version
        }

    def _rollback_add_foreign_key(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop the foreign key."""
        table_name = operation.get('table')
        fk_name = operation.get('name')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(fk_name, 'foreign key name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use drop_foreign_key operation
        drop_op = {'type': 'drop_foreign_key', 'table': table_name, 'name': fk_name}
        self._drop_foreign_key(drop_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'add_foreign_key',
            'table': table_name,
            'name': fk_name,
            'version': version
        }

    def _rollback_drop_foreign_key(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Re-add the foreign key (requires original definition)."""
        table_name = operation.get('table')
        original_definition = operation.get('original_definition')

        if not original_definition:
            raise MigrationError(
                f"cannot rollback version {version}: missing original_definition for drop_foreign_key"
            )

        self._validate_identifier(table_name, 'table name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use add_foreign_key operation with original definition
        add_op = {
            'type': 'add_foreign_key',
            'table': table_name,
            **original_definition
        }
        self._add_foreign_key(add_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'drop_foreign_key',
            'table': table_name,
            'name': original_definition.get('name'),
            'version': version
        }

    def _rollback_create_index(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop the index."""
        index_name = operation.get('name')

        self._validate_identifier(index_name, 'index name')

        if not self._index_exists(index_name):
            raise MigrationError(f"rollback failed: index '{index_name}' does not exist")

        # Use drop_index operation
        drop_op = {'type': 'drop_index', 'name': index_name}
        self._drop_index(drop_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'create_index',
            'name': index_name,
            'table': operation.get('table'),
            'version': version
        }

    def _rollback_drop_index(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Re-create the index (requires original definition)."""
        index_name = operation.get('name')
        original_definition = operation.get('original_definition')

        if not original_definition:
            raise MigrationError(
                f"cannot rollback version {version}: missing original_definition for drop_index"
            )

        # Use create_index operation with original definition
        create_op = {
            'type': 'create_index',
            'name': index_name,
            **original_definition
        }
        self._create_index(create_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'drop_index',
            'name': index_name,
            'version': version
        }

    def _rollback_add_check_constraint(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Drop the check constraint."""
        table_name = operation.get('table')
        constraint_name = operation.get('name')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(constraint_name, 'constraint name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use drop_check_constraint operation
        drop_op = {'type': 'drop_check_constraint', 'table': table_name, 'name': constraint_name}
        self._drop_check_constraint(drop_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'add_check_constraint',
            'table': table_name,
            'name': constraint_name,
            'version': version
        }

    def _rollback_drop_check_constraint(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Re-add the check constraint (requires original definition)."""
        table_name = operation.get('table')
        constraint_name = operation.get('name')
        original_definition = operation.get('original_definition')

        if not original_definition:
            raise MigrationError(
                f"cannot rollback version {version}: missing original_definition for drop_check_constraint"
            )

        self._validate_identifier(table_name, 'table name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Use add_check_constraint operation with original definition
        add_op = {
            'type': 'add_check_constraint',
            'table': table_name,
            'name': constraint_name,
            **original_definition
        }
        self._add_check_constraint(add_op, version)

        return {
            'event': 'operation_rolled_back',
            'type': 'drop_check_constraint',
            'table': table_name,
            'name': constraint_name,
            'version': version
        }

    def _rollback_transform_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Cannot be automatically rolled back."""
        raise MigrationError(
            f"cannot rollback version {version}: missing rollback_operations for transform_data"
        )

    def _rollback_migrate_column_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Reverse the migration (copy from to_column back to from_column)."""
        table_name = operation.get('table')
        from_column = operation.get('from_column')
        to_column = operation.get('to_column')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(from_column, 'column name')
        self._validate_identifier(to_column, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"rollback failed: table '{table_name}' does not exist")

        # Copy data from to_column back to from_column
        sql = f"""UPDATE {self._quote_identifier(table_name)}
                  SET {self._quote_identifier(from_column)} = {self._quote_identifier(to_column)}"""
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_rolled_back',
            'type': 'migrate_column_data',
            'table': table_name,
            'from_column': to_column,
            'to_column': from_column,
            'version': version
        }

    def _rollback_backfill_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Cannot be automatically rolled back."""
        raise MigrationError(
            f"cannot rollback version {version}: missing rollback_operations for backfill_data"
        )

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

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        if not self._table_exists(ref_table):
            raise MigrationError(
                f"cannot add foreign key: referenced table '{ref_table}' does not exist"
            )

        table_columns = self._get_table_columns(table_name)
        for col in columns:
            self._validate_identifier(col, 'column name')
            if col not in table_columns:
                raise MigrationError(
                    f"cannot add foreign key: column '{col}' does not exist in table '{table_name}'"
                )

        ref_table_columns = self._get_table_columns(ref_table)
        for col in ref_columns:
            self._validate_identifier(col, 'referenced column name')
            if col not in ref_table_columns:
                raise MigrationError(
                    f"cannot add foreign key: referenced column '{ref_table}.{col}' does not exist"
                )

        if len(columns) != len(ref_columns):
            raise MigrationError(
                f"foreign key column count mismatch: {len(columns)} columns reference {len(ref_columns)} columns"
            )

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

        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        fk_col_list = ', '.join(self._quote_identifier(c) for c in columns)
        ref_col_list = ', '.join(self._quote_identifier(c) for c in ref_columns)
        fk_defs.append(
            f"CONSTRAINT {self._quote_identifier(fk_name)} "
            f"FOREIGN KEY ({fk_col_list}) "
            f"REFERENCES {self._quote_identifier(ref_table)} ({ref_col_list}) "
            f"ON DELETE {on_delete_upper} ON UPDATE {on_update_upper}"
        )

        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        all_defs = col_defs + fk_defs + check_defs

        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        for idx in indexes_to_recreate:
            if idx['sql']:
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

        sql = row[0]
        # Look for the constraint name in the SQL (handle both quoted and unquoted identifiers)
        fk_pattern = rf'CONSTRAINT\s+["\']?{re.escape(fk_name)}["\']?\s+FOREIGN\s+KEY'
        if not re.search(fk_pattern, sql, re.IGNORECASE):
            existing_fks = self._get_foreign_keys(table_name)
            found = False
            for i, fk in enumerate(existing_fks):
                if self._get_fk_constraint_name(table_name, i) == fk_name:
                    found = True
                    break
            if not found:
                raise MigrationError(
                    f"foreign key '{fk_name}' does not exist in table '{table_name}'"
                )

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

        fk_defs = []
        fk_pattern = rf'CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY'
        for i, fk in enumerate(existing_fks):
            fk_constraint_name = self._get_fk_constraint_name(table_name, i)
            if fk_constraint_name == fk_name:
                continue

            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        all_defs = col_defs + fk_defs + check_defs

        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        for idx in indexes_to_recreate:
            if idx['sql']:
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

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        if self._index_exists(index_name):
            raise MigrationError(f"index '{index_name}' already exists")

        table_columns = self._get_table_columns(table_name)
        for col in columns:
            self._validate_identifier(col, 'column name')
            if col not in table_columns:
                raise MigrationError(
                    f"cannot create index: column '{col}' does not exist in table '{table_name}'"
                )

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

        existing_checks = self._get_check_constraints(table_name)
        for check in existing_checks:
            if check['name'] == constraint_name:
                raise MigrationError(
                    f"check constraint '{constraint_name}' already exists in table '{table_name}'"
                )

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

        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        check_defs = []
        for check in existing_checks:
            check_defs.append(
                f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
            )

        check_defs.append(
            f"CONSTRAINT {self._quote_identifier(constraint_name)} CHECK ({expression})"
        )

        all_defs = col_defs + fk_defs + check_defs

        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        try:
            self.conn.execute(insert_sql)
        except sqlite3.IntegrityError as e:
            self.conn.rollback()
            self.conn.execute(f"DROP TABLE IF EXISTS {self._quote_identifier(new_table_name)}")
            raise MigrationError(f"check constraint violation: {expression}")

        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        for idx in indexes_to_recreate:
            if idx['sql']:
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

        fk_defs = []
        for fk in existing_fks:
            fk_defs.append(
                f"FOREIGN KEY ({self._quote_identifier(fk['from'])}) "
                f"REFERENCES {self._quote_identifier(fk['table'])} ({self._quote_identifier(fk['to'])}) "
                f"ON DELETE {fk['on_delete']} ON UPDATE {fk['on_update']}"
            )

        check_defs = []
        for check in existing_checks:
            if check['name'] != constraint_name:
                check_defs.append(
                    f"CONSTRAINT {self._quote_identifier(check['name'])} CHECK ({check['expression']})"
                )

        all_defs = col_defs + fk_defs + check_defs

        indexes_to_recreate = self._get_indexes_for_table(table_name)

        create_sql = f"CREATE TABLE {self._quote_identifier(new_table_name)} ({', '.join(all_defs)})"
        self.conn.execute(create_sql)

        col_names = [self._quote_identifier(col['name']) for col in columns_info]
        insert_sql = f"INSERT INTO {self._quote_identifier(new_table_name)} ({', '.join(col_names)}) SELECT {', '.join(col_names)} FROM {self._quote_identifier(table_name)}"
        self.conn.execute(insert_sql)

        drop_sql = f"DROP TABLE {self._quote_identifier(table_name)}"
        self.conn.execute(drop_sql)

        rename_sql = f"ALTER TABLE {self._quote_identifier(new_table_name)} RENAME TO {self._quote_identifier(table_name)}"
        self.conn.execute(rename_sql)

        for idx in indexes_to_recreate:
            if idx['sql']:
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

    def _migrate_column_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Migrate data from one column to another."""
        table_name = operation.get('table')
        from_column = operation.get('from_column')
        to_column = operation.get('to_column')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(from_column, 'column name')
        self._validate_identifier(to_column, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        table_columns = self._get_table_columns(table_name)
        if from_column not in table_columns:
            raise MigrationError(
                f"cannot migrate data: column '{from_column}' does not exist in table '{table_name}'"
            )
        if to_column not in table_columns:
            raise MigrationError(
                f"cannot migrate data: column '{to_column}' does not exist in table '{table_name}'"
            )

        sql = f"""UPDATE {self._quote_identifier(table_name)}
                  SET {self._quote_identifier(to_column)} = {self._quote_identifier(from_column)}"""
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'migrate_column_data',
            'table': table_name,
            'from_column': from_column,
            'to_column': to_column,
            'version': version
        }

    def _transform_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Transform data using SQL expressions."""
        table_name = operation.get('table')
        transformations = operation.get('transformations', [])

        self._validate_identifier(table_name, 'table name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        if not transformations:
            raise MigrationError("transform_data requires 'transformations' array")

        set_clauses = []
        for transform in transformations:
            column = transform.get('column')
            expression = transform.get('expression')
            self._validate_identifier(column, 'column name')
            set_clauses.append(f"{self._quote_identifier(column)} = {expression}")

        sql = f"UPDATE {self._quote_identifier(table_name)} SET {', '.join(set_clauses)}"
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'transform_data',
            'table': table_name,
            'version': version
        }

    def _backfill_data(self, operation: Dict[str, Any], version: int) -> Dict[str, Any]:
        """Backfill data using default values."""
        table_name = operation.get('table')
        column = operation.get('column')
        default_value = operation.get('default')

        self._validate_identifier(table_name, 'table name')
        self._validate_identifier(column, 'column name')

        if not self._table_exists(table_name):
            raise MigrationError(f"invalid operation: table '{table_name}' does not exist")

        sql = f"""UPDATE {self._quote_identifier(table_name)}
                  SET {self._quote_identifier(column)} = {default_value}
                  WHERE {self._quote_identifier(column)} IS NULL"""
        self.conn.execute(sql)
        self.conn.commit()

        return {
            'event': 'operation_applied',
            'type': 'backfill_data',
            'table': table_name,
            'column': column,
            'version': version
        }

    def _validate_migration_file(self, migration_data: Dict[str, Any], check_dependencies: bool = False) -> None:
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

        # Validate depends_on field if present
        depends_on = migration_data.get('depends_on')
        if depends_on is not None:
            if not isinstance(depends_on, list):
                raise MigrationError("invalid migration schema: 'depends_on' must be an array")
            for dep in depends_on:
                if not isinstance(dep, int) or dep <= 0:
                    raise MigrationError(f"invalid dependency version: {dep}")

        valid_operations = {
            'create_table', 'add_column', 'drop_column',
            'add_foreign_key', 'drop_foreign_key',
            'create_index', 'drop_index',
            'add_check_constraint', 'drop_check_constraint',
            'transform_data', 'migrate_column_data', 'backfill_data'
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
        rollback_operations = migration_data.get('rollback_operations')

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
            elif op_type == 'migrate_column_data':
                result = self._migrate_column_data(op, version)
            elif op_type == 'transform_data':
                result = self._transform_data(op, version)
            elif op_type == 'backfill_data':
                result = self._backfill_data(op, version)

            print(json.dumps(result), file=sys.stdout)
            operations_count += 1

        self._record_migration(version, description, operations, rollback_operations)

        print(
            json.dumps({
                'event': 'migration_complete',
                'version': version,
                'operations_count': operations_count
            }),
            file=sys.stdout
        )

    def _get_rollback_operations(self, operation: Dict[str, Any]) -> Dict[str, Any]:
        """Generate rollback operation for a given forward operation."""
        op_type = operation.get('type')

        if op_type == 'create_table':
            return {'type': 'drop_table', 'table': operation.get('table')}
        elif op_type == 'add_column':
            return {'type': 'drop_column', 'table': operation.get('table'), 'column': operation.get('column', {}).get('name', operation.get('column'))}
        elif op_type == 'drop_column':
            return {'type': 'add_column', 'table': operation.get('table'), 'column': operation.get('original_definition', {'name': operation.get('column'), 'type': 'TEXT'})}
        elif op_type == 'add_foreign_key':
            return {'type': 'drop_foreign_key', 'table': operation.get('table'), 'name': operation.get('name')}
        elif op_type == 'drop_foreign_key':
            return {'type': 'add_foreign_key', 'table': operation.get('table'), **operation.get('original_definition', {})}
        elif op_type == 'create_index':
            return {'type': 'drop_index', 'name': operation.get('name')}
        elif op_type == 'drop_index':
            return {'type': 'create_index', 'name': operation.get('name'), **operation.get('original_definition', {})}
        elif op_type == 'add_check_constraint':
            return {'type': 'drop_check_constraint', 'table': operation.get('table'), 'name': operation.get('name')}
        elif op_type == 'drop_check_constraint':
            return {'type': 'add_check_constraint', 'table': operation.get('table'), 'name': operation.get('name'), **operation.get('original_definition', {})}
        elif op_type == 'transform_data':
            return {'type': 'transform_data', 'error': 'cannot_auto_rollback'}
        elif op_type == 'migrate_column_data':
            return {'type': 'migrate_column_data', 'table': operation.get('table'), 'from_column': operation.get('to_column'), 'to_column': operation.get('from_column')}
        elif op_type == 'backfill_data':
            return {'type': 'backfill_data', 'error': 'cannot_auto_rollback'}

        return {'type': 'unknown', 'original': operation}

    def rollback_migration(self, to_version: Optional[int] = None, count: int = 1, migrations_dir: Optional[str] = None) -> None:
        """Rollback migrations."""
        self._ensure_migrations_table()

        applied_migrations = self._get_applied_migrations()

        if not applied_migrations:
            raise MigrationError("no migrations to rollback")

        current_version = max(m['version'] for m in applied_migrations)

        if to_version is not None:
            if to_version < 0:
                raise MigrationError(f"version {to_version} not found")

            versions_to_rollback = [m for m in applied_migrations if m['version'] > to_version]

            if to_version > 0:
                version_exists = any(m['version'] == to_version for m in applied_migrations)
                if not version_exists:
                    raise MigrationError(f"version {to_version} not found")
        else:
            versions_to_rollback = sorted(applied_migrations, key=lambda m: m['version'], reverse=True)[:count]

        if not versions_to_rollback:
            raise MigrationError("no migrations to rollback")

        # Build dependency graph if migrations_dir is provided
        # This allows us to check dependencies during rollback
        graph = None
        if migrations_dir:
            dir_path = Path(migrations_dir)
            if dir_path.exists() and dir_path.is_dir():
                graph = DependencyGraph()
                for m in applied_migrations:
                    graph.add_migration(m, '')

        # Sort in reverse dependency order (dependents first, then dependencies)
        # If we have a graph, use it to order rollbacks properly
        if graph:
            # For rollback, we need to process in reverse topological order
            # i.e., if A depends on B, we must rollback A before B
            # Build a reverse graph where dependencies point to dependents
            dependents_graph = {}
            for v in graph.migrations:
                deps = graph.dependencies.get(v, [])
                for dep in deps:
                    if dep not in dependents_graph:
                        dependents_graph[dep] = []
                    dependents_graph[dep].append(v)

            # Sort versions_to_rollback respecting dependencies
            # Higher versions with dependents still applied should be rolled back first
            def get_rollback_priority(version):
                # Higher priority = rollback first
                # Dependencies (lower versions) should have lower priority
                # so they are rolled back after their dependents
                priority = version
                # Check if any dependent is still applied (won't be rolled back)
                for dep_version in dependents_graph.get(version, []):
                    if dep_version not in [m['version'] for m in versions_to_rollback]:
                        # This version has a dependent that won't be rolled back
                        # This should be an error, but we handle ordering here
                        pass
                return priority

            versions_to_rollback = sorted(versions_to_rollback, key=lambda m: get_rollback_priority(m['version']), reverse=True)
        else:
            versions_to_rollback = sorted(versions_to_rollback, key=lambda m: m['version'], reverse=True)

        rolled_back_versions = []

        self.conn.execute("PRAGMA foreign_keys = ON")

        try:
            for migration in versions_to_rollback:
                version = migration['version']
                description = migration['description']
                operations = migration['operations']

                # Check if any remaining applied migration depends on this version
                remaining_applied = [m for m in self._get_applied_migrations() if m['version'] != version]
                for remaining in remaining_applied:
                    remaining_deps = remaining.get('depends_on', [])
                    if version in remaining_deps:
                        raise MigrationError(
                            f"cannot rollback version {version}: migration {remaining['version']} depends on it"
                        )

                rollback_ops = migration.get('rollback_operations')

                print(json.dumps({
                    'event': 'rollback_started',
                    'version': version,
                    'description': description
                }), file=sys.stdout)

                if rollback_ops:
                    for op in reversed(rollback_ops):
                        result = self._execute_rollback_operation(op, version, is_explicit=True)
                        print(json.dumps(result), file=sys.stdout)
                else:
                    for op in reversed(operations):
                        result = self._execute_rollback_operation(op, version, is_explicit=False)
                        print(json.dumps(result), file=sys.stdout)

                self._remove_migration(version)

                print(json.dumps({
                    'event': 'rollback_complete',
                    'version': version
                }), file=sys.stdout)

                rolled_back_versions.append(version)

        except Exception as e:
            self.conn.rollback()
            raise

        print(json.dumps({
            'event': 'rollback_finished',
            'versions_rolled_back': rolled_back_versions,
            'final_version': min(rolled_back_versions) - 1 if rolled_back_versions else current_version
        }), file=sys.stdout)

    def _execute_rollback_operation(self, operation: Dict[str, Any], version: int, is_explicit: bool = False) -> Dict[str, Any]:
        """Execute a single rollback operation.

        Args:
            operation: The operation to execute
            version: The migration version
            is_explicit: If True, this is from explicit rollback_operations (execute directly)
                        If False, this is from automatic rollback (reverse the operation)
        """
        op_type = operation.get('type')

        if is_explicit:
            if op_type == 'create_table':
                result = self._create_table(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'drop_table':
                table_name = operation.get('table')
                if self._table_exists(table_name):
                    self.conn.execute(f"DROP TABLE {self._quote_identifier(table_name)}")
                    self.conn.commit()
                return {
                    'event': 'operation_rolled_back',
                    'type': 'drop_table',
                    'table': table_name,
                    'version': version
                }
            elif op_type == 'add_column':
                result = self._add_column(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'drop_column':
                result = self._drop_column(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'add_foreign_key':
                result = self._add_foreign_key(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'drop_foreign_key':
                result = self._drop_foreign_key(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'create_index':
                result = self._create_index(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'drop_index':
                result = self._drop_index(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'add_check_constraint':
                result = self._add_check_constraint(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'drop_check_constraint':
                result = self._drop_check_constraint(operation, version)
                result['event'] = 'operation_rolled_back'
                return result
            elif op_type == 'transform_data':
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for transform_data"
                )
            elif op_type == 'migrate_column_data':
                return self._rollback_migrate_column_data(operation, version)
            elif op_type == 'backfill_data':
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for backfill_data"
                )
            else:
                raise MigrationError(f"unknown rollback operation type: {op_type}")
        else:
            # Automatic rollback: reverse the operation
            if op_type == 'create_table':
                return self._rollback_create_table(operation, version)
            elif op_type == 'drop_table':
                # For dropping table in rollback, we need to check if it exists first
                table_name = operation.get('table')
                if self._table_exists(table_name):
                    self.conn.execute(f"DROP TABLE {self._quote_identifier(table_name)}")
                    self.conn.commit()
                return {
                    'event': 'operation_rolled_back',
                    'type': 'create_table',
                    'table': table_name,
                    'version': version
                }
            elif op_type == 'add_column':
                return self._rollback_add_column(operation, version)
            elif op_type == 'drop_column':
                return self._rollback_drop_column(operation, version)
            elif op_type == 'add_foreign_key':
                return self._rollback_add_foreign_key(operation, version)
            elif op_type == 'drop_foreign_key':
                return self._rollback_drop_foreign_key(operation, version)
            elif op_type == 'create_index':
                return self._rollback_create_index(operation, version)
            elif op_type == 'drop_index':
                return self._rollback_drop_index(operation, version)
            elif op_type == 'add_check_constraint':
                return self._rollback_add_check_constraint(operation, version)
            elif op_type == 'drop_check_constraint':
                return self._rollback_drop_check_constraint(operation, version)
            elif op_type == 'transform_data':
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for transform_data"
                )
            elif op_type == 'migrate_column_data':
                return self._rollback_migrate_column_data(operation, version)
            elif op_type == 'backfill_data':
                raise MigrationError(
                    f"cannot rollback version {version}: missing rollback_operations for backfill_data"
                )
            else:
                raise MigrationError(f"unknown rollback operation type: {op_type}")


def scan_migrations_directory(directory: str) -> DependencyGraph:
    """Scan a directory for migration files and build a dependency graph."""
    dir_path = Path(directory)
    if not dir_path.exists():
        raise MigrationError(f"directory not found: {directory}")
    if not dir_path.is_dir():
        raise MigrationError(f"not a directory: {directory}")

    graph = DependencyGraph()
    json_files = sorted(dir_path.glob('*.json'))

    for json_file in json_files:
        try:
            with open(json_file, 'r') as f:
                migration_data = json.load(f)
        except json.JSONDecodeError as e:
            raise MigrationError(f"invalid migration file: {json_file}")

        if 'version' not in migration_data:
            raise MigrationError(f"invalid migration file: {json_file}")

        version = migration_data['version']
        if not isinstance(version, int):
            raise MigrationError(f"invalid migration file: {json_file}")

        print(json.dumps({
            'event': 'migration_discovered',
            'file': str(json_file.name),
            'version': version
        }), file=sys.stdout)

        graph.add_migration(migration_data, str(json_file))

    return graph


def validate_migration_file(migration_file: str, migrations_dir: Optional[str] = None) -> bool:
    """Validate a migration file, optionally checking dependencies."""
    migration_path = Path(migration_file)
    if not migration_path.exists():
        raise MigrationError(f"migration file not found: {migration_file}")

    print(json.dumps({
        'event': 'validation_started',
        'migration_file': migration_file
    }), file=sys.stdout)

    try:
        with open(migration_path, 'r') as f:
            migration_data = json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid migration file: {migration_file}")

    # Validate basic structure
    tool = MigrationTool(':memory:')
    tool._validate_migration_file(migration_data)

    version = migration_data['version']
    depends_on = migration_data.get('depends_on', [])

    # Check dependencies if migrations directory provided
    if migrations_dir and depends_on:
        # Scan directory for other migrations (excluding the one being validated)
        dir_path = Path(migrations_dir)
        if not dir_path.exists():
            raise MigrationError(f"directory not found: {migrations_dir}")
        if not dir_path.is_dir():
            raise MigrationError(f"not a directory: {migrations_dir}")

        graph = DependencyGraph()
        json_files = sorted(dir_path.glob('*.json'))

        for json_file in json_files:
            # Skip the migration file being validated (to avoid duplicate version error)
            if str(json_file.resolve()) == str(migration_path.resolve()):
                continue

            try:
                with open(json_file, 'r') as f:
                    other_migration_data = json.load(f)
            except json.JSONDecodeError as e:
                raise MigrationError(f"invalid migration file: {json_file}")

            if 'version' not in other_migration_data:
                raise MigrationError(f"invalid migration file: {json_file}")

            other_version = other_migration_data['version']
            if not isinstance(other_version, int):
                raise MigrationError(f"invalid migration file: {json_file}")

            print(json.dumps({
                'event': 'migration_discovered',
                'file': str(json_file.name),
                'version': other_version
            }), file=sys.stdout)

            graph.add_migration(other_migration_data, str(json_file))

        # Add the current migration to the graph for cycle detection
        graph.add_migration(migration_data, migration_file)

        # Validate dependencies exist and are not future versions
        for dep in depends_on:
            if dep not in graph.migrations:
                raise MigrationError(f"dependency version {dep} not found")
            # Only check for future dependency if dep > version (not valid circular dependency)
            # This check needs to be deferred until after cycle detection

        print(json.dumps({
            'event': 'dependency_check',
            'version': version,
            'depends_on': depends_on,
            'status': 'ok'
        }), file=sys.stdout)

        # Check for circular dependencies
        cycle = graph.detect_cycle()
        if cycle:
            print(json.dumps({
                'event': 'circular_dependency_detected',
                'cycle': cycle,
                'status': 'error'
            }), file=sys.stdout)
            print(f"Error: circular dependency detected: cycle {cycle}", file=sys.stderr)
            print(json.dumps({
                'event': 'validation_complete',
                'version': version,
                'status': 'invalid'
            }), file=sys.stdout)
            return False

        # Now check for future dependencies (only valid if no cycle)
        for dep in depends_on:
            if dep >= version:
                raise MigrationError(f"migration version {version} cannot depend on future version {dep}")
    elif depends_on and not migrations_dir:
        # Has dependencies but no migrations dir to check against
        print(json.dumps({
            'event': 'dependency_check',
            'version': version,
            'depends_on': depends_on,
            'status': 'warning',
            'message': 'cannot verify dependencies without --migrations-dir'
        }), file=sys.stdout)

    print(json.dumps({
        'event': 'validation_complete',
        'version': version,
        'status': 'valid'
    }), file=sys.stdout)
    return True


def migrate_all(migrations_dir: str, database_path: str) -> None:
    """Apply all migrations from a directory in dependency order."""
    print(json.dumps({
        'event': 'scan_started',
        'directory': migrations_dir
    }), file=sys.stdout)

    # Scan directory for migrations
    graph = scan_migrations_directory(migrations_dir)

    migrations_found = len(graph.migrations)
    print(json.dumps({
        'event': 'scan_complete',
        'migrations_found': migrations_found
    }), file=sys.stdout)

    if migrations_found == 0:
        return

    # Validate dependencies
    try:
        graph.validate_dependencies()
    except MigrationError as e:
        raise e

    # Check for circular dependencies
    cycle = graph.detect_cycle()
    if cycle:
        print(json.dumps({
            'event': 'circular_dependency_detected',
            'cycle': cycle,
            'status': 'error'
        }), file=sys.stdout)
        raise MigrationError(f"circular dependency detected: cycle {cycle}")

    # Validate no future dependencies (only valid after cycle check)
    try:
        graph.validate_no_future_dependencies()
    except MigrationError as e:
        raise e

    # Perform topological sort
    print(json.dumps({
        'event': 'dependency_resolution_started'
    }), file=sys.stdout)

    order = graph.topological_sort()
    print(json.dumps({
        'event': 'dependency_resolved',
        'order': order
    }), file=sys.stdout)

    # Apply migrations in order
    tool = MigrationTool(database_path)
    tool.connect()

    try:
        tool._ensure_migrations_table()

        migrations_applied = 0
        migrations_skipped = 0
        final_version = 0

        for version in order:
            migration_data = graph.migrations[version]
            file_path = graph.file_paths[version]

            # Check if already applied
            if tool._is_migration_applied(version):
                print(json.dumps({
                    'event': 'migration_skipped',
                    'version': version,
                    'reason': 'already_applied'
                }), file=sys.stdout)
                migrations_skipped += 1
                continue

            description = migration_data.get('description', '')

            # Check dependencies are satisfied
            depends_on = migration_data.get('depends_on', [])
            for dep in depends_on:
                if not tool._is_migration_applied(dep):
                    raise MigrationError(
                        f"dependency not satisfied: migration {version} requires migration {dep}"
                    )

            print(json.dumps({
                'event': 'migration_started',
                'version': version,
                'description': description
            }), file=sys.stdout)

            operations = migration_data['operations']
            rollback_operations = migration_data.get('rollback_operations')

            operations_count = 0
            for op in operations:
                op_type = op['type']

                if op_type == 'create_table':
                    result = tool._create_table(op, version)
                elif op_type == 'add_column':
                    result = tool._add_column(op, version)
                elif op_type == 'drop_column':
                    result = tool._drop_column(op, version)
                elif op_type == 'add_foreign_key':
                    result = tool._add_foreign_key(op, version)
                elif op_type == 'drop_foreign_key':
                    result = tool._drop_foreign_key(op, version)
                elif op_type == 'create_index':
                    result = tool._create_index(op, version)
                elif op_type == 'drop_index':
                    result = tool._drop_index(op, version)
                elif op_type == 'add_check_constraint':
                    result = tool._add_check_constraint(op, version)
                elif op_type == 'drop_check_constraint':
                    result = tool._drop_check_constraint(op, version)
                elif op_type == 'migrate_column_data':
                    result = tool._migrate_column_data(op, version)
                elif op_type == 'transform_data':
                    result = tool._transform_data(op, version)
                elif op_type == 'backfill_data':
                    result = tool._backfill_data(op, version)

                print(json.dumps(result), file=sys.stdout)
                operations_count += 1

            tool._record_migration(version, description, operations, rollback_operations)

            print(json.dumps({
                'event': 'migration_complete',
                'version': version,
                'operations_count': operations_count
            }), file=sys.stdout)

            migrations_applied += 1
            final_version = version

        print(json.dumps({
            'event': 'batch_complete',
            'migrations_applied': migrations_applied,
            'migrations_skipped': migrations_skipped,
            'final_version': final_version
        }), file=sys.stdout)

    finally:
        tool.close()


def main():
    parser = argparse.ArgumentParser(description='Database Migration Tool for SQLite')

    subparsers = parser.add_subparsers(dest='command', help='Available commands')

    migrate_parser = subparsers.add_parser('migrate', help='Apply a migration')
    migrate_parser.add_argument('migration_file', help='Path to migration JSON file')
    migrate_parser.add_argument('database', help='Path to SQLite database file')

    rollback_parser = subparsers.add_parser('rollback', help='Rollback migrations')
    rollback_parser.add_argument('database', help='Path to SQLite database file')
    rollback_parser.add_argument('--to-version', type=int, help='Rollback to a specific version (exclusive)')
    rollback_parser.add_argument('--count', type=int, default=1, help='Number of migrations to rollback (default: 1)')
    rollback_parser.add_argument('--migrations-dir', help='Directory containing migration files (for dependency checking)')

    validate_parser = subparsers.add_parser('validate', help='Validate a migration file')
    validate_parser.add_argument('migration_file', help='Path to migration JSON file')
    validate_parser.add_argument('--migrations-dir', help='Directory containing all migration files (for dependency checking)')

    migrate_all_parser = subparsers.add_parser('migrate-all', help='Apply all migrations from a directory')
    migrate_all_parser.add_argument('--migrations-dir', required=True, help='Directory containing migration files')
    migrate_all_parser.add_argument('database', help='Path to SQLite database file')

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == 'migrate':
            tool = MigrationTool(args.database)
            tool.connect()
            try:
                tool.apply_migration(args.migration_file)
            finally:
                tool.close()
        elif args.command == 'rollback':
            tool = MigrationTool(args.database)
            tool.connect()
            try:
                to_version = getattr(args, 'to_version', None)
                count = getattr(args, 'count', 1)
                migrations_dir = getattr(args, 'migrations_dir', None)
                tool.rollback_migration(to_version=to_version, count=count, migrations_dir=migrations_dir)
            finally:
                tool.close()
        elif args.command == 'validate':
            migrations_dir = getattr(args, 'migrations_dir', None)
            validate_migration_file(args.migration_file, migrations_dir)
        elif args.command == 'migrate-all':
            migrate_all(args.migrations_dir, args.database)
        else:
            parser.print_help()
            sys.exit(1)
    except MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except sqlite3.Error as e:
        print(f"Error: SQL error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
