#!/usr/bin/env python3
"""Forge - CLI tool for managing blueprints in a compute cluster."""

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ForgeError(Exception):
    """Base exception for Forge errors."""

    def __init__(self, error_type: str, detail: Any = None):
        self.error_type = error_type
        self.detail = detail
        super().__init__(json.dumps({"error": error_type, "detail": detail}))


class JSONStore:
    """In-memory cache with JSON persistence."""

    def __init__(self, data_dir: str, filename: str):
        self.file_path = Path(data_dir) / filename
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: list[dict] | None = None

    def _load(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        if not self.file_path.exists():
            self._cache = []
            return []
        try:
            with open(self.file_path) as f:
                self._cache = json.load(f)
        except (json.JSONDecodeError, IOError):
            self._cache = []
        return self._cache

    def _save(self, items: list[dict]) -> None:
        self._cache = items
        with open(self.file_path, 'w') as f:
            json.dump(items, f, indent=2)

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda a: (a.get('created_at', ''), a.get('uuid', '')))

    def get(self, key: str, value: str) -> dict | None:
        for item in self._load():
            if item.get(key) == value:
                return item
        return None



class BlueprintStore(JSONStore):
    """In-memory cache with JSON persistence for blueprints."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "blueprints.json")

    def create(self, item: dict) -> dict:
        existing = self._load()
        existing.append(item)
        self._save(existing)
        return item

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda b: (b.get('created_at', ''), b.get('name', ''), b.get('uuid', '')))

    def delete(self, key: str, value: str) -> bool:
        items = self._load()
        after = [b for b in items if b.get(key) != value]
        if len(after) < len(items):
            self._save(after)
            return True
        return False

    def delete_by_names(self, names: list[str]) -> tuple[int, list[str]]:
        items = self._load()
        name_set = set(names)
        remaining = [b for b in items if b['name'] not in name_set]
        deleted = len(items) - len(remaining)
        if deleted:
            self._save(remaining)
        unresolved = [n for n in names if n not in name_set]
        return deleted, unresolved


class AllocationStore(JSONStore):
    """In-memory cache with JSON persistence for allocations."""

    def __init__(self, data_dir: str):
        super().__init__(data_dir, "allocations.json")

    def list_by_filters(self, assignment_id: str | None = None, binding_status: str | None = None) -> list[dict]:
        items = self._load()
        filtered = items
        if assignment_id is not None:
            filtered = [a for a in filtered if a.get('assignment_id') == assignment_id]
        if binding_status is not None:
            filtered = [a for a in filtered if a.get('binding_status') == binding_status]
        return sorted(filtered, key=lambda a: (a.get('created_at', ''), a.get('uuid', '')))

    def create(self, items: list[dict]) -> list[dict]:
        existing = self._load()
        existing.extend(items)
        self._save(existing)
        return items

    def get(self, key: str, value: str) -> dict | None:
        for item in self._load():
            if item.get(key) == value:
                return item
        return None

    def bind(self, uuid: str, assignment_id: str) -> bool:
        """Set allocation's binding_status to 'bound' and record assignment_id. Returns True if found and updated."""
        items = self._load()
        for item in items:
            if item.get('uuid') == uuid:
                item['binding_status'] = 'bound'
                item['assignment_id'] = assignment_id
                self._save(items)
                return True
        return False

    def unbind(self, uuid: str) -> bool:
        """Set allocation's binding_status to 'unbound' and clear assignment_id. Returns True if found and updated."""
        items = self._load()
        for item in items:
            if item.get('uuid') == uuid:
                item['binding_status'] = 'unbound'
                item['assignment_id'] = None
                self._save(items)
                return True
        return False

    def delete_by_assignment_id(self, assignment_id: str) -> int:
        """Delete all allocations with matching assignment_id. Returns count deleted."""
        items = self._load()
        before = len(items)
        after = [a for a in items if a.get('assignment_id') != assignment_id]
        if len(after) < before:
            self._save(after)
        return before - len(after)

    def delete_by_ids(self, uuids: list[str]) -> tuple[bool, int]:
        """
        Delete allocations by UUID list.
        Returns (success, count) where success is False if any UUID not found.
        If any UUID not found, no deletions occur.
        """
        items = self._load()
        existing_uuids = {item.get('uuid') for item in items}
        requested_uuids = set(uuids)

        # Check if all UUIDs exist
        if not requested_uuids.issubset(existing_uuids):
            return False, 0

        new_items = [a for a in items if a.get('uuid') not in requested_uuids]
        count = len(items) - len(new_items)
        if count:
            self._save(new_items)
        return True, count


def validate_requirement_set(rs: dict) -> list[str]:
    errors = []
    if not isinstance(rs, dict):
        return ["requirement set must be an object"]
    if 'resource_type' not in rs:
        errors.append("missing 'resource_type'")
    elif not isinstance(rs['resource_type'], str):
        errors.append("'resource_type' must be a string")
    if 'resource_count' not in rs:
        errors.append("missing 'resource_count'")
    elif not isinstance(rs['resource_count'], int) or rs['resource_count'] < 1:
        errors.append("'resource_count' must be a positive integer")
    if 'capabilities' in rs and not isinstance(rs['capabilities'], list):
        errors.append("'capabilities' must be an array")
    elif isinstance(rs.get('capabilities'), list):
        for i, cap in enumerate(rs['capabilities']):
            if not isinstance(cap, str):
                errors.append(f"capabilities[{i}] must be a string")
    return errors


