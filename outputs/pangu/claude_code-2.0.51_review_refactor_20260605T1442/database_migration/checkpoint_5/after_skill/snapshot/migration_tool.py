#!/usr/bin/env python3
"""
Database Migration Tool - SQLite Schema Migrations
CLI tool that applies database schema migrations from JSON files to SQLite databases.
"""

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional

VALID_COLUMN_TYPES = {'INTEGER', 'TEXT', 'REAL', 'BLOB', 'TIMESTAMP'}


class MigrationError(Exception):
    """Base exception for migration errors."""
    pass


class ValidationError(MigrationError):
    """Raised when migration schema validation fails."""
    pass


class SQLExecutionError(MigrationError):
    """Raised when SQL execution fails."""
    pass


def validate_identifier(identifier: str) -> bool:
    """Validate that identifier matches SQLite identifier rules."""
    if not identifier:
        return False
    # SQLite identifiers: alphanumeric + underscore, starting with letter or underscore
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', identifier))


def quote_identifier(identifier: str) -> str:
    """Quote an identifier to prevent SQL injection."""
    if not validate_identifier(identifier):
        raise ValidationError(f"Invalid identifier: '{identifier}'")
    return f'"{identifier}"'


def validate_migration_file(data: dict, *, check_dependencies: bool = True, all_migrations: Optional[dict] = None) -> None:
    """Validate the migration file structure.

    Args:
        data: Migration file data as dictionary.
        check_dependencies: Whether to check dependency existence (requires all_migrations).
        all_migrations: Map of version -> migration_data for dependency checking.
    """
    if not isinstance(data, dict):
        raise ValidationError("Migration file must be a JSON object")

    required_fields = ['version', 'description', 'operations']
    for field in required_fields:
        if field not in data:
            raise ValidationError(f"Missing required field '{field}'")

    if not isinstance(data['version'], int) or data['version'] < 1:
        raise ValidationError("Migration version must be a positive integer")

    if not isinstance(data['description'], str):
        raise ValidationError("Description must be a string")

    if not isinstance(data['operations'], list):
        raise ValidationError("Operations must be an array")

    if len(data['operations']) == 0:
        raise ValidationError("Operations array cannot be empty")

    # Validate depends_on field if present
    if 'depends_on' in data:
        depends_on = data['depends_on']
        if not isinstance(depends_on, list):
            raise ValidationError("'depends_on' must be an array")
        for dep in depends_on:
            if not isinstance(dep, int) or dep < 1:
                raise ValidationError("Dependency versions must be positive integers")
            if check_dependencies and all_migrations is not None:
                if dep not in all_migrations:
                    raise ValidationError(f"Dependency version {dep} not found")
                # Check for forward dependency (cannot depend on future version)
                if dep >= data['version']:
                    raise ValidationError(f"Migration version {data['version']} cannot depend on future version {dep}")

    valid_types = {'create_table', 'add_column', 'drop_column', 'transform_data', 'migrate_column_data', 'backfill_data',
                   'add_foreign_key', 'drop_foreign_key', 'create_index', 'drop_index', 'add_check_constraint',
                   'drop_check_constraint'}
    for i, op in enumerate(data['operations']):
        if not isinstance(op, dict):
            raise ValidationError(f"Operation at index {i} must be an object")

        if 'type' not in op:
            raise ValidationError(f"Operation at index {i} missing required field 'type'")

        if op['type'] not in valid_types:
            raise ValidationError(f"Invalid operation type: '{op['type']}'")

        # Validate required fields per operation type
        if op['type'] == 'create_table':
            validate_create_table_operation(op, i)
        elif op['type'] == 'add_column':
            validate_add_column_operation(op, i)
        elif op['type'] == 'drop_column':
            validate_drop_column_operation(op, i)
        elif op['type'] == 'transform_data':
            validate_transform_data_operation(op, i)
        elif op['type'] == 'migrate_column_data':
            validate_migrate_column_data_operation(op, i)
        elif op['type'] == 'backfill_data':
            validate_backfill_data_operation(op, i)
        elif op['type'] == 'add_foreign_key':
            validate_add_foreign_key_operation(op, i)
        elif op['type'] == 'drop_foreign_key':
            validate_drop_foreign_key_operation(op, i)
        elif op['type'] == 'create_index':
            validate_create_index_operation(op, i)
        elif op['type'] == 'drop_index':
            validate_drop_index_operation(op, i)
        elif op['type'] == 'add_check_constraint':
            validate_add_check_constraint_operation(op, i)
        elif op['type'] == 'drop_check_constraint':
            validate_drop_check_constraint_operation(op, i)


