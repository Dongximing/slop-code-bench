"""CLI commands for the migration tool."""

import argparse
import json
import os
import sqlite3
import sys
from typing import Optional

from .validators import error_exit, warning, output_json, validate_migration, NON_REVERSIBLE_OPS
from .operations import (
    EXECUTORS,
    ROLLBACK_EXECUTORS,
    init_migrations_table,
    is_migration_applied,
    record_migration,
    get_applied_migrations,
    get_current_version,
)


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
    if not os.path.exists(db_file):
        error_exit(f"database file not found: {db_file}")

    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
    except sqlite3.Error as e:
        error_exit(f"error connecting to database: {e}")

    try:
        init_migrations_table(cursor)

        applied = get_applied_migrations(cursor)

        if not applied:
            error_exit("no migrations to rollback")

        current_version = get_current_version(cursor)

        if to_version is not None:
            versions_to_rollback = [v for v, _, _ in applied if v > to_version]
            if not versions_to_rollback:
                error_exit(f"version {to_version} not found")
        elif count is not None:
            if count <= 0:
                error_exit("count must be positive")
            versions_to_rollback = sorted((v for v, _, _ in applied), reverse=True)[:count]
        else:
            versions_to_rollback = [current_version]

        versions_to_rollback.sort(reverse=True)

        all_rolled_back = []

        for version in versions_to_rollback:
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

            rollback_ops = None
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

            try:
                stored_operations = json.loads(operations_json) if operations_json else []
            except json.JSONDecodeError:
                stored_operations = []

            if rollback_ops is not None:
                for op in reversed(rollback_ops):
                    op_type = op["type"]
                    executor = EXECUTORS.get(op_type)
                    if executor:
                        executor(cursor, op, version)
                    else:
                        error_exit(f"cannot rollback: unknown operation type '{op_type}'")
            else:
                for op in stored_operations:
                    if op["type"] in NON_REVERSIBLE_OPS:
                        error_exit(f"cannot rollback version {version}: missing rollback_operations for {op['type']}")

                for op in reversed(stored_operations):
                    op_type = op["type"]
                    rollback_fn = ROLLBACK_EXECUTORS.get(op_type)
                    if rollback_fn:
                        rollback_fn(cursor, op, version)
                    else:
                        error_exit(f"cannot rollback: unknown operation type '{op_type}'")

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
        error_exit(f"rollback failed: {str(e)}")
    finally:
        conn.close()


def run_status(db_file: str) -> None:
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
