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


class BlueprintStore:
    """In-memory cache with JSON persistence for blueprints."""

    def __init__(self, data_dir: str):
        self.file_path = Path(data_dir) / "blueprints.json"
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

    def _save(self, blueprints: list[dict]) -> None:
        self._cache = blueprints
        with open(self.file_path, 'w') as f:
            json.dump(blueprints, f, indent=2)

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda b: (b.get('created_at', ''), b.get('name', ''), b.get('uuid', '')))

    def get(self, key: str, value: str) -> dict | None:
        for bp in self._load():
            if bp.get(key) == value:
                return bp
        return None

    def create(self, blueprint: dict) -> dict:
        blueprints = self._load()
        blueprints.append(blueprint)
        self._save(blueprints)
        return blueprint

    def delete(self, key: str, value: str) -> bool:
        blueprints = self._load()
        before = len(blueprints)
        self._save([b for b in blueprints if b.get(key) != value])
        return len(self._load()) < before

    def delete_by_names(self, names: list[str]) -> tuple[int, list[str]]:
        blueprints = self._load()
        name_set = set(names)
        name_to_uuid = {b['name']: b['uuid'] for b in blueprints}
        unresolved = [n for n in names if n not in name_to_uuid]
        remaining = [b for b in blueprints if b['name'] not in name_set]
        deleted = len(blueprints) - len(remaining)
        if deleted:
            self._save(remaining)
        return deleted, unresolved


def validate_requirement_set(rs: dict) -> list[str]:
    """Validate a single requirement set, return list of errors."""
    errors = []
    if not isinstance(rs, dict):
        return ["requirement set must be an object"]
    if 'resource_type' not in rs:
        errors.append("missing 'resource_type'")
    elif not isinstance(rs['resource_type'], str):
        errors.append("'resource_type' must be a string")
    if 'resource_count' not in rs:
        errors.append("missing 'resource_count'")
    elif not isinstance(rs['resource_count'], int) or rs['resource_count'] < 1 or isinstance(rs['resource_count'], bool):
        errors.append("'resource_count' must be a positive integer")
    if 'capabilities' in rs:
        if not isinstance(rs['capabilities'], list):
            errors.append("'capabilities' must be an array")
        else:
            for i, cap in enumerate(rs['capabilities']):
                if not isinstance(cap, str):
                    errors.append(f"capabilities[{i}] must be a string")
    return errors


def validate_blueprint(data: dict) -> list[str]:
    """Validate blueprint input, return list of errors."""
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
    """Keep only the supported keys from a requirement set."""
    result = {}
    for key in ('resource_type', 'resource_count', 'capabilities'):
        if key in rs:
            result[key] = rs[key]
    return result


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
    bp_subparsers = blueprint_parser.add_subparsers(dest='blueprint_command', required=True)

    create_parser = bp_subparsers.add_parser('create', help='Create a new blueprint')
    create_parser.add_argument('input_file', nargs='?', type=argparse.FileType('r'), default=sys.stdin)

    bp_subparsers.add_parser('list', help='List all blueprints')

    get_parser = bp_subparsers.add_parser('get', help='Get a blueprint by UUID')
    get_parser.add_argument('uuid', help='Blueprint UUID')

    delete_parser = bp_subparsers.add_parser('delete', help='Delete a blueprint')
    delete_parser.add_argument('uuid', nargs='?', help='Blueprint UUID (for single delete)')
    delete_parser.add_argument('--names', help='Comma-separated list of blueprint names for batch delete')

    args = parser.parse_args()
    store = BlueprintStore(args.data_dir)

    if args.blueprint_command == 'create':
        try:
            data = json.load(args.input_file)
        except json.JSONDecodeError:
            output_error("validation_error", "input is not valid JSON")

        errors = validate_blueprint(data)
        if errors:
            output_error("validation_error", {"field": "blueprint", "errors": errors})

        name = data['name']
        if store.get('name', name):
            output_error("conflict", f"blueprint with name '{name}' already exists")

        blueprint = {
            "uuid": generate_uuid(),
            "name": name,
            "requirement_sets": [normalize_requirement_set(rs) for rs in data['requirement_sets']],
            "created_at": generate_timestamp(),
        }
        output_success(store.create(blueprint))

    elif args.blueprint_command == 'list':
        output_success(store.all())

    elif args.blueprint_command == 'get':
        blueprint = store.get('uuid', args.uuid)
        if not blueprint:
            output_error("not_found", f"blueprint with uuid '{args.uuid}' not found")
        output_success(blueprint)

    elif args.blueprint_command == 'delete':
        if args.names:
            deleted, unresolved = store.delete_by_names(args.names.split(','))
            if unresolved:
                output_error("not_found", {"unresolved": unresolved})
            output_success({"deleted": deleted})
        elif args.uuid:
            if not store.delete('uuid', args.uuid):
                output_error("not_found", f"blueprint with uuid '{args.uuid}' not found")
            output_success({"deleted": 1})
        else:
            output_error("validation_error", "either UUID or --names must be provided")


if __name__ == '__main__':
    main()