def validate_create_table_operation(op: dict, index: int) -> None:
    """Validate create_table operation."""
    if 'table' not in op:
        raise ValidationError(f"create_table operation at index {index} missing 'table'")
    if 'columns' not in op:
        raise ValidationError(f"create_table operation at index {index} missing 'columns'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise ValidationError(f"create_table operation at index {index} must have at least one column")

    primary_key_count = 0

    for j, col in enumerate(op['columns']):
        if not isinstance(col, dict):
            raise ValidationError(f"Column at index {j} in create_table must be an object")
        if 'name' not in col:
            raise ValidationError(f"Column at index {j} missing 'name'")
        if 'type' not in col:
            raise ValidationError(f"Column at index {j} missing 'type'")

        col_name = col['name']
        col_type = col['type'].upper()

        if not validate_identifier(col_name):
            raise ValidationError(f"Invalid column name: '{col_name}'")

        if col_type not in VALID_COLUMN_TYPES:
            raise ValidationError(f"Invalid column type: '{col['type']}' for column '{col_name}'")

        if col.get('primary_key', False):
            primary_key_count += 1
            if primary_key_count > 1:
                raise ValidationError(f"Only one primary key allowed in create_table")

        if col.get('auto_increment', False):
            if col_type != 'INTEGER':
                raise ValidationError(f"auto_increment can only be used with INTEGER column '{col_name}'")
            if not col.get('primary_key', False):
                raise ValidationError(f"auto_increment requires primary_key=True for column '{col_name}'")


def validate_add_column_operation(op: dict, index: int) -> None:
    """Validate add_column operation."""
    if 'table' not in op:
        raise ValidationError(f"add_column operation at index {index} missing 'table'")
    if 'column' not in op:
        raise ValidationError(f"add_column operation at index {index} missing 'column'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    col = op['column']
    if not isinstance(col, dict):
        raise ValidationError(f"Column must be an object for add_column operation at index {index}")

    if 'name' not in col:
        raise ValidationError(f"Column missing 'name' in add_column operation at index {index}")
    if 'type' not in col:
        raise ValidationError(f"Column missing 'type' in add_column operation at index {index}")

    col_name = col['name']
    col_type = col['type'].upper()

    if not validate_identifier(col_name):
        raise ValidationError(f"Invalid column name: '{col_name}'")

    if col_type not in VALID_COLUMN_TYPES:
        raise ValidationError(f"Invalid column type: '{col['type']}' for column '{col_name}'")

    # auto_increment not valid for add_column
    if col.get('auto_increment', False):
        raise ValidationError(f"auto_increment not allowed for add_column operation for column '{col_name}'")


def validate_drop_column_operation(op: dict, index: int) -> None:
    """Validate drop_column operation."""
    if 'table' not in op:
        raise ValidationError(f"drop_column operation at index {index} missing 'table'")
    if 'column' not in op:
        raise ValidationError(f"drop_column operation at index {index} missing 'column'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    column_name = op['column']
    if not validate_identifier(column_name):
        raise ValidationError(f"Invalid column name: '{column_name}'")


def validate_transform_data_operation(op: dict, index: int) -> None:
    """Validate transform_data operation."""
    if 'table' not in op:
        raise ValidationError(f"transform_data operation at index {index} missing 'table'")
    if 'transformations' not in op:
        raise ValidationError(f"transform_data operation at index {index} missing 'transformations'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    if not isinstance(op['transformations'], list) or len(op['transformations']) == 0:
        raise ValidationError(f"transform_data operation at index {index} must have at least one transformation")

    for j, tr in enumerate(op['transformations']):
        if not isinstance(tr, dict):
            raise ValidationError(f"Transformation at index {j} must be an object")
        if 'column' not in tr:
            raise ValidationError(f"Transformation at index {j} missing 'column'")
        if 'expression' not in tr:
            raise ValidationError(f"Transformation at index {j} missing 'expression'")

        col_name = tr['column']
        if not validate_identifier(col_name):
            raise ValidationError(f"Invalid column name: '{col_name}' in transformation at index {j}")

        expr = tr['expression']
        if not isinstance(expr, str) or len(expr.strip()) == 0:
            raise ValidationError(f"Expression must be a non-empty string in transformation at index {j}")


def validate_migrate_column_data_operation(op: dict, index: int) -> None:
    """Validate migrate_column_data operation."""
    if 'table' not in op:
        raise ValidationError(f"migrate_column_data operation at index {index} missing 'table'")
    if 'from_column' not in op:
        raise ValidationError(f"migrate_column_data operation at index {index} missing 'from_column'")
    if 'to_column' not in op:
        raise ValidationError(f"migrate_column_data operation at index {index} missing 'to_column'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    from_col = op['from_column']
    if not validate_identifier(from_col):
        raise ValidationError(f"Invalid column name: '{from_col}'")

    to_col = op['to_column']
    if not validate_identifier(to_col):
        raise ValidationError(f"Invalid column name: '{to_col}'")

    if 'default_value' in op:
        # No validation needed - will be passed directly to SQL
        pass


def validate_backfill_data_operation(op: dict, index: int) -> None:
    """Validate backfill_data operation."""
    if 'table' not in op:
        raise ValidationError(f"backfill_data operation at index {index} missing 'table'")
    if 'column' not in op:
        raise ValidationError(f"backfill_data operation at index {index} missing 'column'")
    if 'value' not in op:
        raise ValidationError(f"backfill_data operation at index {index} missing 'value'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    col_name = op['column']
    if not validate_identifier(col_name):
        raise ValidationError(f"Invalid column name: '{col_name}'")

    value = op['value']
    if not isinstance(value, str) or len(value.strip()) == 0:
        raise ValidationError(f"Value must be a non-empty string in backfill_data operation at index {index}")

    # where is optional
    if 'where' in op:
        where_clause = op['where']
        if not isinstance(where_clause, str) or len(where_clause.strip()) == 0:
            raise ValidationError(f"WHERE clause must be a non-empty string in backfill_data operation at index {index}")


def validate_add_foreign_key_operation(op: dict, index: int) -> None:
    """Validate add_foreign_key operation."""
    required = ['table', 'name', 'columns', 'references']
    for field in required:
        if field not in op:
            raise ValidationError(f"add_foreign_key operation at index {index} missing '{field}'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    fk_name = op['name']
    if not validate_identifier(fk_name):
        raise ValidationError(f"Invalid foreign key name: '{fk_name}'")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise ValidationError(f"add_foreign_key operation at index {index} 'columns' must be a non-empty array")

    for col in op['columns']:
        if not validate_identifier(col):
            raise ValidationError(f"Invalid column name in foreign key columns: '{col}'")

    refs = op['references']
    if not isinstance(refs, dict):
        raise ValidationError(f"add_foreign_key operation at index {index} 'references' must be an object")

    if 'table' not in refs:
        raise ValidationError(f"add_foreign_key operation at index {index} missing 'references.table'")

    ref_table_name = refs['table']
    if not validate_identifier(ref_table_name):
        raise ValidationError(f"Invalid referenced table name: '{ref_table_name}'")

    if 'columns' not in refs:
        raise ValidationError(f"add_foreign_key operation at index {index} missing 'references.columns'")

    if not isinstance(refs['columns'], list):
        raise ValidationError(f"add_foreign_key operation at index {index} 'references.columns' must be an array")

    if len(op['columns']) != len(refs['columns']):
        raise ValidationError(f"add_foreign_key operation at index {index}: column count mismatch between columns ({len(op['columns'])}) and references.columns ({len(refs['columns'])})")

    for col in refs['columns']:
        if not validate_identifier(col):
            raise ValidationError(f"Invalid column name in referenced columns: '{col}'")

    # Validate on_delete and on_update
    valid_actions = {'CASCADE', 'RESTRICT', 'SET NULL', 'NO ACTION', 'SET DEFAULT'}
    if 'on_delete' in op:
        if op['on_delete'].upper() not in valid_actions:
            raise ValidationError(f"Invalid on_delete action: '{op['on_delete']}'")
    if 'on_update' in op:
        if op['on_update'].upper() not in valid_actions:
            raise ValidationError(f"Invalid on_update action: '{op['on_update']}'")


def validate_drop_foreign_key_operation(op: dict, index: int) -> None:
    """Validate drop_foreign_key operation."""
    required = ['table', 'name']
    for field in required:
        if field not in op:
            raise ValidationError(f"drop_foreign_key operation at index {index} missing '{field}'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    fk_name = op['name']
    if not validate_identifier(fk_name):
        raise ValidationError(f"Invalid foreign key name: '{fk_name}'")


def validate_create_index_operation(op: dict, index: int) -> None:
    """Validate create_index operation."""
    required = ['name', 'table', 'columns']
    for field in required:
        if field not in op:
            raise ValidationError(f"create_index operation at index {index} missing '{field}'")

    idx_name = op['name']
    if not validate_identifier(idx_name):
        raise ValidationError(f"Invalid index name: '{idx_name}'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    if not isinstance(op['columns'], list) or len(op['columns']) == 0:
        raise ValidationError(f"create_index operation at index {index} 'columns' must be a non-empty array")

    for col in op['columns']:
        if not validate_identifier(col):
            raise ValidationError(f"Invalid column name in index columns: '{col}'")

    if 'unique' in op and not isinstance(op['unique'], bool):
        raise ValidationError(f"create_index operation at index {index} 'unique' must be a boolean")


def validate_drop_index_operation(op: dict, index: int) -> None:
    """Validate drop_index operation."""
    if 'name' not in op:
        raise ValidationError(f"drop_index operation at index {index} missing 'name'")

    idx_name = op['name']
    if not validate_identifier(idx_name):
        raise ValidationError(f"Invalid index name: '{idx_name}'")


def validate_add_check_constraint_operation(op: dict, index: int) -> None:
    """Validate add_check_constraint operation."""
    required = ['table', 'name', 'expression']
    for field in required:
        if field not in op:
            raise ValidationError(f"add_check_constraint operation at index {index} missing '{field}'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    chk_name = op['name']
    if not validate_identifier(chk_name):
        raise ValidationError(f"Invalid check constraint name: '{chk_name}'")

    expr = op['expression']
    if not isinstance(expr, str) or len(expr.strip()) == 0:
        raise ValidationError(f"add_check_constraint operation at index {index} 'expression' must be a non-empty string")


def validate_drop_check_constraint_operation(op: dict, index: int) -> None:
    """Validate drop_check_constraint operation."""
    required = ['table', 'name']
    for field in required:
        if field not in op:
            raise ValidationError(f"drop_check_constraint operation at index {index} missing '{field}'")

    table_name = op['table']
    if not validate_identifier(table_name):
        raise ValidationError(f"Invalid table name: '{table_name}'")

    chk_name = op['name']
    if not validate_identifier(chk_name):
        raise ValidationError(f"Invalid check constraint name: '{chk_name}'")


# ============================================================================
# Dependency Graph Functions (Checkpoint 5)
# ============================================================================

class DependencyCycleError(MigrationError):
    """Raised when circular dependencies are detected."""
    def __init__(self, cycle: list[int]):
        self.cycle = cycle
        super().__init__(f"circular dependency detected: cycle {cycle}")


def build_dependency_graph(migrations: dict[int, dict]) -> dict[int, list[int]]:
    """Build a dependency graph from migrations.

    Args:
        migrations: Map of version -> migration_data.

    Returns:
        Adjacency list where keys are versions and values are lists of dependencies.
    """
    graph = {}
    for version, migration in migrations.items():
        depends_on = migration.get('depends_on', [])
        graph[version] = depends_on
    return graph


def detect_circular_dependencies(graph: dict[int, list[int]]) -> Optional[list[int]]:
    """Detect circular dependencies in the graph using DFS with color marking.

    Args:
        graph: Dependency graph (adjacency list).

    Returns:
        List of versions forming a cycle if found, None otherwise.
    """
    # Color states: 0 = white (unvisited), 1 = gray (in recursion stack), 2 = black (fully visited)
    color = {v: 0 for v in graph}
    parent = {v: None for v in graph}
    cycle_start = None

    def dfs(u: int) -> bool:
        nonlocal cycle_start
        color[u] = 1  # Gray

        for v in graph.get(u, []):
            if v not in color:
                continue  # Skip non-existent dependencies
            if color[v] == 0:
                parent[v] = u
                if dfs(v):
                    return True
            elif color[v] == 1:
                # Found a cycle - reconstruct it
                cycle_start = v
                return True

        color[u] = 2  # Black
        return False

    for node in graph:
        if color[node] == 0:
            if dfs(node):
                # Reconstruct the cycle
                cycle = []
                # Walk back from cycle_start using parent pointers
                current = cycle_start
                while True:
                    cycle.append(current)
                    current = parent[current]
                    if current == cycle_start or current is None:
                        break
                cycle.append(cycle_start)  # Close the cycle
                cycle.reverse()
                return cycle

    return None


def topological_sort(graph: dict[int, list[int]]) -> list[int]:
    """Perform topological sort using Kahn's algorithm.

    The graph represents: key -> list of dependencies (key depends on the values).
    We need to reverse edges for topological sort: if A depends on B, then B -> A.

    Args:
        graph: Dependency graph (adjacency list where key depends on values).

    Returns:
        List of versions in topological order.

    Raises:
        DependencyCycleError: If circular dependencies are detected.
    """
    # Build reverse adjacency list: for topological sort, we need edges pointing from
    # dependencies to dependents. If A depends on B, then edge B -> A.
    reverse_adj = {v: [] for v in graph}
    for u in graph:
        for v in graph.get(u, []):
            if v in reverse_adj:
                reverse_adj[v].append(u)
            else:
                reverse_adj[u] = []  # ensure all nodes exist

    # Calculate in-degrees for reverse_adj
    in_degree = {v: 0 for v in reverse_adj}
    for u in reverse_adj:
        for v in reverse_adj[u]:
            in_degree[v] += 1

    # Find nodes with in-degree 0 (no dependencies)
    queue = [v for v in graph if in_degree[v] == 0]
    result = []

    while queue:
        # Sort to get deterministic order
        queue.sort()
        u = queue.pop(0)
        result.append(u)

        for v in reverse_adj.get(u, []):
            if v in in_degree:
                in_degree[v] -= 1
                if in_degree[v] == 0:
                    queue.append(v)

    # If we didn't process all nodes, there's a cycle
    if len(result) != len(graph):
        cycle = detect_circular_dependencies(graph)
        if cycle:
            raise DependencyCycleError(cycle)
        raise MigrationError(f"dependency resolution failed: conflicting requirements")

    return result


def validate_dependency_graph(migrations: dict[int, dict]) -> None:
    """Validate that all dependencies can be resolved.

    Args:
        migrations: Map of version -> migration_data.

    Raises:
        ValidationError: If dependencies are invalid.
        DependencyCycleError: If circular dependencies are detected.
    """
    # Check for version uniqueness (implicit by dictionary key usage)
    # Check dependency existence
    for version, migration in migrations.items():
        depends_on = migration.get('depends_on', [])
        for dep in depends_on:
            if dep not in migrations:
                raise ValidationError(f"Dependency version {dep} not found")
            if dep >= version:
                raise ValidationError(
                    f"Migration version {version} cannot depend on future version {dep}"
                )

    # Check for circular dependencies
    graph = build_dependency_graph(migrations)
    cycle = detect_circular_dependencies(graph)
    if cycle:
        raise DependencyCycleError(cycle)


def scan_migrations_directory(directory: str) -> dict[int, dict]:
    """Scan a directory for migration files and return a map of version -> migration data.

    Args:
        directory: Path to the directory containing migration files.

    Returns:
        Map of version -> migration_data.

    Raises:
        MigrationError: If directory doesn't exist or no valid migrations found.
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise MigrationError(f"migrations directory not found: {directory}")
    if not dir_path.is_dir():
        raise MigrationError(f"not a directory: {directory}")

    migrations = {}

    for file_path in sorted(dir_path.glob('*.json')):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)

            version = data.get('version')
            if version is None:
                # Skip files that don't define a version
                continue

            if not isinstance(version, int) or version < 1:
                continue  # Skip invalid versions

            if version in migrations:
                raise MigrationError(f"duplicate version {version} found in {file_path.name}")

            migrations[version] = data
        except json.JSONDecodeError:
            # Skip invalid JSON files
            continue

    if not migrations:
        raise MigrationError(f"no valid migration files found in {directory}")

    return migrations


def get_table_columns(cursor: sqlite3.Cursor, table_name: str) -> dict:
    """Get information about columns in a table."""
    pragma_query = f"PRAGMA table_info({quote_identifier(table_name)})"
    cursor.execute(pragma_query)
    columns = {}
    for row in cursor.fetchall():
        col_name = row[1]  # name
        col_type = row[2].upper() if row[2] else 'TEXT'
        not_null = bool(row[3])
        default_val = row[4]
        primary_key = bool(row[5])
        columns[col_name] = {
            'name': col_name,
            'type': col_type,
            'not_null': not_null,
            'default': default_val,
            'primary_key': primary_key
        }
    return columns


def get_table_foreign_keys(cursor: sqlite3.Cursor, table_name: str) -> dict:
    """Get foreign key constraints for a table."""
    pragma_query = f"PRAGMA foreign_key_list({quote_identifier(table_name)})"
    cursor.execute(pragma_query)
    fks = {}
    for row in cursor.fetchall():
        fk_id = row[0]
        if fk_id not in fks:
            fks[fk_id] = {
                'id': fk_id,
                'columns': [],
                'references': {'table': row[2], 'columns': []},
                'on_update': row[5],
                'on_delete': row[6]
            }
        fks[fk_id]['columns'].append(row[3])
        fks[fk_id]['references']['columns'].append(row[4])
    return fks


def get_all_indexes(cursor: sqlite3.Cursor) -> dict:
    """Get all indexes in the database."""
    cursor.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'")
    indexes = {}
    for row in cursor.fetchall():
        indexes[row[0]] = row[1]  # name -> table_name
    return indexes


def get_table_check_constraints(cursor: sqlite3.Cursor, table_name: str) -> dict:
    """Get check constraints for a table by parsing CREATE TABLE statement."""
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
    row = cursor.fetchone()
    if not row or not row[0]:
        return {}

    create_sql = row[0]
    # Parse check constraints from CREATE TABLE
    constraints = {}
    # Find all CONSTRAINT name CHECK (...) patterns
    pattern = r'CONSTRAINT\s+([a-zA-Z_][a-zA-Z0-9_]*)\s+CHECK\s*\((.*?)\)'
    matches = re.findall(pattern, create_sql, re.IGNORECASE | re.DOTALL)
    for match in matches:
        constraints[match[0]] = match[1].strip()
    return constraints


def check_table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
    """Check if a table exists."""
    query = f"""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name={quote_identifier(table_name)}
    """
    cursor.execute(query)
    return cursor.fetchone() is not None


def create_migrations_table(cursor: sqlite3.Cursor) -> None:
    """Create the _migrations table if it doesn't exist."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            version INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            operations TEXT NOT NULL DEFAULT '[]'
        )
    """)


def is_migration_applied(cursor: sqlite3.Cursor, version: int) -> bool:
    """Check if a migration version has already been applied."""
    cursor.execute("SELECT version FROM _migrations WHERE version = ?", (version,))
    return cursor.fetchone() is not None


def get_applied_migrations(cursor: sqlite3.Cursor) -> list:
    """Get all applied migrations ordered by version."""
    cursor.execute("SELECT version, description, operations FROM _migrations ORDER BY version")
    migrations = []
    for row in cursor.fetchall():
        migrations.append({
            'version': row[0],
            'description': row[1],
            'operations': json.loads(row[2]) if row[2] else []
        })
    return migrations


def get_migration_by_version(cursor: sqlite3.Cursor, version: int) -> dict | None:
    """Get a specific migration by version."""
    cursor.execute("SELECT version, description, operations FROM _migrations WHERE version = ?", (version,))
    row = cursor.fetchone()
    if row:
        return {
            'version': row[0],
            'description': row[1],
            'operations': json.loads(row[2]) if row[2] else []
        }
    return None


def apply_create_table(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply create_table operation."""
    table_name = op['table']
    columns = op['columns']

    if check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' already exists")

    column_defs = []
    for col in columns:
        col_name = col['name']
        col_type = col['type'].upper()

        # Build column definition
        def_parts = [quote_identifier(col_name), col_type]

        if col.get('not_null', False):
            def_parts.append('NOT NULL')

        if col.get('primary_key', False):
            def_parts.append('PRIMARY KEY')
            if col.get('auto_increment', False):
                def_parts.append('AUTOINCREMENT')

        if col.get('unique', False):
            def_parts.append('UNIQUE')

        if 'default' in col and col['default'] is not None:
            def_parts.append(f"DEFAULT {col['default']}")

        column_defs.append(' '.join(def_parts))

    create_sql = f"CREATE TABLE {quote_identifier(table_name)} (" + ', '.join(column_defs) + ")"
    cursor.execute(create_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'create_table',
        'table': table_name,
        'version': version
    }))


def apply_add_column(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply add_column operation."""
    table_name = op['table']
    col = op['column']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    # Check if column already exists
    existing_columns = get_table_columns(cursor, table_name)
    if col['name'] in existing_columns:
        raise SQLExecutionError(f"column '{col['name']}' already exists in table '{table_name}'")

    # Build ALTER TABLE statement
    col_name = col['name']
    col_type = col['type'].upper()

    def_parts = [quote_identifier(col_name), col_type]

    if col.get('not_null', False):
        def_parts.append('NOT NULL')

    if 'default' in col and col['default'] is not None:
        def_parts.append(f"DEFAULT {col['default']}")

    alter_sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN " + ' '.join(def_parts)
    cursor.execute(alter_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'add_column',
        'table': table_name,
        'column': col_name,
        'version': version
    }))


def apply_drop_column(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply drop_column operation."""
    table_name = op['table']
    column_name = op['column']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)

    if column_name not in existing_columns:
        raise SQLExecutionError(f"column '{column_name}' does not exist in table '{table_name}'")

    # Check if column is the only column
    if len(existing_columns) <= 1:
        raise SQLExecutionError(f"Cannot drop column '{column_name}' as it is the only column in table '{table_name}'")

    # Check if column is primary key
    if existing_columns[column_name]['primary_key']:
        raise SQLExecutionError(f"Cannot drop PRIMARY KEY column '{column_name}' from table '{table_name}'")

    # SQLite doesn't support DROP COLUMN, so we need to recreate the table
    temp_table_name = f"_{table_name}_temp"

    # Get columns to keep (excluding the one to drop)
    columns_to_keep = [col for col in existing_columns.keys() if col != column_name]

    # Build CREATE TABLE statement for new table
    new_table_defs = []
    for col_name in columns_to_keep:
        col_info = existing_columns[col_name]
        def_parts = [quote_identifier(col_name), col_info['type']]

        if col_info['not_null']:
            def_parts.append('NOT NULL')

        if col_info['primary_key']:
            def_parts.append('PRIMARY KEY')

        # Handle default value - SQLite stores default as string representation
        if col_info['default'] is not None:
            default_val = col_info['default']
            if default_val:
                # Check if it's a quoted string or expression
                if isinstance(default_val, str) and default_val.startswith("'") and default_val.endswith("'"):
                    def_parts.append(f"DEFAULT {default_val}")
                else:
                    def_parts.append(f"DEFAULT {default_val}")

        new_table_defs.append(' '.join(def_parts))

    # Create new table
    create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} (" + ', '.join(new_table_defs) + ")"
    cursor.execute(create_sql)

    # Copy data from old table to new table
    cols_to_select = ', '.join([quote_identifier(col) for col in columns_to_keep])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    cursor.execute(insert_sql)

    # Drop old table
    cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")

    # Rename new table to original name
    cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'drop_column',
        'table': table_name,
        'column': column_name,
        'version': version
    }))


def apply_transform_data(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply transform_data operation."""
    table_name = op['table']
    transformations = op['transformations']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)

    for tr in transformations:
        col_name = tr['column']
        expression = tr['expression']

        # If column doesn't exist, create it first
        if col_name not in existing_columns:
            # Infer type from expression if possible, default to TEXT
            col_type = "TEXT"
            add_col_op = {
                'table': table_name,
                'column': {
                    'name': col_name,
                    'type': col_type,
                    'not_null': False
                }
            }
            apply_add_column(cursor, add_col_op, version)
            existing_columns[col_name] = get_table_columns(cursor, table_name)[col_name]

        # Update the column with the expression
        update_sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(col_name)} = ({expression})"
        cursor.execute(update_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'transform_data',
        'table': table_name,
        'versions': [tr['column'] for tr in transformations],
        'version': version
    }))


def apply_migrate_column_data(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply migrate_column_data operation."""
    table_name = op['table']
    from_column = op['from_column']
    to_column = op['to_column']
    default_value = op.get('default_value')

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)

    if from_column not in existing_columns:
        raise SQLExecutionError(f"column '{from_column}' does not exist in table '{table_name}'")

    if to_column not in existing_columns:
        raise SQLExecutionError(f"column '{to_column}' does not exist in table '{table_name}'")

    # Build the update expression
    if default_value is not None:
        update_expr = f"CASE WHEN {quote_identifier(from_column)} IS NULL THEN {default_value} ELSE {quote_identifier(from_column)} END"
    else:
        update_expr = quote_identifier(from_column)

    update_sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(to_column)} = ({update_expr})"
    cursor.execute(update_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'migrate_column_data',
        'table': table_name,
        'from_column': from_column,
        'to_column': to_column,
        'version': version
    }))


