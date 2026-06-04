#!/usr/bin/env python3
"""Forge - Revision negotiation and feature gating implementation."""

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None


class Error(Exception):
    """Base exception for Forge errors."""

    def __init__(self, category: str, detail: str):
        self.category = category
        self.detail = detail
        super().__init__(f"{category}: {detail}")


# Supported revision range
MIN_REVISION = (1, 0)
MAX_REVISION = (1, 2)

# Map revision to feature availability
def has_feature(min_revision: tuple[int, int], negotiated: tuple[int, int]) -> bool:
    """Check if a feature is available at the negotiated revision."""
    return negotiated >= min_revision


FEATURE_BLUEPRINT_GET_BY_NAME = (1, 2)
FEATURE_TENANT_ID_IN_ALLOCATION_BIND = (1, 1)


def parse_revision(rev_str: str) -> tuple[int, int] | None:
    """Parse a revision string into (major, minor) tuple.

    Returns None if the string does not match the format <major>.<minor>
    where both components are non-negative integers.
    """
    if not isinstance(rev_str, str):
        return None
    # Must match exactly: digits.digits (no leading sign, no extra chars)
    match = re.fullmatch(r'(\d+)\.(\d+)', rev_str)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    return (major, minor)


def normalize_revision(major: int, minor: int) -> str:
    """Normalize revision components to canonical string without leading zeros."""
    return f"{major}.{minor}"


def negotiate_revision(rev_str: str | None) -> tuple[tuple[int, int], str]:
    """Negotiate the revision from the flag value.

    Returns (major, minor) tuple and the canonical string representation.
    Raises Error for parse_error or unsupported_revision.
    """
    if rev_str is None:
        return MIN_REVISION, normalize_revision(*MIN_REVISION)

    if rev_str == "latest":
        return MAX_REVISION, normalize_revision(*MAX_REVISION)

    parsed = parse_revision(rev_str)
    if parsed is None:
        raise Error("parse_error", f"Invalid revision format: '{rev_str}'")

    if parsed < MIN_REVISION or parsed > MAX_REVISION:
        raise Error(
            "unsupported_revision",
            f"Revision '{rev_str}' is not supported. "
            f"Supported range: {MIN_REVISION[0]}.{MIN_REVISION[0]}-{MAX_REVISION[0]}.{MAX_REVISION[1]}"
        )

    return parsed, normalize_revision(*parsed)


def output_error(category: str, detail: str, revision: str = "1.0") -> None:
    """Write error JSON to stdout and exit with code 1."""
    error_obj = {"error": category, "detail": detail, "revision": revision}
    print(json.dumps(error_obj))
    sys.exit(1)


def output_success(data: Any) -> None:
    """Write success JSON to stdout and exit with code 0."""
    print(json.dumps(data))
    sys.exit(0)


def output_version():
    """Output version discovery response."""
    result = {
        "max_revision": f"{MAX_REVISION[0]}.{MAX_REVISION[1]}",
        "min_revision": f"{MIN_REVISION[0]}.{MIN_REVISION[1]}"
    }
    print(json.dumps(result))
    sys.exit(0)


def wrap_collection(resource_type: str, items: list, revision: str) -> dict:
    """Wrap a collection in a resource-key envelope."""
    return {resource_type: items, "revision": revision}


def add_revision_to_response(data: dict, revision: str) -> dict:
    """Add revision field to a single-resource response."""
    return {**data, "revision": revision}


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
                if is_well_formed_uuid(parent_uuid):
                    # Well-formed UUID but unit doesn't exist -> not_found
                    output_error("not_found", f"Parent unit with UUID '{parent_uuid}' not found")
                else:
                    # Not well-formed UUID format -> validation_error
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


def is_well_formed_uuid(uuid_str: str) -> bool:
    """Check if string is a well-formed lowercase hex UUID (8-4-4-4-12)."""
    if not isinstance(uuid_str, str):
        return False
    parts = uuid_str.split('-')
    if len(parts) != 5:
        return False
    if len(parts[0]) != 8 or len(parts[1]) != 4 or len(parts[2]) != 4 or len(parts[3]) != 4 or len(parts[4]) != 12:
        return False
    for part in parts:
        if not all(c in '0123456789abcdef' for c in part):
            return False
    return True


