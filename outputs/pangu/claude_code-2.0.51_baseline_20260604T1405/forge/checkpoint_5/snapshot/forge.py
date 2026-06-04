#!/usr/bin/env python3
"""Forge - Blueprint management for compute cluster resource allocation."""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Error(Exception):
    """Base exception for Forge errors."""

    def __init__(self, category: str, detail: str):
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}")


def output_error(category: str, detail: str) -> None:
    """Write error JSON to stdout and exit with code 1."""
    error_obj = {"error": category, "detail": detail}
    print(json.dumps(error_obj))
    sys.exit(1)


def output_success(data: Any) -> None:
    """Write success JSON to stdout and exit with code 0."""
    print(json.dumps(data))
    sys.exit(0)


def validate_requirement_set(rs: dict, path: str) -> list[str]:
    """Validate a requirement set object. Returns list of error messages."""
    errors = []
    supported_keys = {"resource_type", "resource_count", "capabilities"}

    # Check for unsupported keys
    for key in rs:
        if key not in supported_keys:
            errors.append(f"{path}.{key} is not a supported key")

    # Validate resource_type
    if "resource_type" not in rs:
        errors.append(f"{path}.resource_type is required")
    elif not isinstance(rs["resource_type"], str):
        errors.append(f"{path}.resource_type must be a string")

    # Validate resource_count
    if "resource_count" not in rs:
        errors.append(f"{path}.resource_count is required")
    else:
        rc = rs["resource_count"]
        # Boolean values are not integers
        if isinstance(rc, bool):
            errors.append(f"{path}.resource_count must be a positive integer")
        elif not isinstance(rc, int):
            errors.append(f"{path}.resource_count must be an integer")
        elif rc < 1:
            errors.append(f"{path}.resource_count must be positive")

    # Validate capabilities
    if "capabilities" in rs:
        caps = rs["capabilities"]
        if not isinstance(caps, list):
            errors.append(f"{path}.capabilities must be an array")
        else:
            for i, cap in enumerate(caps):
                if not isinstance(cap, str):
                    errors.append(f"{path}.capabilities[{i}] must be a string")

    return errors


def validate_blueprint_create(data: dict) -> list[str]:
    """Validate blueprint creation input. Returns list of error messages."""
    errors = []

    # Validate name
    if "name" not in data:
        errors.append("name is required")
    elif not isinstance(data["name"], str):
        errors.append("name must be a string")
    elif len(data["name"]) == 0:
        errors.append("name must be non-empty")

    # Validate requirement_sets
    if "requirement_sets" not in data:
        errors.append("requirement_sets is required")
    elif not isinstance(data["requirement_sets"], list):
        errors.append("requirement_sets must be an array")
    elif len(data["requirement_sets"]) == 0:
        errors.append("requirement_sets must be non-empty")

    # Validate each requirement set
    if isinstance(data.get("requirement_sets"), list):
        for i, rs in enumerate(data["requirement_sets"]):
            rs_errors = validate_requirement_set(rs, f"requirement_sets[{i}]")
            errors.extend(rs_errors)

    # Ignore extra top-level keys per spec
    return errors