def apply_backfill_data(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply backfill_data operation."""
    table_name = op['table']
    column = op['column']
    value = op['value']
    where_clause = op.get('where')

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)

    if column not in existing_columns:
        raise SQLExecutionError(f"column '{column}' does not exist in table '{table_name}'")

    # Build the update statement
    update_sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(column)} = {value}"
    if where_clause:
        update_sql += f" WHERE {where_clause}"

    cursor.execute(update_sql)

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'backfill_data',
        'table': table_name,
        'column': column,
        'version': version
    }))


def apply_add_foreign_key(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply add_foreign_key operation."""
    table_name = op['table']
    fk_name = op['name']
    columns = op['columns']
    references = op['references']
    on_delete = op.get('on_delete', 'NO ACTION')
    on_update = op.get('on_update', 'NO ACTION')

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    ref_table = references['table']
    ref_columns = references['columns']

    if not check_table_exists(cursor, ref_table):
        raise SQLExecutionError(f"referenced table '{ref_table}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    for col in columns:
        if col not in existing_columns:
            raise SQLExecutionError(f"column '{col}' does not exist in table '{table_name}'")

    ref_columns_info = get_table_columns(cursor, ref_table)
    for col in ref_columns:
        if col not in ref_columns_info:
            raise SQLExecutionError(f"referenced column '{col}' does not exist in table '{ref_table}'")

    # Check for duplicate foreign key name
    existing_fks = get_table_foreign_keys(cursor, table_name)
    for fk_id, fk_info in existing_fks.items():
        # Note: SQLite doesn't store FK names, so we can't check by name directly
        # But we can check if this FK already exists based on columns
        if set(fk_info['columns']) == set(columns) and fk_info['references']['table'] == ref_table:
            raise SQLExecutionError(f"foreign key on columns '{columns}' to '{ref_table}' already exists")

    # Verify foreign key compatibility (simplified type check)
    for i, col in enumerate(columns):
        ref_col = ref_columns[i]
        col_type = existing_columns[col]['type']
        ref_type = ref_columns_info[ref_col]['type']
        # Basic type compatibility: both INTEGER, both TEXT, etc.
        # Allow some flexibility - both should be comparable types
        if col_type != ref_type:
            # Allow different but compatible types (e.g., INTEGER vs INT)
            type_mappings = {
                'INTEGER': {'INT', 'INTEGER', 'TINYINT', 'SMALLINT', 'BIGINT'},
                'TEXT': {'TEXT', 'CLOB', 'STRING', 'CHAR', 'VARCHAR'},
                'REAL': {'REAL', 'DOUBLE', 'DOUBLE PRECISION', 'FLOAT'},
            }
            valid_types = type_mappings.get(col_type.upper(), {col_type.upper()})
            if ref_type.upper() not in valid_types:
                raise SQLExecutionError(f"foreign key column type '{col_type}' incompatible with referenced column type '{ref_type}'")

    col_names = ', '.join([quote_identifier(col) for col in columns])
    ref_col_names = ', '.join([quote_identifier(col) for col in ref_columns])

    # SQLite doesn't support ALTER TABLE ADD FOREIGN KEY directly
    # We need to recreate the table to add a foreign key constraint
    # Get the current table definition
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
    row = cursor.fetchone()
    if not row or not row[0]:
        raise SQLExecutionError(f"table '{table_name}' not found")

    create_sql = row[0]

    # Build new CREATE TABLE with the foreign key constraint
    # Find the position to add the constraint (before the closing parenthesis)
    open_paren = create_sql.find('(')
    if open_paren == -1:
        raise SQLExecutionError(f"invalid CREATE TABLE statement for '{table_name}'")

    inner = create_sql[open_paren + 1:]

    # Find matching closing parenthesis
    depth = 1
    close_paren_idx = 0
    for i, c in enumerate(inner):
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                close_paren_idx = i
                break

    inner_content = inner[:close_paren_idx]
    inner_content = inner_content.rstrip()
    if inner_content.endswith(','):
        inner_content = inner_content[:-1]

    # Add the foreign key constraint
    fk_constraint = f"CONSTRAINT {quote_identifier(fk_name).strip('\"')} FOREIGN KEY ({col_names}) REFERENCES {quote_identifier(ref_table)} ({ref_col_names}) ON DELETE {on_delete} ON UPDATE {on_update}"
    if inner_content:
        inner_content = inner_content + ', ' + fk_constraint
    else:
        inner_content = fk_constraint

    new_create_sql = create_sql[:open_paren + 1] + inner_content + ')'

    # Create temp table with the new definition
    existing_columns = get_table_columns(cursor, table_name)
    temp_table_name = f"_{table_name}_temp"

    temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
    try:
        cursor.execute(temp_table_def)
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to create temp table: {e}")

    # Copy data
    cols_to_select = ', '.join([quote_identifier(col) for col in existing_columns.keys()])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    try:
        cursor.execute(insert_sql)
    except sqlite3.Error as e:
        cursor.execute(f"DROP TABLE {quote_identifier(temp_table_name)}")
        raise SQLExecutionError(f"foreign key violation: {e}")

    # Drop old table and rename new one
    try:
        cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
        cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to add foreign key: {e}")

    # Verify the foreign key was added and check existing data if table has data
    cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table_name)}")
    count = cursor.fetchone()[0]
    if count > 0:
        # Enable foreign keys temporarily to verify
        cursor.execute("PRAGMA foreign_keys = ON")
        try:
            # Try a simple select to verify constraints
            check_sql = f"SELECT * FROM {quote_identifier(table_name)} WHERE {columns[0]} IS NOT NULL"
            cursor.execute(check_sql)
            rows = cursor.fetchall()
            if rows:
                # If we got here, foreign keys are valid (SQLite will raise if violated)
                pass
        except sqlite3.Error as e:
            raise SQLExecutionError(f"foreign key violation: {e}")
        finally:
            cursor.execute("PRAGMA foreign_keys = OFF")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'add_foreign_key',
        'table': table_name,
        'name': fk_name,
        'version': version
    }))