class ModuleStorage:
    """Handles persistence of modules to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.modules_file = self.data_dir / "modules.json"

    def _load(self) -> list[dict]:
        """Load all modules from storage."""
        if not self.modules_file.exists():
            return []
        with open(self.modules_file, "r") as f:
            return json.load(f)

    def _save(self, modules: list[dict]) -> None:
        """Save all modules to storage."""
        with open(self.modules_file, "w") as f:
            json.dump(modules, f, indent=2)

    def create(self, unit_uuid: str, component_type: str, firmware_image: str = None) -> dict:
        """Create a new module."""
        modules = self._load()

        # Validate unit_uuid exists (check via UnitStorage)
        unit_storage = UnitStorage(self.data_dir)
        unit = unit_storage.get_by_uuid(unit_uuid)
        if unit is None:
            output_error("not_found", f"Unit with UUID '{unit_uuid}' not found")

        # Validate component_type
        if not component_type or not isinstance(component_type, str):
            output_error("validation_error", "component_type must be a non-empty string")

        # Validate firmware_image format if provided and non-null
        if firmware_image is not None:
            if not is_well_formed_uuid(firmware_image):
                output_error("invalid_image", f"Invalid firmware image UUID format: '{firmware_image}'")

        # Generate UUID and timestamp
        module_uuid = str(uuid.uuid4()).lower()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        module = {
            "uuid": module_uuid,
            "unit_uuid": unit_uuid,
            "component_type": component_type,
            "firmware_image": None,  # Start with null until first flash
            "created_at": timestamp,
        }

        # If firmware_image was provided and valid, set it
        if firmware_image is not None:
            module["firmware_image"] = firmware_image

        modules.append(module)
        self._save(modules)

        # Update parent unit's module_uuids (sorted ascending lexicographic)
        unit_module_uuids = sorted(unit.get("module_uuids", []) + [module_uuid])
        unit["module_uuids"] = unit_module_uuids
        unit_storage._save(unit_storage._load())  # Save the updated unit

        return module

    def list_all(self, unit_filter: str = None, generic_filters: list = None) -> list[dict]:
        """List all modules with filtering and ordering."""
        modules = self._load()

        # Apply unit filter
        if unit_filter is not None:
            modules = [m for m in modules if m["unit_uuid"] == unit_filter]

        # Apply generic filters
        if generic_filters:
            for filter_expr in generic_filters:
                if "=" not in filter_expr:
                    output_error("validation_error", f"Invalid generic filter format: '{filter_expr}'")
                key, value = filter_expr.split("=", 1)
                if value == "null":
                    modules = [m for m in modules if m.get(key) is None]
                elif key in modules[0] if modules else False:
                    modules = [m for m in modules if self._matches_filter(m, key, value)]
                else:
                    modules = []

        # Sort: unit_uuid ascending, created_at ascending, uuid ascending
        modules.sort(key=lambda m: (m["unit_uuid"], m["created_at"], m["uuid"]))

        return modules

    def _matches_filter(self, module: dict, key: str, value: str) -> bool:
        """Check if a module matches a generic filter."""
        if key not in module:
            return False
        field_value = module[key]
        if field_value is None:
            return False
        return str(field_value) == value

    def get_by_uuid(self, uuid_str: str) -> dict | None:
        """Get a module by UUID."""
        modules = self._load()
        for module in modules:
            if module["uuid"] == uuid_str:
                return module
        return None

    def flash(self, module_uuid: str, image_uuid: str) -> dict:
        """Flash firmware onto a module."""
        # Step 1: Format validation
        if not is_well_formed_uuid(image_uuid):
            output_error("invalid_image", f"Invalid firmware image UUID format: '{image_uuid}'")

        # Step 2: Module existence
        modules = self._load()
        module = None
        for m in modules:
            if m["uuid"] == module_uuid:
                module = m
                break
        if module is None:
            output_error("not_found", f"Module with UUID '{module_uuid}' not found")

        # Step 3: Image lookup (must be a known image - exists as non-null firmware_image on any module)
        known_images = set()
        for m in modules:
            if m.get("firmware_image") is not None:
                known_images.add(m["firmware_image"])
        if image_uuid not in known_images:
            output_error("flash_failed", f"Firmware image '{image_uuid}' not found in known images")

        # Perform the flash
        module["firmware_image"] = image_uuid
        self._save(modules)
        return module


class SchemaStorage:
    """Handles persistence of schema version to the data directory."""

    SCHEMA_VERSION = 1
    SCHEMA_FILE = ".schema_version"

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.schema_file = self.data_dir / self.SCHEMA_FILE

    def exists(self) -> bool:
        """Check if schema has been initialized."""
        return self.schema_file.exists()

    def init_schema(self) -> dict:
        """Create initial schema. Returns status dict.

        Raises conflict error if schema already exists.
        """
        if self.exists():
            output_error("conflict", "Schema already exists")

        with open(self.schema_file, "w") as f:
            f.write(str(self.SCHEMA_VERSION))

        return {"status": "created"}

    def get_version(self) -> dict:
        """Get current schema version.

        Raises not_found if schema not initialized.
        """
        if not self.exists():
            output_error("not_found", "Schema not initialized")

        version = self.schema_file.read_text().strip()
        return {"version": version}

    def upgrade_schema(self) -> dict:
        """Upgrade schema to current version.

        Returns status dict.
        Raises not_found if schema not initialized.
        """
        if not self.exists():
            output_error("not_found", "Schema not initialized")

        current_version_str = self.schema_file.read_text().strip()
        try:
            current_version = int(current_version_str)
        except ValueError:
            current_version = 0

        if current_version >= self.SCHEMA_VERSION:
            return {"status": "up_to_date", "version": str(self.SCHEMA_VERSION)}

        with open(self.schema_file, "w") as f:
            f.write(str(self.SCHEMA_VERSION))

        return {"status": "upgraded", "version": str(self.SCHEMA_VERSION)}


class RulesLoader:
    """Loads and manages access control rules."""

    def __init__(self, rules_file: str | None):
        self.rules_file = rules_file
        self.rules = {}
        self._load_rules()

    def _load_rules(self) -> None:
        """Load rules from file if it exists."""
        if self.rules_file is None:
            return

        path = Path(self.rules_file)
        if not path.exists():
            # Missing rules file is not an error - treat as no rules
            return

        if yaml is None:
            # If pyyaml not available, can't parse YAML
            return

        with open(self.rules_file, "r") as f:
            content = f.read()

        if self.rules_file.endswith(".json"):
            # JSON format not supported per spec
            return
        elif self.rules_file.endswith((".yaml", ".yml")):
            try:
                self.rules = yaml.safe_load(content) or {}
            except Exception:
                self.rules = {}
        else:
            # Unknown extension
            return

    def get_required_role(self, operation: str) -> str | None:
        """Get required role for operation, or None if no rule."""
        return self.rules.get(operation)


def validate_scope(scope: str) -> bool:
    """Validate --scope value. Returns True if valid.

    Recognized values: project (default), system.
    """
    return scope in {"project", "system"}


def validate_config_file(config_path: str, data_dir: str) -> dict:
    """Validate and load configuration file.

    Returns dict with 'rules_file' and 'enabled_plugins' if present.
    Raises configuration_error on invalid values.
    """
    if not config_path:
        return {}

    path = Path(config_path)
    if not path.exists():
        output_error("configuration_error", f"Config file not found: {config_path}")

    if yaml is None:
        output_error("configuration_error", "PyYAML required to load configuration file")

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception:
        output_error("configuration_error", f"Failed to parse config file: {config_path}")

    result = {}

    # Validate rules_file key
    if "rules_file" in config:
        rules_val = config["rules_file"]
        if not isinstance(rules_val, str) or not rules_val:
            output_error("configuration_error", "rules_file must be a non-empty string path")
        # Resolve relative to data-dir
        result["rules_file"] = str(Path(data_dir) / rules_val)

    # Validate enabled_plugins key
    if "enabled_plugins" in config:
        plugins_val = config["enabled_plugins"]
        if not isinstance(plugins_val, list):
            output_error("configuration_error", "enabled_plugins must be a list of strings")
        for plugin in plugins_val:
            if not isinstance(plugin, str):
                output_error("configuration_error", "enabled_plugins must be a list of strings")
        result["enabled_plugins"] = plugins_val

    return result


def validate_plugins(data_dir: str, enabled_plugins: list[str]) -> None:
    """Validate that all enabled plugins are registered.

    A plugin is registered if a directory named after it exists under <data-dir>/plugins/.
    Raises configuration_error for unrecognized names.
    """
    if not enabled_plugins:
        return

    plugins_dir = Path(data_dir) / "plugins"

    for plugin_name in enabled_plugins:
        plugin_dir = plugins_dir / plugin_name
        if not plugin_dir.exists():
            output_error("configuration_error", f"Unregistered plugin: {plugin_name}")


def check_readiness(rules_loader: RulesLoader | None) -> dict:
    """Perform readiness check and return result.

    Overall status is "pass" only when every individual check passes.
    """
    checks = []

    # Check: rules_file_format
    rules_file_path = rules_loader.rules_file if rules_loader else None
    rules_format_check = {
        "name": "rules_file_format",
        "detail": "",
        "result": "pass",
    }

    if rules_file_path is None:
        rules_format_check["detail"] = "No rules file configured"
    else:
        path = Path(rules_file_path)
        if not path.exists():
            rules_format_check["result"] = "fail"
            rules_format_check["detail"] = f"Rules file does not exist: {rules_file_path}"
        elif rules_file_path.endswith(".json"):
            rules_format_check["result"] = "fail"
            rules_format_check["detail"] = "Rules file has .json extension (must be .yaml or .yml)"
        elif rules_file_path.endswith((".yaml", ".yml")):
            rules_format_check["result"] = "pass"
            rules_format_check["detail"] = f"Rules file format is valid YAML: {rules_file_path}"
        else:
            # Check first non-whitespace character
            try:
                content = path.read_text()
                stripped = content.lstrip()
                if stripped.startswith("{"):
                    rules_format_check["result"] = "fail"
                    rules_format_check["detail"] = "Rules file has .json content (extension must be .yaml or .yml)"
                else:
                    rules_format_check["result"] = "pass"
                    rules_format_check["detail"] = f"Rules file format appears valid: {rules_file_path}"
            except Exception as e:
                rules_format_check["result"] = "fail"
                rules_format_check["detail"] = f"Could not read rules file: {e}"

    checks.append(rules_format_check)

    # All individual checks for now
    all_pass = all(check["result"] == "pass" for check in checks)
    status = "pass" if all_pass else "fail"

    return {"status": status, "checks": checks}


def perform_access_check(
    rules_loader: RulesLoader | None,
    role: str | None,
    scope: str,
    operation: str,
) -> None:
    """Perform access control check for a protected operation.

    Returns None if access granted, otherwise raises forbidden error.
    """
    # Protected operations
    PROTECTED_OPERATIONS = {"blueprint:create", "blueprint:delete"}

    if operation not in PROTECTED_OPERATIONS:
        return

    # Rule 1: System scope is unconditionally forbidden
    if scope == "system":
        output_error("forbidden", f"System scope is unconditionally unsupported for protected operation: {operation}")

    # Rule 2: If no rules file, proceed without checks
    if rules_loader is None:
        return

    # Rule 3: If no rule entry for operation, proceed without checks
    required_role = rules_loader.get_required_role(operation)
    if required_role is None:
        return

    # Rule 4: Role must be provided
    if role is None:
        output_error("forbidden", f"Missing --role for protected operation: {operation}")

    # Rule 5: Role must exactly match required role (case-sensitive)
    if role != required_role:
        output_error("forbidden", f"Role '{role}' does not match required role '{required_role}' for operation: {operation}")


class TagStorage:
    """Handles persistence of tags to the data directory."""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tags_file = self.data_dir / "tags.json"

    def _load(self) -> list[dict]:
        """Load all tags from storage."""
        if not self.tags_file.exists():
            return []
        with open(self.tags_file, "r") as f:
            return json.load(f)

    def _save(self, tags: list[dict]) -> None:
        """Save all tags to storage."""
        with open(self.tags_file, "w") as f:
            json.dump(tags, f, indent=2)

    def create(self, module_uuid: str, key: str, value: str) -> dict:
        """Create a new tag."""
        tags = self._load()

        # Validate required fields
        if not module_uuid or not isinstance(module_uuid, str):
            output_error("validation_error", "module_uuid must be a non-empty string")
        if not key or not isinstance(key, str):
            output_error("validation_error", "key must be a non-empty string")
        if not value or not isinstance(value, str):
            output_error("validation_error", "value must be a non-empty string")

        # Validate module exists
        module_storage = ModuleStorage(self.data_dir)
        module = module_storage.get_by_uuid(module_uuid)
        if module is None:
            output_error("not_found", f"Module with UUID '{module_uuid}' not found")

        # Check for conflict (duplicate module_uuid, key pair)
        for tag in tags:
            if tag["module_uuid"] == module_uuid and tag["key"] == key:
                output_error("conflict", f"Tag with key '{key}' already exists for module '{module_uuid}'")

        # Generate UUID and timestamp
        tag_uuid = str(uuid.uuid4()).lower()
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        tag = {
            "uuid": tag_uuid,
            "module_uuid": module_uuid,
            "key": key,
            "value": value,
            "created_at": timestamp,
        }

        tags.append(tag)
        self._save(tags)

        return tag

    def list_all(self, module_filter: str = None, key_filter: str = None) -> list[dict]:
        """List all tags with optional filtering."""
        tags = self._load()

        # Apply module filter
        if module_filter is not None:
            tags = [t for t in tags if t["module_uuid"] == module_filter]

        # Apply key filter (case-sensitive exact match)
        if key_filter is not None:
            tags = [t for t in tags if t["key"] == key_filter]

        # Sort: module_uuid ascending, key ascending, uuid ascending
        tags.sort(key=lambda t: (t["module_uuid"], t["key"], t["uuid"]))

        return tags

    def get_by_uuid(self, uuid_str: str) -> dict | None:
        """Get a tag by UUID."""
        tags = self._load()
        for tag in tags:
            if tag["uuid"] == uuid_str:
                return tag
        return None

    def delete_by_uuid(self, uuid_str: str) -> int:
        """Delete a tag by UUID. Returns 1 if deleted, 0 if not found."""
        tags = self._load()
        for i, tag in enumerate(tags):
            if tag["uuid"] == uuid_str:
                del tags[i]
                self._save(tags)
                return 1
        return 0


# Command handlers

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


# Module command handlers

def cmd_module_create(args, storage: ModuleStorage) -> None:
    """Handle module create command."""
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    # Validate required fields
    unit_uuid = input_data.get("unit_uuid")
    component_type = input_data.get("component_type")
    firmware_image = input_data.get("firmware_image")

    if not unit_uuid or not isinstance(unit_uuid, str):
        output_error("validation_error", "unit_uuid is required and must be a non-empty string")
    if not component_type or not isinstance(component_type, str):
        output_error("validation_error", "component_type is required and must be a non-empty string")

    module = storage.create(unit_uuid, component_type, firmware_image)
    output_success(module)


def cmd_module_list(args, storage: ModuleStorage) -> None:
    """Handle module list command."""
    modules = storage.list_all(
        unit_filter=args.unit,
        generic_filters=args.filter
    )
    output_success(modules)


def cmd_module_get(args, storage: ModuleStorage) -> None:
    """Handle module get command."""
    module = storage.get_by_uuid(args.uuid)
    if module is None:
        output_error("not_found", f"Module with UUID '{args.uuid}' not found")
    output_success(module)


def cmd_module_flash(args, storage: ModuleStorage) -> None:
    """Handle module flash command."""
    module = storage.flash(args.uuid, args.image)
    output_success(module)


# Tag command handlers

def cmd_tag_create(args, storage: TagStorage) -> None:
    """Handle tag create command."""
    try:
        input_data = json.load(sys.stdin)
    except json.JSONDecodeError:
        output_error("validation_error", "stdin is not valid JSON")

    module_uuid = input_data.get("module_uuid")
    key = input_data.get("key")
    value = input_data.get("value")

    tag = storage.create(module_uuid, key, value)
    output_success(tag)


def cmd_tag_list(args, storage: TagStorage) -> None:
    """Handle tag list command."""
    tags = storage.list_all(
        module_filter=args.module,
        key_filter=args.key
    )
    output_success(tags)


def cmd_tag_get(args, storage: TagStorage) -> None:
    """Handle tag get command."""
    tag = storage.get_by_uuid(args.uuid)
    if tag is None:
        output_error("not_found", f"Tag with UUID '{args.uuid}' not found")
    output_success(tag)


def cmd_tag_delete(args, storage: TagStorage) -> None:
    """Handle tag delete command."""
    count = storage.delete_by_uuid(args.uuid)
    if count == 0:
        output_error("not_found", f"Tag with UUID '{args.uuid}' not found")
    output_success({"deleted": count})


def cmd_tag_update(args, storage: TagStorage) -> None:
    """Handle tag update command."""
    # This command should not exist - return unknown_command
    output_error("unknown_command", "tag update is not a valid command")


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
    parser.add_argument(
        "--config",
        help="Path to YAML configuration file",
    )
    parser.add_argument(
        "--rules-file",
        help="Path to YAML rules file for access control",
    )
    parser.add_argument(
        "--role",
        help="Caller's assigned role",
    )
    parser.add_argument(
        "--scope",
        choices=["project", "system"],
        default="project",
        help="Caller's authorization scope (default: project)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # VERSION subcommand
    version_parser = subparsers.add_parser(
        "version", help="Show version information"
    )
    version_parser.set_defaults(func=lambda args, _: output_version())

    # CHECK subcommand
    check_parser = subparsers.add_parser("check", help="Perform operational checks")
    check_subparsers = check_parser.add_subparsers(
        dest="check_command", required=True
    )

    check_readiness_parser = check_subparsers.add_parser(
        "readiness", help="Validate operational configuration"
    )
    check_readiness_parser.set_defaults(func=lambda args, _: None)

    # SCHEMA subcommand
    schema_parser = subparsers.add_parser("schema", help="Manage schema lifecycle")
    schema_subparsers = schema_parser.add_subparsers(
        dest="schema_command", required=True
    )

    schema_init_parser = schema_subparsers.add_parser(
        "init", help="Create initial schema"
    )
    schema_init_parser.set_defaults(func=lambda args, _: None)

    schema_version_parser = schema_subparsers.add_parser(
        "version", help="Report current schema version"
    )
    schema_version_parser.set_defaults(func=lambda args, _: None)

    schema_upgrade_parser = schema_subparsers.add_parser(
        "upgrade", help="Apply pending schema changes"
    )
    schema_upgrade_parser.set_defaults(func=lambda args, _: None)

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

    # Module subcommands
    module_parser = subparsers.add_parser("module", help="Manage modules")
    module_subparsers = module_parser.add_subparsers(
        dest="module_command", required=True
    )

    # module create
    module_create_parser = module_subparsers.add_parser(
        "create", help="Create a new module from stdin"
    )
    module_create_parser.set_defaults(func=cmd_module_create)

    # module list
    module_list_parser = module_subparsers.add_parser(
        "list", help="List all modules"
    )
    module_list_parser.add_argument(
        "--unit", help="Restrict to modules belonging to this unit"
    )
    module_list_parser.add_argument(
        "--filter", action="append", dest="filter",
        help="Generic field filter; may appear multiple times"
    )
    module_list_parser.set_defaults(func=cmd_module_list)

    # module get
    module_get_parser = module_subparsers.add_parser("get", help="Get a module by UUID")
    module_get_parser.add_argument("uuid", help="Module UUID")
    module_get_parser.set_defaults(func=cmd_module_get)

    # module flash
    module_flash_parser = module_subparsers.add_parser(
        "flash", help="Flash firmware to a module"
    )
    module_flash_parser.add_argument("uuid", help="Module UUID")
    module_flash_parser.add_argument(
        "--image", required=True, help="Firmware image UUID"
    )
    module_flash_parser.set_defaults(func=cmd_module_flash)

    # Tag subcommands
    tag_parser = subparsers.add_parser("tag", help="Manage tags")
    tag_subparsers = tag_parser.add_subparsers(
        dest="tag_command", required=True
    )

    # tag create
    tag_create_parser = tag_subparsers.add_parser(
        "create", help="Create a new tag from stdin"
    )
    tag_create_parser.set_defaults(func=cmd_tag_create)

    # tag list
    tag_list_parser = tag_subparsers.add_parser(
        "list", help="List all tags"
    )
    tag_list_parser.add_argument(
        "--module", help="Restrict to tags belonging to this module"
    )
    tag_list_parser.add_argument(
        "--key", help="Restrict to tags with this key (case-sensitive exact match)"
    )
    tag_list_parser.set_defaults(func=cmd_tag_list)

    # tag get
    tag_get_parser = tag_subparsers.add_parser("get", help="Get a tag by UUID")
    tag_get_parser.add_argument("uuid", help="Tag UUID")
    tag_get_parser.set_defaults(func=cmd_tag_get)

    # tag delete
    tag_delete_parser = tag_subparsers.add_parser(
        "delete", help="Delete a tag by UUID"
    )
    tag_delete_parser.add_argument("uuid", help="Tag UUID")
    tag_delete_parser.set_defaults(func=cmd_tag_delete)

    # tag update (should return unknown_command)
    tag_update_parser = tag_subparsers.add_parser(
        "update", help="This command does not exist"
    )
    tag_update_parser.set_defaults(func=cmd_tag_update)

    args = parser.parse_args()

    # Load and validate config file if provided
    config_rules_file = None
    config_enabled_plugins = []

    if hasattr(args, 'config') and args.config:
        config_data = validate_config_file(args.config, args.data_dir)
        if 'rules_file' in config_data:
            config_rules_file = config_data['rules_file']
        if 'enabled_plugins' in config_data:
            config_enabled_plugins = config_data['enabled_plugins']

    # Determine rules file (CLI flag takes precedence over config)
    effective_rules_file = args.rules_file if hasattr(args, 'rules_file') and args.rules_file else config_rules_file

    # Validate scope
    if not validate_scope(args.scope):
        output_error("validation_error", f"Unrecognized --scope value: '{args.scope}'")

    # Create rules loader
    rules_loader = None
    if effective_rules_file:
        rules_loader = RulesLoader(effective_rules_file)

    # Create schema storage
    schema_storage = SchemaStorage(args.data_dir)

    # Create command-specific storage
    command_storage = None

    # Validate plugins if any are enabled (before command dispatch)
    if config_enabled_plugins:
        validate_plugins(args.data_dir, config_enabled_plugins)

    # Handle schema commands (they don't require command-specific storage)
    if hasattr(args, 'schema_command') or args.command == "schema":
        if args.schema_command == "init":
            result = schema_storage.init_schema()
            output_success(result)
        elif args.schema_command == "version":
            result = schema_storage.get_version()
            output_success(result)
        elif args.schema_command == "upgrade":
            result = schema_storage.upgrade_schema()
            output_success(result)
        else:
            output_error("validation_error", "Unknown schema command")

    # Handle check commands
    if hasattr(args, 'check_command') or args.command == "check":
        if args.check_command == "readiness":
            result = check_readiness(rules_loader)
            output_success(result)
        else:
            output_error("validation_error", "Unknown check command")

    # For commands that need storage, create it
    try:
        if hasattr(args, 'blueprint_command') or args.command == "blueprint":
            command_storage = BlueprintStorage(args.data_dir)
        elif hasattr(args, 'allocation_command') or args.command == "allocation":
            command_storage = AllocationStorage(args.data_dir)
        elif hasattr(args, 'unit_command') or args.command == "unit":
            command_storage = UnitStorage(args.data_dir)
        elif hasattr(args, 'module_command') or args.command == "module":
            command_storage = ModuleStorage(args.data_dir)
        elif hasattr(args, 'tag_command') or args.command == "tag":
            command_storage = TagStorage(args.data_dir)
        elif hasattr(args, 'ledger_command') or args.command == "ledger":
            # ledger capabilities uses UnitStorage
            command_storage = UnitStorage(args.data_dir)
    except Exception as e:
        output_error("validation_error", f"Invalid data directory: {e}")

    # For protected operations, perform access check
    # Protected operations: blueprint:create, blueprint:delete
    PROTECTED_OPERATIONS = {"blueprint:create", "blueprint:delete"}

    if hasattr(args, 'blueprint_command') or args.command == "blueprint":
        blueprint_op = None
        if hasattr(args, 'blueprint_command'):
            blueprint_op = args.blueprint_command
        elif args.command == "blueprint" and hasattr(args, 'blueprint_command'):
            blueprint_op = args.blueprint_command

        if blueprint_op in ["create", "delete"]:
            operation_name = f"blueprint:{blueprint_op}"
            perform_access_check(rules_loader, args.role, args.scope, operation_name)

    # Execute the command
    # Some commands (version, check, schema) already handled above
    if hasattr(args, 'func') and args.func is not None:
        args.func(args, command_storage)
    elif hasattr(args, 'blueprint_command'):
        if args.blueprint_command == "create":
            cmd_create(args, command_storage)
        elif args.blueprint_command == "list":
            cmd_list(args, command_storage)
        elif args.blueprint_command == "get":
            cmd_get(args, command_storage)
        elif args.blueprint_command == "delete":
            cmd_delete(args, command_storage)
        else:
            output_error("validation_error", "Unknown blueprint command")
    elif hasattr(args, 'allocation_command'):
        if args.allocation_command == "create":
            cmd_allocation_create(args, command_storage)
        elif args.allocation_command == "list":
            cmd_allocation_list(args, command_storage)
        elif args.allocation_command == "get":
            cmd_allocation_get(args, command_storage)
        elif args.allocation_command == "delete":
            cmd_allocation_delete(args, command_storage)
        else:
            output_error("validation_error", "Unknown allocation command")
    elif hasattr(args, 'unit_command'):
        if args.unit_command == "create":
            cmd_unit_create(args, command_storage)
        elif args.unit_command == "list":
            cmd_unit_list(args, command_storage)
        elif args.unit_command == "get":
            cmd_unit_get(args, command_storage)
        elif args.unit_command == "activate":
            cmd_unit_activate(args, command_storage)
        elif args.unit_command == "deactivate":
            cmd_unit_deactivate(args, command_storage)
        else:
            output_error("validation_error", "Unknown unit command")
    elif hasattr(args, 'module_command'):
        if args.module_command == "create":
            cmd_module_create(args, command_storage)
        elif args.module_command == "list":
            cmd_module_list(args, command_storage)
        elif args.module_command == "get":
            cmd_module_get(args, command_storage)
        elif args.module_command == "flash":
            cmd_module_flash(args, command_storage)
        else:
            output_error("validation_error", "Unknown module command")
    elif hasattr(args, 'tag_command'):
        if args.tag_command == "create":
            cmd_tag_create(args, command_storage)
        elif args.tag_command == "list":
            cmd_tag_list(args, command_storage)
        elif args.tag_command == "get":
            cmd_tag_get(args, command_storage)
        elif args.tag_command == "delete":
            cmd_tag_delete(args, command_storage)
        elif args.tag_command == "update":
            # Tag update command exists but returns unknown_command error
            cmd_tag_update(args, command_storage)
        else:
            output_error("validation_error", "Unknown tag command")
    elif hasattr(args, 'ledger_command'):
        if args.ledger_command == "capabilities":
            cmd_ledger_capabilities(args, command_storage)
        else:
            output_error("validation_error", "Unknown ledger command")
    else:
        output_error("validation_error", "Unknown command")


if __name__ == "__main__":
    main()