class BlueprintStorage:
    """Handles persistence of blueprints to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.blueprints_file = self.data_dir / "blueprints.json"

    def _load(self) -> list[dict]:
        """Load all blueprints from storage."""
        if not self.blueprints_file.exists():
            return []
        with open(self.blueprints_file, "r") as f:
            return json.load(f)

    def _save(self, blueprints: list[dict]) -> None:
        """Save all blueprints to storage."""
        with open(self.blueprints_file, "w") as f:
            json.dump(blueprints, f, indent=2)

    def create(self, name: str, requirement_sets: list[dict]) -> dict:
        """Create a new blueprint."""
        blueprints = self._load()

        # Check for name conflict
        for bp in blueprints:
            if bp["name"] == name:
                output_error("conflict", f"Blueprint with name '{name}' already exists")

        # Generate UUID
        blueprint_uuid = str(uuid.uuid4()).lower()

        # Generate timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        blueprint = {
            "uuid": blueprint_uuid,
            "name": name,
            "requirement_sets": requirement_sets,
            "created_at": timestamp,
        }

        blueprints.append(blueprint)
        self._save(blueprints)

        return blueprint

    def list_all(self) -> list[dict]:
        """List all blueprints, sorted by created_at, then name, then uuid."""
        blueprints = self._load()
        # Sort: created_at ascending, then name ascending, then uuid ascending
        blueprints.sort(key=lambda bp: (bp["created_at"], bp["name"], bp["uuid"]))
        return blueprints

    def get_by_uuid(self, uuid_str: str) -> dict | None:
        """Get a blueprint by UUID."""
        blueprints = self._load()
        for bp in blueprints:
            if bp["uuid"] == uuid_str:
                return bp
        return None

    def get_by_name(self, name: str) -> dict | None:
        """Get a blueprint by name."""
        blueprints = self._load()
        for bp in blueprints:
            if bp["name"] == name:
                return bp
        return None

    def delete_by_uuid(self, uuid_str: str) -> int:
        """Delete a blueprint by UUID. Returns 1 if deleted, 0 if not found."""
        blueprints = self._load()
        for i, bp in enumerate(blueprints):
            if bp["uuid"] == uuid_str:
                del blueprints[i]
                self._save(blueprints)
                return 1
        return 0

    def delete_by_names(self, names: list[str]) -> tuple[int, list[str]]:
        """
        Delete blueprints by names. Returns (count, unresolved_names).
        If any names are unresolved, returns (0, unresolved_names).
        """
        blueprints = self._load()

        # Find all matching blueprints
        name_to_index = {}
        unresolved = []

        for name in names:
            found = False
            for i, bp in enumerate(blueprints):
                if bp["name"] == name:
                    if name in name_to_index:
                        # Duplicate name
                        unresolved.append(name)
                    else:
                        name_to_index[name] = i
                        found = True
                    break
            if not found:
                unresolved.append(name)

        # If any unresolved, return failure
        if unresolved:
            return 0, unresolved

        # Delete in reverse order to maintain indices
        indices_to_delete = sorted(name_to_index.values(), reverse=True)
        for idx in indices_to_delete:
            del blueprints[idx]

        self._save(blueprints)
        return len(names), []


class AllocationStorage:
    """Handles persistence of allocations to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.allocations_file = self.data_dir / "allocations.json"

    def _load(self) -> dict:
        """Load all allocations from storage as a dict by UUID."""
        if not self.allocations_file.exists():
            return {}
        with open(self.allocations_file, "r") as f:
            return json.load(f)

    def _save(self, allocations: dict) -> None:
        """Save all allocations to storage."""
        with open(self.allocations_file, "w") as f:
            json.dump(allocations, f, indent=2)

    def create(self, allocation_data: dict) -> dict:
        """Create a new allocation."""
        allocations = self._load()

        # Generate UUID
        allocation_uuid = str(uuid.uuid4()).lower()

        # Generate timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        allocation = {
            "uuid": allocation_uuid,
            "binding_status": "unbound",
            "assignment_id": None,
            **allocation_data,
            "created_at": timestamp,
        }

        allocations[allocation_uuid] = allocation
        self._save(allocations)

        return allocation

    def get_by_uuid(self, uuid_str: str) -> dict | None:
        """Get an allocation by UUID."""
        allocations = self._load()
        return allocations.get(uuid_str)

    def get_all(self) -> list[dict]:
        """Get all allocations."""
        allocations = self._load()
        return list(allocations.values())

    def bind_allocations(self, assignment_id: str, operations: dict) -> dict:
        """
        Bind or unbind allocations from an assignment.
        operations is a dict of {uuid: operation_type}.
        Returns a dict of modified allocation data keyed by uuid.
        """
        allocations = self._load()
        modified = {}

        for uuid_str, op_type in operations.items():
            if uuid_str not in allocations:
                continue
            alloc = allocations[uuid_str]
            if op_type == "add":
                alloc["binding_status"] = "bound"
                alloc["assignment_id"] = assignment_id
            elif op_type == "remove":
                alloc["binding_status"] = "unbound"
                alloc["assignment_id"] = None
            modified[uuid_str] = alloc

        if modified:
            self._save(allocations)

        return modified

    def delete_by_assignment(self, assignment_id: str) -> int:
        """
        Delete all allocations whose assignment_id matches the given UUID.
        Returns count of deleted allocations.
        """
        allocations = self._load()
        count = 0

        # Find allocations to delete
        to_delete = []
        for uuid_str, alloc in allocations.items():
            if alloc.get("assignment_id") == assignment_id:
                to_delete.append(uuid_str)
                count += 1

        for uuid_str in to_delete:
            del allocations[uuid_str]

        if count > 0:
            self._save(allocations)

        return count

    def delete_by_ids(self, uuids: list[str]) -> int:
        """
        Delete allocations by UUID list.
        Returns count of deleted allocations.
        """
        allocations = self._load()
        count = 0

        for uuid_str in uuids:
            if uuid_str in allocations:
                del allocations[uuid_str]
                count += 1

        if count > 0:
            self._save(allocations)

        return count

    def has_bound_allocations_for_assignment(self, assignment_id: str) -> bool:
        """Check if assignment has any bound allocations."""
        allocations = self._load()
        for alloc in allocations.values():
            if alloc.get("assignment_id") == assignment_id and alloc.get("binding_status") == "bound":
                return True
        return False