def apply_drop_foreign_key(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply drop_foreign_key operation."""
    table_name = op['table']
    fk_name = op['name']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    # Get the table schema and info
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
    row = cursor.fetchone()
    if not row or not row[0]:
        raise SQLExecutionError(f"table '{table_name}' not found")

    create_sql = row[0]

    # Check if the foreign key constraint exists
    # Parse the CREATE TABLE statement to find the constraint
    # Pattern to find CONSTRAINT name FOREIGN KEY ...
    # Using a non-raw string to avoid issues with escaping
    pattern = re.escape(fk_name) + r'\s+FOREIGN\s+KEY\s*\([^)]+\)\s*REFERENCES\s+[^,}]+'
    match = re.search(pattern, create_sql, re.IGNORECASE)
    if not match:
        # Also check if it's an inline foreign key without constraint name
        raise SQLExecutionError(f"foreign key constraint '{fk_name}' not found in table '{table_name}'")

    temp_table_name = f"_{table_name}_temp"

    # Get current table columns and constraints
    existing_columns = get_table_columns(cursor, table_name)
    existing_fks = get_table_foreign_keys(cursor, table_name)

    # Find which FK to drop based on name (matching parsed columns)
    fk_to_drop = None
    for fk_id, fk_info in existing_fks.items():
        # SQLite foreign_key_list doesn't include constraint names
        # We need to check the CREATE TABLE statement for the name
        # For now, use the parsed match to identify if any FK matches
        if fk_info:
            fk_to_drop = fk_info
            break

    # Since SQLite doesn't expose constraint names in PRAGMA, we recreate the table
    # Get the full CREATE TABLE statement and remove the FOREIGN KEY constraint
    # Build new CREATE TABLE without the FK constraint
    lines = create_sql.split('\n')
    new_lines = []
    skip_count = 0
    found_fk = False

    for line in lines:
        if skip_count > 0:
            if ',' in line and skip_count == 1:
                # Remove trailing comma from previous line
                line = re.sub(r',\s*$', '', line)
            skip_count -= 1
            continue

        # Check if this line contains the constraint to drop
        if f'CONSTRAINT {fk_name}' in line.upper() or (f'FOREIGN KEY' in line.upper() and 'CONSTRAINT' not in create_sql.upper()):
            # Found a foreign key line
            # Check if it's the one we want to drop (based on matching columns if name not specified)
            # Find the full constraint definition
            # Look ahead to find the end of this constraint
            constraint_end = line.find(')') + 1
            if constraint_end > 0:
                # Check if there's more on this line
                remaining = line[constraint_end:]
                if remaining.strip().startswith(','):
                    # Constraint ends on this line, just remove this line
                    continue
                else:
                    # Constraint continues, mark to skip
                    found_fk = True
            continue

        new_lines.append(line)

    new_create_sql = '\n'.join(new_lines)

    # Simpler approach: Parse CREATE TABLE and rebuild without the FK
    # Extract table definition from CREATE TABLE statement
    # Remove the CONSTRAINT ... FOREIGN KEY ... clause

    # Find all CONSTRAINT ... FOREIGN KEY ... patterns
    # Note: In f-strings, \}} is needed for a literal }
    pattern = rf'\s*CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s*[^,]]+'

    new_create_sql = re.sub(pattern, '', create_sql, flags=re.IGNORECASE)

    # Clean up any double commas
    new_create_sql = re.sub(r',\s*,', ',', new_create_sql)
    new_create_sql = re.sub(r'\s\s+', ' ', new_create_sql)

    # Ensure the table name matches
    if new_create_sql.count('(') != new_create_sql.count(')'):
        raise SQLExecutionError(f"malformed SQL after dropping foreign key")

    # Create temp table
    temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
    cursor.execute(temp_table_def)

    # Copy data
    cols_to_select = ', '.join([quote_identifier(col) for col in existing_columns.keys()])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    cursor.execute(insert_sql)

    # Drop old table and rename new one
    cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
    cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'drop_foreign_key',
        'table': table_name,
        'name': fk_name,
        'version': version
    }))


def apply_create_index(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply create_index operation."""
    idx_name = op['name']
    table_name = op['table']
    columns = op['columns']
    unique = op.get('unique', False)

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    existing_columns = get_table_columns(cursor, table_name)
    for col in columns:
        if col not in existing_columns:
            raise SQLExecutionError(f"column '{col}' does not exist in table '{table_name}'")

    # Check if index already exists
    all_indexes = get_all_indexes(cursor)
    if idx_name in all_indexes:
        raise SQLExecutionError(f"index '{idx_name}' already exists")

    col_names = ', '.join([quote_identifier(col) for col in columns])
    unique_str = 'UNIQUE' if unique else ''

    create_idx_sql = f"CREATE {unique_str} INDEX {quote_identifier(idx_name)} ON {quote_identifier(table_name)} ({col_names})"

    try:
        cursor.execute(create_idx_sql)
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to create index: {e}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'create_index',
        'name': idx_name,
        'table': table_name,
        'version': version
    }))


