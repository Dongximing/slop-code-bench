#!/usr/bin/env python3
"""
Forge - A command-line tool for managing blueprints in a compute cluster.
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class ForgeError(Exception):
    """Base exception for Forge errors."""
    def __init__(self, error_type: str, detail: Any):
        self.error_type = error_type
        self.detail = detail
        super().__init__(json.dumps({"error": error_type, "detail": detail}))


class ValidationError(ForgeError):
    """Raised when input validation fails."""
    def __init__(self, detail: Any):
        super().__init__("validation_error", detail)


class NotFoundError(ForgeError):
    """Raised when a resource is not found."""
    def __init__(self, detail: Any):
        super().__init__("not_found", detail)


class ConflictError(ForgeError):
    """Raised when a conflict occurs (e.g., duplicate name)."""
    def __init__(self, detail: Any):
        super().__init__("conflict", detail)


class BlueprintPersistence:
    """Handles persistence of blueprints to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.data_dir / "blueprints.json"

    def _load(self) -> list:
        """Load all blueprints from the JSON file."""
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []

    def _save(self, blueprints: list) -> None:
        """Save all blueprints to the JSON file."""
        with open(self.file_path, 'w') as f:
            json.dump(blueprints, f, indent=2)

    def get_all(self) -> list:
        """Get all blueprints."""
        return self._load()

    def get_by_uuid(self, uuid: str) -> Optional[dict]:
        """Get a blueprint by UUID."""
        blueprints = self._load()
        for bp in blueprints:
            if bp.get('uuid') == uuid:
                return bp
        return None

    def get_by_name(self, name: str) -> Optional[dict]:
        """Get a blueprint by name."""
        blueprints = self._load()
        for bp in blueprints:
            if bp.get('name') == name:
                return bp
        return None

    def create(self, blueprint: dict) -> dict:
        """Create a new blueprint."""
        blueprints = self._load()
        blueprints.append(blueprint)
        self._save(blueprints)
        return blueprint

    def delete_by_uuid(self, uuid: str) -> bool:
        """Delete a blueprint by UUID. Returns True if deleted, False if not found."""
        blueprints = self._load()
        original_len = len(blueprints)
        blueprints = [bp for bp in blueprints if bp.get('uuid') != uuid]
        if len(blueprints) != original_len:
            self._save(blueprints)
            return True
        return False

    def delete_by_names(self, names: list) -> tuple:
        """
        Delete blueprints by names.
        Returns (deleted_count, unresolved_names).
        """
        blueprints = self._load()
        name_to_uuid = {bp['name']: bp['uuid'] for bp in blueprints}
        unresolved = [n for n in names if n not in name_to_uuid]

        if unresolved:
            return 0, unresolved

        remaining = [bp for bp in blueprints if bp['name'] not in names]
        deleted_count = len(blueprints) - len(remaining)
        self._save(remaining)
        return deleted_count, []


class BlueprintValidator:
    """Validates blueprint and requirement set data."""

    @staticmethod
    def validate_requirement_set(rs: dict, allow_unsupported_keys: bool = True) -> list:
        """Validate a requirement set. Returns list of error messages."""
        errors = []
        supported_keys = {'resource_type', 'resource_count', 'capabilities'}

        # Check for unsupported keys
        if allow_unsupported_keys:
            for key in rs:
                if key not in supported_keys:
                    errors.append(f"unsupported key '{key}' in requirement set")

        # Validate resource_type
        if 'resource_type' not in rs:
            errors.append("requirement set missing 'resource_type'")
        elif not isinstance(rs['resource_type'], str):
            errors.append("requirement set 'resource_type' must be a string")

        # Validate resource_count
        if 'resource_count' not in rs:
            errors.append("requirement set missing 'resource_count'")
        elif not isinstance(rs['resource_count'], int) or rs['resource_count'] < 1:
            errors.append("requirement set 'resource_count' must be a positive integer")
        elif isinstance(rs['resource_count'], bool):
            errors.append("requirement set 'resource_count' must be a positive integer (not a boolean)")

        # Validate capabilities if present
        if 'capabilities' in rs:
            if not isinstance(rs['capabilities'], list):
                errors.append("requirement set 'capabilities' must be an array of strings")
            else:
                for i, cap in enumerate(rs['capabilities']):
                    if not isinstance(cap, str):
                        errors.append(f"requirement set 'capabilities[{i}]' must be a string")

        return errors

    @staticmethod
    def validate_blueprint_input(data: dict, allow_unsupported_keys: bool = False) -> list:
        """Validate blueprint input. Returns list of error messages."""
        errors = []

        # Validate name
        if 'name' not in data:
            errors.append("blueprint missing 'name'")
        elif not isinstance(data['name'], str) or len(data['name'].strip()) == 0:
            errors.append("blueprint 'name' must be a non-empty string")

        # Validate requirement_sets
        if 'requirement_sets' not in data:
            errors.append("blueprint missing 'requirement_sets'")
        elif not isinstance(data['requirement_sets'], list) or len(data['requirement_sets']) == 0:
            errors.append("blueprint 'requirement_sets' must be a non-empty array")
        else:
            for i, rs in enumerate(data['requirement_sets']):
                if not isinstance(rs, dict):
                    errors.append(f"requirement_sets[{i}] must be an object")
                else:
                    rs_errors = BlueprintValidator.validate_requirement_set(rs, allow_unsupported_keys)
                    for e in rs_errors:
                        errors.append(f"requirement_sets[{i}]: {e}")

        return errors

    @staticmethod
    def normalize_requirement_set(rs: dict) -> dict:
        """Normalize a requirement set, keeping only supported keys."""
        result = {}
        if 'resource_type' in rs:
            result['resource_type'] = rs['resource_type']
        if 'resource_count' in rs:
            result['resource_count'] = rs['resource_count']
        if 'capabilities' in rs and rs['capabilities']:
            result['capabilities'] = rs['capabilities']
        return result