class UnitStorage:
    """Handles persistence of units to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.units_file = self.data_dir / "units.json"

    def _load(self) -> list[dict]:
        """Load all units from storage."""
        if not self.units_file.exists():
            return []
        with open(self.units_file, "r") as f:
            return json.load(f)

    def _save(self, units: list[dict]) -> None:
        """Save all units to storage."""
        with open(self.units_file, "w") as f:
            json.dump(units, f, indent=2)

    def create(self, data: dict) -> dict:
        """Create a new unit with validation."""
        units = self._load()

        # Validate system-assigned fields are not present
        system_fields = {"uuid", "status", "module_uuids", "created_at", "reserved_capacity"}
        provided_system_fields = system_fields.intersection(data.keys())
        if provided_system_fields:
            errors = [f"{field} is system-assigned and cannot be provided" for field in provided_system_fields]
            output_error("validation_error", "; ".join(errors))

        # Validate required fields
        errors = []

        # category - required, non-empty string
        if "category" not in data:
            errors.append("category is required")
        elif not isinstance(data["category"], str) or len(data["category"]) == 0:
            errors.append("category must be a non-empty string")

        # manufacturer - required, non-empty string
        if "manufacturer" not in data:
            errors.append("manufacturer is required")
        elif not isinstance(data["manufacturer"], str) or len(data["manufacturer"]) == 0:
            errors.append("manufacturer must be a non-empty string")

        # host - required, non-empty string
        if "host" not in data:
            errors.append("host is required")
        elif not isinstance(data["host"], str) or len(data["host"]) == 0:
            errors.append("host must be a non-empty string")

        # capacity - required, non-negative integer
        if "capacity" not in data:
            errors.append("capacity is required")
        else:
            # Boolean values are not integers
            if isinstance(data["capacity"], bool):
                errors.append("capacity must be a non-negative integer")
            elif not isinstance(data["capacity"], int):
                errors.append("capacity must be a non-negative integer")
            elif data["capacity"] < 0:
                errors.append("capacity must be a non-negative integer")

        # parent_uuid - optional, but if provided must be non-empty string and reference existing unit
        if "parent_uuid" in data:
            parent_uuid = data["parent_uuid"]
            if not isinstance(parent_uuid, str) or len(parent_uuid) == 0:
                errors.append("parent_uuid must be a non-empty string if provided")
            elif not self._unit_exists(parent_uuid):
                # Check if parent_uuid is well-formed UUID format
                if self._is_well_formed_uuid(parent_uuid):
                    # Well-formed UUID but unit doesn't exist → not_found
                    output_error("not_found", f"Parent unit with UUID '{parent_uuid}' not found")
                else:
                    # Not well-formed UUID format → validation_error
                    errors.append("parent_uuid must be a valid UUID format")

        # capabilities - optional array of strings
        capabilities = []
        if "capabilities" in data:
            caps = data["capabilities"]
            if not isinstance(caps, list):
                errors.append("capabilities must be an array")
            else:
                for i, cap in enumerate(caps):
                    if not isinstance(cap, str):
                        errors.append(f"capabilities[{i}] must be a string")
                # Sort capabilities ascending lexicographic
                capabilities = sorted(caps)

        if errors:
            output_error("validation_error", "; ".join(errors))

        # Generate UUID
        unit_uuid = str(uuid.uuid4()).lower()

        # Generate timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        unit = {
            "uuid": unit_uuid,
            "category": data["category"],
            "manufacturer": data["manufacturer"],
            "host": data["host"],
            "status": "active",
            "parent_uuid": data.get("parent_uuid"),
            "module_uuids": [],
            "capacity": data["capacity"],
            "capabilities": capabilities,
            "reserved_capacity": 0,
            "created_at": timestamp,
        }

        units.append(unit)
        self._save(units)

        return unit

    def _unit_exists(self, uuid_str: str) -> bool:
        """Check if a unit with the given UUID exists."""
        units = self._load()
        for unit in units:
            if unit["uuid"] == uuid_str:
                return True
        return False

    def list_all(self, category_filter: str = None, manufacturer_filter: str = None,
                 host_filter: str = None, generic_filters: list = None) -> list[dict]:
        """List all units with filtering and ordering."""
        units = self._load()

        # Enrich units with new fields if missing
        for unit in units:
            self._enrich_unit(unit)

        # Apply named filters
        if category_filter is not None:
            units = [u for u in units if u["category"] == category_filter]
        if manufacturer_filter is not None:
            units = [u for u in units if u["manufacturer"] == manufacturer_filter]
        if host_filter is not None:
            units = [u for u in units if u["host"] == host_filter]

        # Apply generic filters
        if generic_filters:
            for filter_expr in generic_filters:
                if "=" not in filter_expr:
                    output_error("validation_error", f"Invalid generic filter format: '{filter_expr}'")

                key, value = filter_expr.split("=", 1)

                # Handle null values
                if value == "null":
                    units = [u for u in units if u.get(key) is None]
                elif key in units[0] if units else False:
                    # Apply filter based on field type
                    units = [u for u in units if self._matches_filter(u, key, value)]
                else:
                    # Nonexistent field name - match no units
                    units = []

        # Sort: host ascending, category ascending, created_at ascending, uuid ascending
        units.sort(key=lambda u: (u["host"], u["category"], u["created_at"], u["uuid"]))

        return units

    def _matches_filter(self, unit: dict, key: str, value: str) -> bool:
        """Check if a unit matches a generic filter."""
        if key not in unit:
            return False

        field_value = unit[key]

        # For null fields, they should have been handled by the caller
        if field_value is None:
            return False

        # Convert to string for comparison
        return str(field_value) == value

    def _is_well_formed_uuid(self, uuid_str: str) -> bool:
        """Check if string is a well-formed UUID (lowercase, hyphenated)."""
        # UUID format: 8-4-4-4-12 hex digits
        if not isinstance(uuid_str, str):
            return False
        parts = uuid_str.split('-')
        if len(parts) != 5:
            return False
        if len(parts[0]) != 8 or len(parts[1]) != 4 or len(parts[2]) != 4 or len(parts[3]) != 4 or len(parts[4]) != 12:
            return False
        # Each part must be hex digits only
        for part in parts:
            if not all(c in '0123456789abcdef' for c in part):
                return False
        return True

    def _enrich_unit(self, unit: dict) -> None:
        """Add new fields to existing units for backward compatibility."""
        if "capabilities" not in unit:
            unit["capabilities"] = []
        if "reserved_capacity" not in unit:
            # For units created before this feature, derive reserved_capacity from status
            if unit["status"] == "inactive":
                unit["reserved_capacity"] = unit.get("capacity", 0)
            else:
                unit["reserved_capacity"] = 0

    def get_by_uuid(self, uuid_str: str) -> dict | None:
        """Get a unit by UUID."""
        units = self._load()
        for unit in units:
            if unit["uuid"] == uuid_str:
                self._enrich_unit(unit)
                return unit
        return None

    def get_active_capabilities(self) -> list[str]:
        """Get sorted unique capabilities from all active units."""
        units = self._load()
        # Enrich units before checking capabilities
        for unit in units:
            self._enrich_unit(unit)
        capabilities = set()
        for unit in units:
            if unit["status"] == "active":
                capabilities.update(unit.get("capabilities", []))
        return sorted(capabilities)

    def activate(self, uuid_str: str) -> dict:
        """Activate a unit by UUID. Returns the updated unit."""
        units = self._load()
        for unit in units:
            if unit["uuid"] == uuid_str:
                self._enrich_unit(unit)
                if unit["status"] == "active":
                    output_error("invalid_transition", f"Unit '{uuid_str}' is already active")
                unit["status"] = "active"
                unit["reserved_capacity"] = 0
                # Capabilities are restored - they should already be present
                units_sorted = self._sort_units(units)
                self._save(units_sorted)
                return unit
        output_error("not_found", f"Unit with UUID '{uuid_str}' not found")

    def deactivate(self, uuid_str: str) -> dict:
        """Deactivate a unit by UUID. Returns the updated unit."""
        units = self._load()
        for unit in units:
            if unit["uuid"] == uuid_str:
                self._enrich_unit(unit)
                if unit["status"] == "inactive":
                    output_error("invalid_transition", f"Unit '{uuid_str}' is already inactive")
                unit["status"] = "inactive"
                unit["reserved_capacity"] = unit["capacity"]
                # Capabilities are removed - set to empty but keep for reference
                units_sorted = self._sort_units(units)
                self._save(units_sorted)
                return unit
        output_error("not_found", f"Unit with UUID '{uuid_str}' not found")

    def _sort_units(self, units: list[dict]) -> list[dict]:
        """Sort units by host, category, created_at, uuid."""
        units.sort(key=lambda u: (u["host"], u["category"], u["created_at"], u["uuid"]))
        return units


def cmd_unit_create(args, storage: UnitStorage) -> None:
    """Handle unit create command."""
    # Read stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    unit = storage.create(input_data)
    output_success(unit)


def cmd_unit_list(args, storage: UnitStorage) -> None:
    """Handle unit list command."""
    units = storage.list_all(
        category_filter=args.category,
        manufacturer_filter=args.manufacturer,
        host_filter=args.host,
        generic_filters=args.filter
    )
    output_success(units)


def cmd_unit_get(args, storage: UnitStorage) -> None:
    """Handle unit get command."""
    unit = storage.get_by_uuid(args.uuid)
    if unit is None:
        output_error("not_found", f"Unit with UUID '{args.uuid}' not found")
    output_success(unit)


def cmd_unit_activate(args, storage: UnitStorage) -> None:
    """Handle unit activate command."""
    unit = storage.activate(args.uuid)
    output_success(unit)


def cmd_unit_deactivate(args, storage: UnitStorage) -> None:
    """Handle unit deactivate command."""
    unit = storage.deactivate(args.uuid)
    output_success(unit)


def cmd_allocation_create(args, storage) -> None:
    """Handle allocation create command."""
    # Read stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    allocation = storage.create(input_data)
    output_success(allocation)


def cmd_allocation_list(args, storage) -> None:
    """Handle allocation list command."""
    allocations = storage.get_all()
    output_success(allocations)


def cmd_allocation_get(args, storage) -> None:
    """Handle allocation get command."""
    allocation = storage.get_by_uuid(args.uuid)
    if allocation is None:
        output_error("not_found", f"Allocation with UUID '{args.uuid}' not found")
    output_success(allocation)


def cmd_allocation_bind(args, storage) -> None:
    """Handle allocation bind command."""
    # Read stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    # Validate assignment_id (Priority 1)
    assignment_id = input_data.get("assignment_id")
    if not assignment_id:
        output_error("validation_error", "assignment_id is required")

    # Validate allocations (Priority 1)
    allocations_obj = input_data.get("allocations")
    if allocations_obj is None:
        output_error("validation_error", "allocations is required")
    if not isinstance(allocations_obj, dict):
        output_error("validation_error", "allocations must be an object")
    if len(allocations_obj) == 0:
        output_error("validation_error", "allocations must contain at least one entry")

    # Check for duplicate assignment guard (Priority 4)
    has_add_operation = any(op == "add" for op in allocations_obj.values())
    if has_add_operation:
        if storage.has_bound_allocations_for_assignment(assignment_id):
            output_error("duplicate_assignment", f"Assignment '{assignment_id}' already has bound allocations")

    # Validate operations (Priority 2) and allocation existence (Priority 3)
    validation_errors = []
    valid_operations = {"add", "remove"}
    operations_to_apply = {}

    for uuid_str, operation in allocations_obj.items():
        # Check operation validity
        if operation not in valid_operations:
            validation_errors.append(f"Invalid operation '{operation}' for allocation '{uuid_str}'")
        # Check allocation exists
        if storage.get_by_uuid(uuid_str) is None:
            validation_errors.append(f"Allocation '{uuid_str}' not found")
        operations_to_apply[uuid_str] = operation

    if validation_errors:
        output_error("validation_error", "; ".join(validation_errors))

    # Apply modifications atomically
    storage.bind_allocations(assignment_id, operations_to_apply)

    output_success({"status": "accepted"})


def cmd_ledger_capabilities(args, storage: UnitStorage) -> None:
    """Handle ledger capabilities command."""
    capabilities = storage.get_active_capabilities()
    output_success(capabilities)


def cmd_allocation_delete(args, storage) -> None:
    """Handle allocation delete command."""
    # Validate exactly one of --assignment or --ids is provided
    if args.assignment and args.ids:
        output_error("validation_error", "Exactly one of --assignment or --ids must be provided")

    if args.assignment:
        count = storage.delete_by_assignment(args.assignment)
        output_success({"deleted": count})

    elif args.ids:
        uuids = [uid.strip() for uid in args.ids.split(",")]
        # Check all exist first (atomic check - Priority 3)
        for uuid_str in uuids:
            if storage.get_by_uuid(uuid_str) is None:
                output_error("not_found", f"Allocation '{uuid_str}' not found")
        # All exist, delete them
        count = storage.delete_by_ids(uuids)
        output_success({"deleted": count})

    else:
        output_error("validation_error", "Exactly one of --assignment or --ids must be provided")


def main():
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Manage blueprints for compute cluster resource allocation",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory for persistent state",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Blueprint subcommands
    blueprint_parser = subparsers.add_parser("blueprint", help="Manage blueprints")
    blueprint_subparsers = blueprint_parser.add_subparsers(
        dest="blueprint_command", required=True
    )

    # blueprint create
    create_parser = blueprint_subparsers.add_parser(
        "create", help="Create a new blueprint from stdin"
    )
    create_parser.set_defaults(func=cmd_create)

    # blueprint list
    list_parser = blueprint_subparsers.add_parser(
        "list", help="List all blueprints"
    )
    list_parser.set_defaults(func=cmd_list)

    # blueprint get
    get_parser = blueprint_subparsers.add_parser("get", help="Get a blueprint by UUID")
    get_parser.add_argument("uuid", help="Blueprint UUID")
    get_parser.set_defaults(func=cmd_get)

    # blueprint delete
    delete_parser = blueprint_subparsers.add_parser(
        "delete", help="Delete a blueprint by UUID or name(s)"
    )
    delete_parser.add_argument("uuid", nargs="?", help="Blueprint UUID")
    delete_parser.add_argument(
        "--names", help="Comma-separated list of blueprint names"
    )
    delete_parser.set_defaults(func=cmd_delete)

    # Allocation subcommands
    allocation_parser = subparsers.add_parser("allocation", help="Manage allocations")
    allocation_subparsers = allocation_parser.add_subparsers(
        dest="allocation_command", required=True
    )

    # allocation create
    allocation_create_parser = allocation_subparsers.add_parser(
        "create", help="Create a new allocation from stdin"
    )
    allocation_create_parser.set_defaults(func=cmd_allocation_create)

    # allocation list
    allocation_list_parser = allocation_subparsers.add_parser(
        "list", help="List all allocations"
    )
    allocation_list_parser.set_defaults(func=cmd_allocation_list)

    # allocation get
    allocation_get_parser = allocation_subparsers.add_parser(
        "get", help="Get an allocation by UUID"
    )
    allocation_get_parser.add_argument("uuid", help="Allocation UUID")
    allocation_get_parser.set_defaults(func=cmd_allocation_get)

    # allocation delete
    allocation_delete_parser = allocation_subparsers.add_parser(
        "delete", help="Delete allocations by assignment or ID list"
    )
    allocation_delete_parser.add_argument(
        "--assignment", help="Delete all allocations with this assignment ID"
    )
    allocation_delete_parser.add_argument(
        "--ids", help="Delete allocations by comma-separated UUID list"
    )
    allocation_delete_parser.set_defaults(func=cmd_allocation_delete)

    # Ledger subcommands
    ledger_parser = subparsers.add_parser("ledger", help="Manage ledger")
    ledger_subparsers = ledger_parser.add_subparsers(dest="ledger_command", required=True)

    # ledger capabilities
    ledger_capabilities_parser = ledger_subparsers.add_parser(
        "capabilities", help="List all capabilities from active units"
    )
    ledger_capabilities_parser.set_defaults(func=cmd_ledger_capabilities)

    # Unit subcommands
    unit_parser = subparsers.add_parser("unit", help="Manage units")
    unit_subparsers = unit_parser.add_subparsers(
        dest="unit_command", required=True
    )

    # unit create
    unit_create_parser = unit_subparsers.add_parser(
        "create", help="Create a new unit from stdin"
    )
    unit_create_parser.set_defaults(func=cmd_unit_create)

    # unit list
    unit_list_parser = unit_subparsers.add_parser(
        "list", help="List all units"
    )
    unit_list_parser.add_argument(
        "--category", help="Exact match on category (case-sensitive)"
    )
    unit_list_parser.add_argument(
        "--manufacturer", help="Exact match on manufacturer (case-sensitive)"
    )
    unit_list_parser.add_argument(
        "--host", help="Exact match on host (case-sensitive)"
    )
    unit_list_parser.add_argument(
        "--filter", action="append", dest="filter",
        help="Generic field filter; may appear multiple times"
    )
    unit_list_parser.set_defaults(func=cmd_unit_list)

    # unit get
    unit_get_parser = unit_subparsers.add_parser("get", help="Get a unit by UUID")
    unit_get_parser.add_argument("uuid", help="Unit UUID")
    unit_get_parser.set_defaults(func=cmd_unit_get)

    # unit activate
    unit_activate_parser = unit_subparsers.add_parser(
        "activate", help="Activate a unit by UUID"
    )
    unit_activate_parser.add_argument("uuid", help="Unit UUID")
    unit_activate_parser.set_defaults(func=cmd_unit_activate)

    # unit deactivate
    unit_deactivate_parser = unit_subparsers.add_parser(
        "deactivate", help="Deactivate a unit by UUID"
    )
    unit_deactivate_parser.add_argument("uuid", help="Unit UUID")
    unit_deactivate_parser.set_defaults(func=cmd_unit_deactivate)

    args = parser.parse_args()

    # Validate data-dir exists (will be created by storage if needed)
    try:
        if hasattr(args, 'blueprint_command') or args.command == "blueprint":
            storage = BlueprintStorage(args.data_dir)
        elif hasattr(args, 'allocation_command') or args.command == "allocation":
            storage = AllocationStorage(args.data_dir)
        elif hasattr(args, 'unit_command') or args.command == "unit":
            storage = UnitStorage(args.data_dir)
        elif hasattr(args, 'ledger_command') or args.command == "ledger":
            # ledger capabilities uses UnitStorage
            storage = UnitStorage(args.data_dir)
        else:
            output_error("validation_error", "Unknown command")
    except Exception as e:
        output_error("validation_error", f"Invalid data directory: {e}")

    # Execute the command
    args.func(args, storage)


def cmd_create(args, storage: BlueprintStorage) -> None:
    """Handle blueprint create command."""
    # Read stdin
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    # Validate input
    errors = validate_blueprint_create(input_data)
    if errors:
        output_error("validation_error", "; ".join(errors))

    # Extract name and requirement_sets
    name = input_data["name"]
    requirement_sets = input_data["requirement_sets"]

    # Create blueprint
    blueprint = storage.create(name, requirement_sets)
    output_success(blueprint)


def cmd_list(args, storage: BlueprintStorage) -> None:
    """Handle blueprint list command."""
    blueprints = storage.list_all()
    output_success(blueprints)


def cmd_get(args, storage: BlueprintStorage) -> None:
    """Handle blueprint get command."""
    blueprint = storage.get_by_uuid(args.uuid)
    if blueprint is None:
        output_error("not_found", f"Blueprint with UUID '{args.uuid}' not found")
    output_success(blueprint)


def cmd_delete(args, storage: BlueprintStorage) -> None:
    """Handle blueprint delete command."""
    # Determine if single UUID or batch by names
    if args.names is not None:
        # Batch delete by names
        names = args.names.split(",")
        # Empty CSV members are treated as unresolved names
        count, unresolved = storage.delete_by_names(names)
        if unresolved:
            output_error("not_found", f"Unresolved names: {', '.join(unresolved)}")
        output_success({"deleted": count})
    elif args.uuid is not None:
        # Single delete by UUID
        count = storage.delete_by_uuid(args.uuid)
        if count == 0:
            output_error("not_found", f"Blueprint with UUID '{args.uuid}' not found")
        output_success({"deleted": count})
    else:
        output_error("validation_error", "Either --names or UUID must be provided")


if __name__ == "__main__":
    main()