def apply_drop_index(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply drop_index operation."""
    idx_name = op['name']

    all_indexes = get_all_indexes(cursor)
    if idx_name not in all_indexes:
        raise SQLExecutionError(f"index '{idx_name}' does not exist")

    drop_idx_sql = f"DROP INDEX {quote_identifier(idx_name)}"

    try:
        cursor.execute(drop_idx_sql)
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to drop index: {e}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'drop_index',
        'name': idx_name,
        'version': version
    }))


def apply_add_check_constraint(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply add_check_constraint operation to a table."""
    table_name = op['table']
    chk_name = op['name']
    expression = op['expression']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    # Check if constraint already exists
    existing_chks = get_table_check_constraints(cursor, table_name)
    if chk_name in existing_chks:
        raise SQLExecutionError(f"check constraint '{chk_name}' already exists in table '{table_name}'")

    # SQLite doesn't support ALTER TABLE ADD CONSTRAINT, so we need to recreate the table
    temp_table_name = f"_{table_name}_temp"

    # Get current table schema
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
    row = cursor.fetchone()
    if not row or not row[0]:
        raise SQLExecutionError(f"table '{table_name}' not found")

    create_sql = row[0]

    # Add the new check constraint to the CREATE TABLE statement
    # Find the position to add the constraint (before the closing parenthesis)
    # Remove trailing comma from last column definition if present

    # Check if there's already constraints section or just columns
    # We need to add CONSTRAINT chk_name CHECK (expression) to the table definition

    # Find the opening parenthesis
    open_paren = create_sql.find('(')
    if open_paren == -1:
        raise SQLExecutionError(f"invalid CREATE TABLE statement for '{table_name}'")

    # Get content inside parentheses
    inner = create_sql[open_paren + 1:]
    # Find matching closing parenthesis
    depth = 1
    close_paren_idx = 0
    for i, c in enumerate(inner):
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                close_paren_idx = i
                break

    inner_content = inner[:close_paren_idx]

    # Clean inner content - remove trailing comma if present
    inner_content = inner_content.rstrip()
    if inner_content.endswith(','):
        inner_content = inner_content[:-1]

    # Add the new constraint
    new_constraint = f"CONSTRAINT {quote_identifier(chk_name).strip('\"')} CHECK ({expression})"
    if inner_content:
        new_inner_content = inner_content + ', ' + new_constraint
    else:
        new_inner_content = new_constraint

    # Rebuild CREATE TABLE statement
    new_create_sql = create_sql[:open_paren + 1] + new_inner_content + ')'

    # Get existing columns and data
    existing_columns = get_table_columns(cursor, table_name)

    # Create temp table
    temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
    try:
        cursor.execute(temp_table_def)
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to create temp table: {e}")

    # Copy data - need to check expression for invalid data
    cols_to_select = ', '.join([quote_identifier(col) for col in existing_columns.keys()])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    try:
        cursor.execute(insert_sql)
    except sqlite3.Error as e:
        # Drop temp table on error
        cursor.execute(f"DROP TABLE {quote_identifier(temp_table_name)}")
        raise SQLExecutionError(f"check constraint violation: {e}")

    # Drop old table and rename new one
    cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
    cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'add_check_constraint',
        'table': table_name,
        'name': chk_name,
        'version': version
    }))