def validate_blueprint(data: dict) -> list[str]:
    errors = []
    if 'name' not in data or not isinstance(data['name'], str) or not data['name'].strip():
        errors.append("'name' must be a non-empty string")
    if 'requirement_sets' not in data:
        errors.append("missing 'requirement_sets'")
    elif not isinstance(data['requirement_sets'], list) or not data['requirement_sets']:
        errors.append("'requirement_sets' must be a non-empty array")
    else:
        for i, rs in enumerate(data['requirement_sets']):
            for e in validate_requirement_set(rs):
                errors.append(f"requirement_sets[{i}]: {e}")
    return errors


def normalize_requirement_set(rs: dict) -> dict:
    return {key: rs[key] for key in ('resource_type', 'resource_count', 'capabilities') if key in rs}


def generate_timestamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def generate_uuid() -> str:
    return str(uuid.uuid4()).lower()


def output_success(data: Any) -> None:
    print(json.dumps(data))
    sys.exit(0)


def output_error(error_type: str, detail: Any = None) -> None:
    print(json.dumps({"error": error_type, "detail": detail}))
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Forge - CLI tool for managing blueprints")
    parser.add_argument('--data-dir', required=True, help="Directory for persistent state")
    subparsers = parser.add_subparsers(dest='command', required=True)

    blueprint_parser = subparsers.add_parser('blueprint', help='Blueprint management commands')
    bp_subparsers = blueprint_parser.add_subparsers(dest='subcommand', required=True)

    create_parser = bp_subparsers.add_parser('create', help='Create a new blueprint')
    create_parser.add_argument('input_file', nargs='?', type=argparse.FileType('r'), default=sys.stdin)

    bp_subparsers.add_parser('list', help='List all blueprints')

    get_parser = bp_subparsers.add_parser('get', help='Get a blueprint by UUID')
    get_parser.add_argument('uuid', help='Blueprint UUID')

    delete_parser = bp_subparsers.add_parser('delete', help='Delete a blueprint')
    delete_parser.add_argument('uuid', nargs='?', help='Blueprint UUID (for single delete)')
    delete_parser.add_argument('--names', help='Comma-separated list of blueprint names for batch delete')

    allocation_parser = subparsers.add_parser('allocation', help='Allocation management commands')
    alloc_subparsers = allocation_parser.add_subparsers(dest='subcommand', required=True)

    create_alloc_parser = alloc_subparsers.add_parser('create', help='Create allocations from a blueprint')
    create_alloc_parser.add_argument('input_file', nargs='?', type=argparse.FileType('r'), default=sys.stdin)

    list_parser = alloc_subparsers.add_parser('list', help='List all allocations')
    list_parser.add_argument('--assignment', help='Filter by assignment ID')
    list_parser.add_argument('--status', help='Filter by binding status')

    get_alloc_parser = alloc_subparsers.add_parser('get', help='Get an allocation by UUID')
    get_alloc_parser.add_argument('uuid', help='Allocation UUID')

    bind_parser = alloc_subparsers.add_parser('bind', help='Bind or unbind allocations from an assignment')

    delete_parser = alloc_subparsers.add_parser('delete', help='Delete allocations')
    delete_parser.add_argument('--assignment', help='Delete all allocations with this assignment ID')
    delete_parser.add_argument('--ids', help='Comma-separated list of allocation UUIDs to delete')

    args = parser.parse_args()
    blueprint_store = BlueprintStore(args.data_dir)
    allocation_store = AllocationStore(args.data_dir)

    if args.command == 'blueprint' and args.subcommand == 'create':
        try:
            data = json.load(args.input_file)
        except json.JSONDecodeError:
            output_error("validation_error", "input is not valid JSON")

        errors = validate_blueprint(data)
        if errors:
            output_error("validation_error", {"field": "blueprint", "errors": errors})

        name = data['name']
        if blueprint_store.get('name', name):
            output_error("conflict", f"blueprint with name '{name}' already exists")

        blueprint = {
            "uuid": generate_uuid(),
            "name": name,
            "requirement_sets": [normalize_requirement_set(rs) for rs in data['requirement_sets']],
            "created_at": generate_timestamp(),
        }
        output_success(blueprint_store.create(blueprint))

    elif args.command == 'blueprint' and args.subcommand == 'list':
        output_success(blueprint_store.all())

    elif args.command == 'blueprint' and args.subcommand == 'get':
        blueprint = blueprint_store.get('uuid', args.uuid)
        if not blueprint:
            output_error("not_found", f"blueprint with uuid '{args.uuid}' not found")
        output_success(blueprint)

    elif args.command == 'blueprint' and args.subcommand == 'delete':
        if args.names:
            deleted, unresolved = blueprint_store.delete_by_names(args.names.split(','))
            if unresolved:
                output_error("not_found", {"unresolved": unresolved})
            output_success({"deleted": deleted})
        elif args.uuid:
            if not blueprint_store.delete('uuid', args.uuid):
                output_error("not_found", f"blueprint with uuid '{args.uuid}' not found")
            output_success({"deleted": 1})
        else:
            output_error("validation_error", "either UUID or --names must be provided")

    elif args.command == 'allocation' and args.subcommand == 'create':
        try:
            payload = json.load(args.input_file)
        except json.JSONDecodeError:
            output_error("validation_error", "input is not valid JSON")

        if not isinstance(payload, dict):
            output_error("validation_error", "payload must be a JSON object")

        if 'blueprint_name' not in payload:
            output_error("validation_error", "missing 'blueprint_name'")

        blueprint_name = payload['blueprint_name']
        if not isinstance(blueprint_name, str) or not blueprint_name.strip():
            output_error("validation_error", "'blueprint_name' must be a non-empty string")

        blueprint = blueprint_store.get('name', blueprint_name)
        if not blueprint:
            output_error("not_found", f"blueprint with name '{blueprint_name}' not found")

        timestamp = generate_timestamp()
        allocations = []

        for idx, rs in enumerate(blueprint['requirement_sets']):
            # Determine if we need to create 2 allocations (duplicate)
            capabilities = rs.get('capabilities', [])
            duplicate = 'category=TYPE_A' in capabilities
            count = 2 if duplicate else 1

            for _ in range(count):
                allocations.append({
                    "uuid": generate_uuid(),
                    "blueprint_name": blueprint_name,
                    "requirement_set_index": idx,
                    "binding_status": "unbound",
                    "assignment_id": None,
                    "created_at": timestamp,
                })

        output_success(allocation_store.create(allocations))

    elif args.command == 'allocation' and args.subcommand == 'list':
        output_success(allocation_store.list_by_filters(args.assignment, args.status))

    elif args.command == 'allocation' and args.subcommand == 'get':
        allocation = allocation_store.get('uuid', args.uuid)
        if not allocation:
            output_error("not_found", f"allocation with uuid '{args.uuid}' not found")
        output_success(allocation)

    elif args.command == 'allocation' and args.subcommand == 'bind':
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError:
            output_error("validation_error", "input is not valid JSON")

        if not isinstance(payload, dict):
            output_error("validation_error", "payload must be a JSON object")

        if 'assignment_id' not in payload:
            output_error("validation_error", "missing 'assignment_id'")
        assignment_id = payload['assignment_id']

        if 'allocations' not in payload:
            output_error("validation_error", "missing 'allocations'")

        allocations = payload['allocations']
        if not isinstance(allocations, dict) or not allocations:
            output_error("validation_error", "allocations must be a non-empty object")

        # Validate all operations are valid
        for uuid, op in allocations.items():
            if op not in ('add', 'remove'):
                output_error("invalid_operation", f"operation '{op}' is not 'add' or 'remove'")

        # Check all allocation UUIDs exist
        for uuid in allocations:
            if not allocation_store.get('uuid', uuid):
                output_error("not_found", f"allocation with uuid '{uuid}' not found")

        # Duplicate assignment guard: check if any allocation being added is already bound
        # A duplicate assignment occurs when trying to add an already-bound allocation to a new assignment
        has_add = any(op == 'add' for op in allocations.values())
        if has_add:
            for uuid in allocations:
                if allocations[uuid] == 'add':
                    alloc = allocation_store.get('uuid', uuid)
                    if alloc and alloc.get('assignment_id') is not None:
                        output_error("duplicate_assignment", f"allocation '{uuid}' is already bound")

        # Apply all operations atomically
        items = allocation_store._load()
        item_map = {item['uuid']: item for item in items}

        for uuid, op in allocations.items():
            if op == 'add':
                if uuid in item_map:
                    item_map[uuid]['binding_status'] = 'bound'
                    item_map[uuid]['assignment_id'] = assignment_id
            else:  # remove
                if uuid in item_map:
                    item_map[uuid]['binding_status'] = 'unbound'
                    item_map[uuid]['assignment_id'] = None

        allocation_store._save(list(item_map.values()))
        output_success({"status": "accepted"})

    elif args.command == 'allocation' and args.subcommand == 'delete':
        if args.assignment and args.ids:
            output_error("validation_error", "cannot specify both --assignment and --ids")
        if not args.assignment and not args.ids:
            output_error("validation_error", "must specify either --assignment or --ids")

        if args.assignment:
            count = allocation_store.delete_by_assignment_id(args.assignment)
            output_success({"deleted": count})
        else:  # args.ids
            uuids = args.ids.split(',')
            # Empty CSV members are treated as unresolved UUIDs
            if any(u == '' for u in uuids):
                output_error("not_found", "one or more UUIDs could not be resolved")
            success, count = allocation_store.delete_by_ids(uuids)
            if not success:
                output_error("not_found", "one or more UUIDs could not be resolved")
            output_success({"deleted": count})


if __name__ == '__main__':
    main()