class ForgeCLI:
    """Main CLI class for Forge commands."""

    def __init__(self, data_dir: str):
        self.persistence = BlueprintPersistence(data_dir)

    def _generate_timestamp(self) -> str:
        """Generate current timestamp in ISO 8601 UTC format."""
        return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def _generate_uuid(self) -> str:
        """Generate a lowercase hyphenated UUID v4."""
        return str(uuid.uuid4()).lower()

    def _sort_key(self, bp: dict) -> tuple:
        """Generate sort key for ordering blueprints."""
        created_at = bp.get('created_at', '')
        name = bp.get('name', '')
        bp_uuid = bp.get('uuid', '')
        return (created_at, name, bp_uuid)

    def blueprint_create(self, input_data: dict) -> dict:
        """Create a new blueprint."""
        # Validate input
        errors = BlueprintValidator.validate_blueprint_input(input_data)
        if errors:
            raise ValidationError([{
                "field": "blueprint",
                "errors": errors
            }])

        name = input_data['name']

        # Check for name conflict
        if self.persistence.get_by_name(name):
            raise ConflictError(f"blueprint with name '{name}' already exists")

        # Normalize requirement sets
        requirement_sets = [
            BlueprintValidator.normalize_requirement_set(rs)
            for rs in input_data['requirement_sets']
        ]

        # Create blueprint with auto-assigned fields
        blueprint = {
            "uuid": self._generate_uuid(),
            "name": name,
            "requirement_sets": requirement_sets,
            "created_at": self._generate_timestamp()
        }

        return self.persistence.create(blueprint)

    def blueprint_list(self) -> list:
        """List all blueprints."""
        blueprints = self.persistence.get_all()
        return sorted(blueprints, key=self._sort_key)

    def blueprint_get(self, uuid: str) -> dict:
        """Get a blueprint by UUID."""
        blueprint = self.persistence.get_by_uuid(uuid)
        if not blueprint:
            raise NotFoundError(f"blueprint with uuid '{uuid}' not found")
        return blueprint

    def blueprint_delete(self, uuid: str) -> dict:
        """Delete a blueprint by UUID."""
        if not self.persistence.delete_by_uuid(uuid):
            raise NotFoundError(f"blueprint with uuid '{uuid}' not found")
        return {"deleted": 1}

    def blueprint_delete_names(self, names: list) -> dict:
        """Delete blueprints by names (batch delete)."""
        deleted_count, unresolved = self.persistence.delete_by_names(names)
        if unresolved:
            raise NotFoundError({"unresolved": unresolved})
        return {"deleted": deleted_count}


def parse_args(args: list) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Forge - CLI tool for managing blueprints",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        '--data-dir',
        required=True,
        help="Directory for persistent state"
    )

    subparsers = parser.add_subparsers(dest='command', required=True)

    # Blueprint subcommands
    blueprint_parser = subparsers.add_parser('blueprint', help='Blueprint management commands')
    blueprint_subparsers = blueprint_parser.add_subparsers(dest='blueprint_command', required=True)

    # blueprint create
    create_parser = blueprint_subparsers.add_parser('create', help='Create a new blueprint')
    create_parser.add_argument(
        'input_file',
        nargs='?',
        type=argparse.FileType('r'),
        default=sys.stdin,
        help="Input JSON file (default: stdin)"
    )

    # blueprint list
    blueprint_subparsers.add_parser('list', help='List all blueprints')

    # blueprint get
    get_parser = blueprint_subparsers.add_parser('get', help='Get a blueprint by UUID')
    get_parser.add_argument('uuid', help='Blueprint UUID')

    # blueprint delete (single)
    delete_parser = blueprint_subparsers.add_parser('delete', help='Delete a blueprint')
    delete_parser.add_argument('uuid', nargs='?', help='Blueprint UUID (for single delete)')
    delete_parser.add_argument(
        '--names',
        help='Comma-separated list of blueprint names for batch delete'
    )

    return parser.parse_args(args)


def output_success(data: Any) -> None:
    """Output successful result as JSON."""
    print(json.dumps(data))
    sys.exit(0)


def output_error(error_type: str, detail: Any) -> None:
    """Output error and exit with code 1."""
    print(json.dumps({"error": error_type, "detail": detail}))
    sys.exit(1)


def main():
    """Main entry point."""
    try:
        args = parse_args(sys.argv[1:])

        forge = ForgeCLI(args.data_dir)

        if args.blueprint_command == 'create':
            # Read input from stdin or file
            try:
                input_data = json.load(args.input_file)
            except json.JSONDecodeError:
                output_error("validation_error", "stdin is not valid JSON")

            result = forge.blueprint_create(input_data)
            output_success(result)

        elif args.blueprint_command == 'list':
            result = forge.blueprint_list()
            output_success(result)

        elif args.blueprint_command == 'get':
            result = forge.blueprint_get(args.uuid)
            output_success(result)

        elif args.blueprint_command == 'delete':
            if args.names is not None:
                # Batch delete by names
                names = args.names.split(',')
                result = forge.blueprint_delete_names(names)
                output_success(result)
            elif args.uuid is not None:
                # Single delete by UUID
                result = forge.blueprint_delete(args.uuid)
                output_success(result)
            else:
                output_error("validation_error", "either UUID or --names must be provided")

    except ForgeError as e:
        output_error(e.error_type, e.detail)
    except Exception as e:
        output_error("validation_error", str(e))


if __name__ == '__main__':
    main()