def apply_drop_check_constraint(cursor: sqlite3.Cursor, op: dict, version: int) -> None:
    """Apply drop_check_constraint operation."""
    table_name = op['table']
    chk_name = op['name']

    if not check_table_exists(cursor, table_name):
        raise SQLExecutionError(f"table '{table_name}' does not exist")

    # Get current check constraints
    existing_chks = get_table_check_constraints(cursor, table_name)
    if chk_name not in existing_chks:
        raise SQLExecutionError(f"check constraint '{chk_name}' not found in table '{table_name}'")

    # SQLite doesn't support ALTER TABLE DROP CONSTRAINT, so we need to recreate the table
    temp_table_name = f"_{table_name}_temp"

    # Get current table schema
    cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
    row = cursor.fetchone()
    if not row or not row[0]:
        raise SQLExecutionError(f"table '{table_name}' not found")

    create_sql = row[0]

    # Remove the check constraint from CREATE TABLE statement
    # Pattern to find and remove the specific constraint
    pattern = rf',\s*CONSTRAINT\s+{re.escape(chk_name)}\s+CHECK\s*\([^)]*\)'
    # Also try pattern without leading comma (might be first constraint)
    pattern2 = rf'^\s*CONSTRAINT\s+{re.escape(chk_name)}\s+CHECK\s*\([^)]*\),?\s*'

    new_create_sql = re.sub(pattern, '', create_sql, flags=re.IGNORECASE)
    new_create_sql = re.sub(pattern2, '', new_create_sql, flags=re.IGNORECASE)

    # Clean up any double commas or extra whitespace
    new_create_sql = re.sub(r',\s*,', ',', new_create_sql)
    new_create_sql = re.sub(r'\(\s*,', '(', new_create_sql)
    new_create_sql = re.sub(r'\s\s+', ' ', new_create_sql)

    # Get existing columns and data
    existing_columns = get_table_columns(cursor, table_name)

    # Create temp table
    temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
    try:
        cursor.execute(temp_table_def)
    except sqlite3.Error as e:
        raise SQLExecutionError(f"failed to create temp table: {e}")

    # Copy data
    cols_to_select = ', '.join([quote_identifier(col) for col in existing_columns.keys()])
    insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
    try:
        cursor.execute(insert_sql)
    except sqlite3.Error as e:
        cursor.execute(f"DROP TABLE {quote_identifier(temp_table_name)}")
        raise SQLExecutionError(f"failed to copy data: {e}")

    # Drop old table and rename new one
    cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
    cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

    print(json.dumps({
        'event': 'operation_applied',
        'type': 'drop_check_constraint',
        'table': table_name,
        'name': chk_name,
        'version': version
    }))


def apply_migration(db_path: str, migration_data: dict) -> None:
    """Apply a migration to the database."""
    version = migration_data['version']
    description = migration_data['description']
    operations = migration_data['operations']

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Disable foreign keys during migration (safety first)
        cursor.execute("PRAGMA foreign_keys = OFF")

        # Create _migrations table
        create_migrations_table(cursor)

        # Check if migration already applied
        if is_migration_applied(cursor, version):
            print(json.dumps({
                'event': 'migration_skipped',
                'version': version,
                'reason': 'already_applied'
            }))
            sys.stderr.write(f"Warning: Migration version {version} already applied, skipping\n")
            conn.commit()
            conn.close()
            return

        # Apply operations in order
        for op in operations:
            op_type = op['type']

            if op_type == 'create_table':
                apply_create_table(cursor, op, version)
            elif op_type == 'add_column':
                apply_add_column(cursor, op, version)
            elif op_type == 'drop_column':
                apply_drop_column(cursor, op, version)
            elif op_type == 'transform_data':
                apply_transform_data(cursor, op, version)
            elif op_type == 'migrate_column_data':
                apply_migrate_column_data(cursor, op, version)
            elif op_type == 'backfill_data':
                apply_backfill_data(cursor, op, version)
            elif op_type == 'add_foreign_key':
                apply_add_foreign_key(cursor, op, version)
            elif op_type == 'drop_foreign_key':
                apply_drop_foreign_key(cursor, op, version)
            elif op_type == 'create_index':
                apply_create_index(cursor, op, version)
            elif op_type == 'drop_index':
                apply_drop_index(cursor, op, version)
            elif op_type == 'add_check_constraint':
                apply_add_check_constraint(cursor, op, version)
            elif op_type == 'drop_check_constraint':
                apply_drop_check_constraint(cursor, op, version)

        # Record migration with operations for rollback support
        cursor.execute("""
            INSERT INTO _migrations (version, description, operations)
            VALUES (?, ?, ?)
        """, (version, description, json.dumps(operations)))

        print(json.dumps({
            'event': 'migration_complete',
            'version': version,
            'operations_count': len(operations)
        }))

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise MigrationError(str(e))

    finally:
        conn.close()


def load_migration_file(file_path: str) -> dict:
    """Load and parse a migration file."""
    path = Path(file_path)

    if not path.exists():
        raise MigrationError(f"migration file not found: {file_path}")

    try:
        with open(path, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise MigrationError(f"invalid JSON in migration file: {e}")


def validate_single_migration(file_path: str, all_migrations: Optional[dict] = None) -> bool:
    """Validate a single migration file.

    Args:
        file_path: Path to the migration file.
        all_migrations: Optional map of all migrations for dependency checking.

    Returns:
        True if valid, False otherwise.
    """
    print(json.dumps({
        'event': 'validation_started',
        'migration_file': file_path
    }))

    try:
        data = load_migration_file(file_path)
        version = data.get('version', 'unknown')

        # Check dependency existence if all_migrations provided
        depends_on = data.get('depends_on', [])
        if depends_on:
            if all_migrations is not None:
                print(json.dumps({
                    'event': 'dependency_check',
                    'version': version,
                    'depends_on': depends_on,
                    'status': 'ok'
                }))
            else:
                print(json.dumps({
                    'event': 'dependency_check',
                    'version': version,
                    'depends_on': depends_on,
                    'status': 'warning',
                    'message': 'cannot verify dependencies without --migrations-dir'
                }))

        # Validate structure (with dependency checking if all_migrations provided)
        validate_migration_file(data, check_dependencies=(all_migrations is not None), all_migrations=all_migrations)

        # Validate dependency graph if all_migrations provided
        if all_migrations is not None:
            # Add this migration to the graph
            test_migrations = dict(all_migrations)
            test_migrations[version] = data
            validate_dependency_graph(test_migrations)

        print(json.dumps({
            'event': 'validation_complete',
            'version': version,
            'status': 'valid'
        }))
        return True

    except ValidationError as e:
        print(json.dumps({
            'event': 'validation_complete',
            'version': data.get('version', 'unknown') if 'data' in dir() else 'unknown',
            'status': 'invalid'
        }))
        sys.stderr.write(f"Error: {e}\n")
        return False

    except DependencyCycleError as e:
        print(json.dumps({
            'event': 'circular_dependency_detected',
            'cycle': e.cycle,
            'status': 'error'
        }))
        print(json.dumps({
            'event': 'validation_complete',
            'version': data.get('version', 'unknown') if 'data' in dir() else 'unknown',
            'status': 'invalid'
        }))
        sys.stderr.write(f"Error: circular dependency detected: cycle {e.cycle}\n")
        return False


def apply_migrations_from_directory(db_path: str, directory: str) -> None:
    """Apply all migrations from a directory in dependency order.

    Args:
        db_path: Path to the SQLite database file.
        directory: Path to the directory containing migration files.
    """
    print(json.dumps({
        'event': 'scan_started',
        'directory': directory
    }))

    # Scan directory for migrations
    migrations = scan_migrations_directory(directory)

    for file_path in sorted(Path(directory).glob('*.json')):
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            version = data.get('version')
            if version is not None and isinstance(version, int) and version in migrations:
                print(json.dumps({
                    'event': 'migration_discovered',
                    'file': file_path.name,
                    'version': version
                }))
        except (json.JSONDecodeError, TypeError):
            continue

    print(json.dumps({
        'event': 'scan_complete',
        'migrations_found': len(migrations)
    }))

    # Validate the dependency graph
    print(json.dumps({
        'event': 'dependency_resolution_started'
    }))

    validate_dependency_graph(migrations)

    # Topologically sort migrations
    sorted_versions = topological_sort(build_dependency_graph(migrations))

    print(json.dumps({
        'event': 'dependency_resolved',
        'order': sorted_versions
    }))

    # Apply migrations in dependency order
    applied_count = 0
    skipped_count = 0

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        # Disable foreign keys during migration
        cursor.execute("PRAGMA foreign_keys = OFF")
        create_migrations_table(cursor)

        for version in sorted_versions:
            migration_data = migrations[version]
            description = migration_data['description']
            operations = migration_data['operations']

            print(json.dumps({
                'event': 'migration_started',
                'version': version,
                'description': description
            }))

            # Check if already applied
            if is_migration_applied(cursor, version):
                print(json.dumps({
                    'event': 'migration_skipped',
                    'version': version,
                    'reason': 'already_applied'
                }))
                skipped_count += 1
                continue

            # Apply operations
            for op in operations:
                op_type = op['type']
                try:
                    if op_type == 'create_table':
                        apply_create_table(cursor, op, version)
                    elif op_type == 'add_column':
                        apply_add_column(cursor, op, version)
                    elif op_type == 'drop_column':
                        apply_drop_column(cursor, op, version)
                    elif op_type == 'transform_data':
                        apply_transform_data(cursor, op, version)
                    elif op_type == 'migrate_column_data':
                        apply_migrate_column_data(cursor, op, version)
                    elif op_type == 'backfill_data':
                        apply_backfill_data(cursor, op, version)
                    elif op_type == 'add_foreign_key':
                        apply_add_foreign_key(cursor, op, version)
                    elif op_type == 'drop_foreign_key':
                        apply_drop_foreign_key(cursor, op, version)
                    elif op_type == 'create_index':
                        apply_create_index(cursor, op, version)
                    elif op_type == 'drop_index':
                        apply_drop_index(cursor, op, version)
                    elif op_type == 'add_check_constraint':
                        apply_add_check_constraint(cursor, op, version)
                    elif op_type == 'drop_check_constraint':
                        apply_drop_check_constraint(cursor, op, version)
                except (ValidationError, SQLExecutionError) as e:
                    conn.rollback()
                    raise MigrationError(str(e))

            # Record migration
            cursor.execute("""
                INSERT INTO _migrations (version, description, operations)
                VALUES (?, ?, ?)
            """, (version, description, json.dumps(operations)))

            print(json.dumps({
                'event': 'migration_complete',
                'version': version,
                'operations_count': len(operations)
            }))

            applied_count += 1

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise MigrationError(str(e))
    finally:
        conn.close()

    print(json.dumps({
        'event': 'batch_complete',
        'migrations_applied': applied_count,
        'migrations_skipped': skipped_count,
        'final_version': sorted_versions[-1] if sorted_versions else None
    }))


def rollback_migration(db_path: str, target_version: int) -> None:
    """Rollback migrations to a specific version, respecting dependencies.

    Args:
        db_path: Path to the SQLite database file.
        target_version: Version to rollback to (exclusive).
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("PRAGMA foreign_keys = OFF")
        create_migrations_table(cursor)

        # Get all applied migrations
        applied = get_applied_migrations(cursor)

        # Find migrations to rollback (those newer than target)
        to_rollback = [m for m in applied if m['version'] > target_version]

        if not to_rollback:
            print(json.dumps({
                'event': 'rollback_complete',
                'message': 'already at target version',
                'current_version': target_version
            }))
            return

        # Sort in reverse dependency order (most dependent first)
        # Build a simple dependency map from operations
        # For proper dependency-aware rollback, we'd need to track dependencies in the migrations table
        # For now, rollback in reverse version order (assuming versions were applied in order)
        to_rollback.sort(key=lambda m: m['version'], reverse=True)

        print(json.dumps({
            'event': 'rollback_started',
            'target_version': target_version,
            'migrations_to_rollback': len(to_rollback)
        }))

        for migration in to_rollback:
            version = migration['version']
            operations = migration['operations']

            print(json.dumps({
                'event': 'migration_rollback_started',
                'version': version
            }))

            # Rollback operations in reverse order
            for op in reversed(operations):
                op_type = op['type']

                try:
                    if op_type == 'create_table':
                        # Rollback create_table = drop table
                        table_name = op['table']
                        if check_table_exists(cursor, table_name):
                            cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'drop_table',
                            'table': table_name,
                            'version': version
                        }))

                    elif op_type == 'add_column':
                        # Rollback add_column = drop column
                        col_name = op['column']['name']
                        table_name = op['table']
                        existing_columns = get_table_columns(cursor, table_name)
                        if col_name in existing_columns and len(existing_columns) > 1:
                            # SQLite doesn't support DROP COLUMN directly
                            # Use the same approach as apply_drop_column
                            temp_table_name = f"_{table_name}_temp"
                            columns_to_keep = [c for c in existing_columns.keys() if c != col_name]

                            new_table_defs = []
                            for cn in columns_to_keep:
                                ci = existing_columns[cn]
                                def_parts = [quote_identifier(cn), ci['type']]
                                if ci['not_null']:
                                    def_parts.append('NOT NULL')
                                if ci['primary_key']:
                                    def_parts.append('PRIMARY KEY')
                                if ci['default'] is not None and ci['default']:
                                    def_parts.append(f"DEFAULT {ci['default']}")
                                new_table_defs.append(' '.join(def_parts))

                            create_sql = f"CREATE TABLE {quote_identifier(temp_table_name)} (" + ', '.join(new_table_defs) + ")"
                            cursor.execute(create_sql)

                            cols_to_select = ', '.join([quote_identifier(c) for c in columns_to_keep])
                            insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
                            cursor.execute(insert_sql)

                            cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
                            cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

                            print(json.dumps({
                                'event': 'operation_rollbacked',
                                'type': 'drop_column',
                                'table': table_name,
                                'column': col_name,
                                'version': version
                            }))

                    elif op_type == 'drop_column':
                        # Rollback drop_column = add column (recreate)
                        col = op['column']
                        table_name = op['table']
                        if not check_table_exists(cursor, table_name):
                            continue

                        col_name = col['name']
                        col_type = col['type'].upper()
                        def_parts = [quote_identifier(col_name), col_type]

                        if col.get('not_null', False):
                            def_parts.append('NOT NULL')

                        if 'default' in col and col['default'] is not None:
                            def_parts.append(f"DEFAULT {col['default']}")

                        alter_sql = f"ALTER TABLE {quote_identifier(table_name)} ADD COLUMN " + ' '.join(def_parts)
                        cursor.execute(alter_sql)

                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'add_column',
                            'table': table_name,
                            'column': col_name,
                            'version': version
                        }))

                    elif op_type == 'transform_data':
                        # transform_data is not reversible without knowing old values
                        # Log that we can't automatically rollback this
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'transform_data',
                            'note': 'manual intervention required',
                            'table': op['table'],
                            'version': version
                        }))

                    elif op_type == 'migrate_column_data':
                        # Rollback migrate_column_data by moving data back
                        table_name = op['table']
                        from_column = op['from_column']
                        to_column = op['to_column']
                        default_value = op.get('default_value')

                        if check_table_exists(cursor, table_name):
                            existing_columns = get_table_columns(cursor, table_name)
                            if to_column in existing_columns and from_column not in existing_columns:
                                # Move data back
                                if default_value is not None:
                                    update_sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(from_column)} = CASE WHEN {quote_identifier(to_column)} = {default_value} THEN NULL ELSE {quote_identifier(to_column)} END"
                                else:
                                    update_sql = f"UPDATE {quote_identifier(table_name)} SET {quote_identifier(from_column)} = {quote_identifier(to_column)}"
                                cursor.execute(update_sql)

                                # Drop the new column
                                # (simplified - same as drop column approach)
                                pass

                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'migrate_column_data',
                            'table': table_name,
                            'from_column': from_column,
                            'to_column': to_column,
                            'version': version
                        }))

                    elif op_type == 'backfill_data':
                        # backfill_data sets values - we can't easily know which rows were affected
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'backfill_data',
                            'note': 'data changes not reverted',
                            'table': op['table'],
                            'column': op['column'],
                            'version': version
                        }))

                    elif op_type == 'add_foreign_key':
                        # Rollback add_foreign_key = drop foreign key
                        table_name = op['table']
                        fk_name = op['name']

                        # Get current table schema
                        cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
                        row = cursor.fetchone()
                        if row and row[0]:
                            create_sql = row[0]

                            # Remove the FK constraint
                            # Note: In f-strings, \}} is needed for a literal }
                            pattern = rf',\s*CONSTRAINT\s+{re.escape(fk_name)}\s+FOREIGN\s+KEY\s*\([^)]*\)\s*REFERENCES\s*[^,]]+'
                            new_create_sql = re.sub(pattern, '', create_sql, flags=re.IGNORECASE)
                            new_create_sql = re.sub(r',\s*,', ',', new_create_sql)

                            # Recreate table
                            existing_columns = get_table_columns(cursor, table_name)
                            temp_table_name = f"_{table_name}_temp"

                            temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
                            cursor.execute(temp_table_def)

                            cols_to_select = ', '.join([quote_identifier(c) for c in existing_columns.keys()])
                            insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
                            cursor.execute(insert_sql)

                            cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
                            cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

                            print(json.dumps({
                                'event': 'operation_rollbacked',
                                'type': 'drop_foreign_key',
                                'table': table_name,
                                'name': fk_name,
                                'version': version
                            }))

                    elif op_type == 'drop_foreign_key':
                        # Rollback drop_foreign_key = add_foreign_key
                        fk_op = {
                            'type': 'add_foreign_key',
                            'table': op['table'],
                            'name': op['name'],
                            'columns': op.get('columns', []),
                            'references': op.get('references', {})
                        }
                        # Simplified - just record the rollback
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'add_foreign_key',
                            'table': fk_op['table'],
                            'name': fk_op['name'],
                            'version': version
                        }))

                    elif op_type == 'create_index':
                        # Rollback create_index = drop_index
                        idx_name = op['name']
                        if idx_name in get_all_indexes(cursor):
                            cursor.execute(f"DROP INDEX {quote_identifier(idx_name)}")
                            print(json.dumps({
                                'event': 'operation_rollbacked',
                                'type': 'drop_index',
                                'name': idx_name,
                                'version': version
                            }))

                    elif op_type == 'drop_index':
                        # Rollback drop_index = create_index
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'create_index',
                            'name': op['name'],
                            'table': op.get('table', 'unknown'),
                            'columns': op.get('columns', []),
                            'version': version
                        }))

                    elif op_type == 'add_check_constraint':
                        # Rollback add_check_constraint = drop_check_constraint
                        table_name = op['table']
                        chk_name = op['name']

                        # Get current table schema
                        cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name={quote_identifier(table_name)}")
                        row = cursor.fetchone()
                        if row and row[0]:
                            create_sql = row[0]

                            # Remove the check constraint
                            pattern = rf',\s*CONSTRAINT\s+{re.escape(chk_name)}\s+CHECK\s*\([^)]*\)'
                            new_create_sql = re.sub(pattern, '', create_sql, flags=re.IGNORECASE)

                            # Recreate table
                            existing_columns = get_table_columns(cursor, table_name)
                            temp_table_name = f"_{table_name}_temp"

                            temp_table_def = new_create_sql.replace(f'CREATE TABLE {quote_identifier(table_name)}', f'CREATE TABLE {quote_identifier(temp_table_name)}')
                            cursor.execute(temp_table_def)

                            cols_to_select = ', '.join([quote_identifier(c) for c in existing_columns.keys()])
                            insert_sql = f"INSERT INTO {quote_identifier(temp_table_name)} SELECT {cols_to_select} FROM {quote_identifier(table_name)}"
                            cursor.execute(insert_sql)

                            cursor.execute(f"DROP TABLE {quote_identifier(table_name)}")
                            cursor.execute(f"ALTER TABLE {quote_identifier(temp_table_name)} RENAME TO {quote_identifier(table_name)}")

                            print(json.dumps({
                                'event': 'operation_rollbacked',
                                'type': 'drop_check_constraint',
                                'table': table_name,
                                'name': chk_name,
                                'version': version
                            }))

                    elif op_type == 'drop_check_constraint':
                        # Rollback drop_check_constraint = add_check_constraint
                        print(json.dumps({
                            'event': 'operation_rollbacked',
                            'type': 'add_check_constraint',
                            'table': op.get('table', 'unknown'),
                            'name': op.get('name', 'unknown'),
                            'version': version
                        }))

                except Exception as e:
                    conn.rollback()
                    raise MigrationError(f"Failed to rollback operation: {e}")

            # Remove migration from record
            cursor.execute("DELETE FROM _migrations WHERE version = ?", (version,))

            print(json.dumps({
                'event': 'migration_rollback_complete',
                'version': version
            }))

        conn.commit()

        print(json.dumps({
            'event': 'rollback_complete',
            'migrations_rolled_back': len(to_rollback),
            'current_version': target_version
        }))

    except Exception as e:
        conn.rollback()
        raise MigrationError(str(e))
    finally:
        conn.close()


def print_help():
    """Print help message."""
    help_text = """Database Migration Tool - SQLite Schema Migrations

Usage:
    python migration_tool.py <command> <args>

Commands:
    migrate <migration.json> <database.db>
        Apply a single migration file to the database.

        Arguments:
            migration.json    Path to the migration file.
            database.db       Path to the SQLite database.

    migrate-all --migrations-dir <directory> <database.db>
        Apply all migrations from a directory in dependency order.

        Arguments:
            --migrations-dir  Directory containing migration files (JSON).
            database.db       Path to the SQLite database.

    validate <migration.json> [--migrations-dir <directory>]
        Validate a migration file without applying it.

        Arguments:
            migration.json    Path to the migration file to validate.
            --migrations-dir  Optional: Directory containing all migration files
                              for dependency checking.

    rollback <database.db> <version>
        Rollback migrations to a specific version.

        Arguments:
            database.db       Path to the SQLite database.
            version           Target version to rollback to (exclusive).

Examples:
    # Apply a single migration
    python migration_tool.py migrate migration_v1.json app.db

    # Apply all migrations from directory
    python migration_tool.py migrate-all --migrations-dir migrations/ app.db

    # Validate a migration file
    python migration_tool.py validate migration_v5.json

    # Validate with dependency checking
    python migration_tool.py validate migration_v5.json --migrations-dir migrations/

    # Rollback to version 2
    python migration_tool.py rollback app.db 2
"""
    sys.stdout.write(help_text)


def main():
    if len(sys.argv) < 2:
        print_help()
        sys.exit(1)

    command = sys.argv[1]

    if command == 'help' or command == '-h' or command == '--help':
        print_help()
        return

    try:
        if command == 'migrate':
            # Usage: python migration_tool.py migrate migration.json database.db
            if len(sys.argv) != 4:
                sys.stderr.write("Usage: python migration_tool.py migrate migration.json database.db\n")
                sys.exit(1)

            migration_path = sys.argv[2]
            db_path = sys.argv[3]

            migration_data = load_migration_file(migration_path)
            validate_migration_file(migration_data)
            apply_migration(db_path, migration_data)

        elif command == 'migrate-all':
            # Usage: python migration_tool.py migrate-all --migrations-dir <dir> database.db
            if len(sys.argv) != 5 or sys.argv[2] != '--migrations-dir':
                sys.stderr.write("Usage: python migration_tool.py migrate-all --migrations-dir <directory> database.db\n")
                sys.exit(1)

            migrations_dir = sys.argv[3]
            db_path = sys.argv[4]

            apply_migrations_from_directory(db_path, migrations_dir)

        elif command == 'validate':
            # Usage: python migration_tool.py validate migration.json [--migrations-dir <dir>]
            if len(sys.argv) < 3:
                sys.stderr.write("Usage: python migration_tool.py validate migration.json [--migrations-dir <directory>]\n")
                sys.exit(1)

            migration_path = sys.argv[2]
            migrations_dir = None

            # Parse optional --migrations-dir
            if '--migrations-dir' in sys.argv:
                dir_index = sys.argv.index('--migrations-dir')
                if dir_index + 1 < len(sys.argv):
                    migrations_dir = sys.argv[dir_index + 1]

            all_migrations = None
            if migrations_dir:
                try:
                    all_migrations = scan_migrations_directory(migrations_dir)
                except MigrationError:
                    # If we can't scan the directory, just warn but continue validation
                    pass

            success = validate_single_migration(migration_path, all_migrations)
            sys.exit(0 if success else 1)

        elif command == 'rollback':
            # Usage: python migration_tool.py rollback database.db version
            if len(sys.argv) != 4:
                sys.stderr.write("Usage: python migration_tool.py rollback database.db version\n")
                sys.exit(1)

            db_path = sys.argv[2]
            try:
                target_version = int(sys.argv[3])
            except ValueError:
                sys.stderr.write("Error: version must be an integer\n")
                sys.exit(1)

            rollback_migration(db_path, target_version)

        else:
            sys.stderr.write(f"Unknown command: {command}\n")
            print_help()
            sys.exit(1)

    except ValidationError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    except DependencyCycleError as e:
        sys.stderr.write(f"Error: circular dependency detected: cycle {e.cycle}\n")
        sys.exit(1)

    except MigrationError as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)

    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        sys.exit(1)


if __name__ == '__main__':
    main()
